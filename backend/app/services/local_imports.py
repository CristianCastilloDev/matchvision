from __future__ import annotations

import hashlib
import io
import json
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.config import Settings, get_settings
from app.services.entity_resolution import normalize_entity_name


ALLOWED_SUFFIXES = {".csv", ".json", ".zip"}
FOOTBALL_DATA_COLUMNS = {
    "Date": "match_date",
    "HomeTeam": "home_team",
    "AwayTeam": "away_team",
    "FTHG": "home_goals",
    "FTAG": "away_goals",
    "HTHG": "halftime_home_goals",
    "HTAG": "halftime_away_goals",
    "HS": "home_shots",
    "AS": "away_shots",
    "HST": "home_shots_on_target",
    "AST": "away_shots_on_target",
    "HC": "home_corners",
    "AC": "away_corners",
    "HF": "home_fouls",
    "AF": "away_fouls",
    "HY": "home_yellow_cards",
    "AY": "away_yellow_cards",
    "HR": "home_red_cards",
    "AR": "away_red_cards",
    "Referee": "referee",
}


class ImportValidationError(ValueError):
    pass


def _clean_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if hasattr(value, "item"):
        return value.item()
    return value


def _safe_archive_payload(content: bytes, max_bytes: int) -> tuple[str, bytes]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ImportValidationError("El archivo ZIP está dañado") from exc
    candidates: list[zipfile.ZipInfo] = []
    total_size = 0
    for info in archive.infolist():
        member = PurePosixPath(info.filename)
        if member.is_absolute() or ".." in member.parts:
            raise ImportValidationError("El ZIP contiene una ruta no segura")
        if info.is_dir():
            continue
        total_size += info.file_size
        if total_size > max_bytes:
            raise ImportValidationError("El contenido descomprimido supera el límite permitido")
        if member.suffix.casefold() in {".csv", ".json"}:
            candidates.append(info)
    if len(candidates) != 1:
        raise ImportValidationError("El ZIP debe contener exactamente un archivo CSV o JSON")
    info = candidates[0]
    with archive.open(info) as stream:
        payload = stream.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ImportValidationError("El archivo contenido supera el límite permitido")
    return PurePosixPath(info.filename).suffix.casefold(), payload


def parse_local_rows(filename: str, content: bytes, max_bytes: int) -> list[dict[str, Any]]:
    if len(content) > max_bytes:
        raise ImportValidationError("El archivo supera el límite permitido")
    suffix = Path(filename).suffix.casefold()
    if suffix not in ALLOWED_SUFFIXES:
        raise ImportValidationError("Solo se permiten archivos CSV, JSON o ZIP")
    if suffix == ".zip":
        suffix, content = _safe_archive_payload(content, max_bytes)
    if suffix == ".csv":
        try:
            frame = pd.read_csv(io.BytesIO(content))
        except Exception as exc:
            raise ImportValidationError(f"No se pudo leer el CSV: {exc}") from exc
        frame = frame.rename(columns=FOOTBALL_DATA_COLUMNS)
        return [
            {str(key): _clean_value(value) for key, value in row.items()}
            for row in frame.to_dict(orient="records")
        ]
    try:
        decoded = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImportValidationError(f"JSON inválido: {exc}") from exc
    if isinstance(decoded, dict):
        decoded = decoded.get("records", decoded.get("matches", [decoded]))
    if not isinstance(decoded, list) or not all(isinstance(row, dict) for row in decoded):
        raise ImportValidationError("El JSON debe ser una lista de objetos o contener 'records'")
    return [{str(key): _clean_value(value) for key, value in row.items()} for row in decoded]


def _parse_datetime(value: Any) -> datetime:
    if value is None:
        raise ImportValidationError("Falta match_date/Date")
    try:
        parsed = pd.to_datetime(value, dayfirst=True, utc=True)
    except Exception as exc:
        raise ImportValidationError(f"Fecha inválida: {value}") from exc
    if pd.isna(parsed):
        raise ImportValidationError(f"Fecha inválida: {value}")
    return parsed.to_pydatetime()


def _optional_int(row: dict[str, Any], key: str) -> int | None:
    value = row.get(key)
    if value is None:
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError) as exc:
        raise ImportValidationError(f"{key} debe ser entero") from exc
    if number < 0:
        raise ImportValidationError(f"{key} no puede ser negativo")
    return number


def _boolean(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "si", "sí"}


def _source_datetime(value: Any) -> datetime:
    if value is None:
        return datetime.now(UTC)
    try:
        parsed = pd.to_datetime(value, utc=True)
    except Exception:
        return datetime.now(UTC)
    return parsed.to_pydatetime() if not pd.isna(parsed) else datetime.now(UTC)


def _get_or_create_competition(db: Session, name: str, run_id: str) -> models.Competition:
    normalized = normalize_entity_name(name)
    for competition in db.scalars(
        select(models.Competition).where(models.Competition.is_mock_data.is_(False))
    ).all():
        if normalize_entity_name(competition.name) == normalized:
            return competition
    entity = models.Competition(
        external_id=f"local-{hashlib.sha1(normalized.encode()).hexdigest()[:16]}",
        name=name,
        data_source="local",
        source_updated_at=datetime.now(UTC),
        is_mock_data=False,
    )
    db.add(entity)
    db.flush()
    return entity


def _get_or_create_season(
    db: Session, competition: models.Competition, name: str
) -> models.Season:
    entity = db.scalar(
        select(models.Season).where(
            models.Season.competition_id == competition.id,
            models.Season.name == name,
            models.Season.is_mock_data.is_(False),
        )
    )
    if entity:
        return entity
    entity = models.Season(
        external_id=f"local-{competition.id}-{normalize_entity_name(name)}",
        competition_id=competition.id,
        name=name,
        data_source="local",
        source_updated_at=datetime.now(UTC),
        is_mock_data=False,
    )
    db.add(entity)
    db.flush()
    return entity


def _get_or_create_team(db: Session, name: str) -> models.Team:
    normalized = normalize_entity_name(name)
    alias = db.scalar(
        select(models.TeamAlias)
        .join(models.Team, models.Team.id == models.TeamAlias.team_id)
        .where(
            models.TeamAlias.normalized_alias == normalized,
            models.Team.is_mock_data.is_(False),
        )
        .limit(1)
    )
    if alias:
        return db.get(models.Team, alias.team_id)  # type: ignore[return-value]
    team = models.Team(
        external_id=f"local-{hashlib.sha1(normalized.encode()).hexdigest()[:16]}",
        name=name,
        data_source="local",
        source_updated_at=datetime.now(UTC),
        is_mock_data=False,
    )
    db.add(team)
    db.flush()
    db.add(
        models.TeamAlias(
            team_id=team.id,
            provider="local",
            alias=name,
            normalized_alias=normalized,
        )
    )
    return team


def _get_or_create_referee(db: Session, name: str | None) -> models.Referee | None:
    if not name:
        return None
    normalized = normalize_entity_name(name)
    for entity in db.scalars(select(models.Referee).where(models.Referee.data_source == "local")):
        if normalize_entity_name(entity.name) == normalized:
            return entity
    entity = models.Referee(
        external_id=f"local-{hashlib.sha1(normalized.encode()).hexdigest()[:16]}",
        name=name,
        data_source="local",
        source_updated_at=datetime.now(UTC),
        is_mock_data=False,
    )
    db.add(entity)
    db.flush()
    return entity


def _record_fingerprint(
    competition: models.Competition,
    season: models.Season,
    match_date: datetime,
    home: models.Team,
    away: models.Team,
) -> str:
    raw = f"{competition.id}|{season.id}|{match_date.isoformat()}|{home.id}|{away.id}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _add_statistics(
    db: Session,
    match: models.Match,
    team: models.Team,
    row: dict[str, Any],
    prefix: str,
    *,
    data_source: str,
    is_mock_data: bool,
) -> None:
    mapping = {
        "shots": f"{prefix}_shots",
        "shots_on_target": f"{prefix}_shots_on_target",
        "corners": f"{prefix}_corners",
        "fouls": f"{prefix}_fouls",
        "yellow_cards": f"{prefix}_yellow_cards",
        "red_cards": f"{prefix}_red_cards",
    }
    values = {field: _optional_int(row, source) for field, source in mapping.items()}
    if not any(value is not None for value in values.values()):
        return
    db.add(
        models.TeamMatchStatistics(
            match_id=match.id,
            team_id=team.id,
            **values,
            data_source=data_source,
            source_updated_at=datetime.now(UTC),
            is_mock_data=is_mock_data,
        )
    )


def persist_match_rows(
    db: Session,
    rows: list[dict[str, Any]],
    *,
    competition_name: str,
    season_name: str,
    run: models.DataIngestionRun,
) -> None:
    competition = _get_or_create_competition(db, competition_name, run.id)
    season = _get_or_create_season(db, competition, season_name)
    run.downloaded_records = len(rows)
    errors: list[dict[str, Any]] = []
    valid = 0
    rejected = 0
    for index, row in enumerate(rows, start=2):
        try:
            row_source = str(row.get("data_source") or run.source or "local_upload")[:50]
            row_is_mock = _boolean(row.get("is_mock_data"), run.is_mock_data)
            row_source_updated_at = _source_datetime(row.get("source_updated_at"))
            home_name = str(
                row.get("home_team") or row.get("home_team_name") or row.get("HomeTeam") or ""
            ).strip()
            away_name = str(
                row.get("away_team") or row.get("away_team_name") or row.get("AwayTeam") or ""
            ).strip()
            if not home_name or not away_name:
                raise ImportValidationError("Falta home_team o away_team")
            home = _get_or_create_team(db, home_name)
            away = _get_or_create_team(db, away_name)
            if home.id == away.id:
                raise ImportValidationError("El local y visitante resuelven al mismo equipo")
            match_date = _parse_datetime(row.get("match_date") or row.get("date"))
            fingerprint = str(row.get("external_id") or _record_fingerprint(competition, season, match_date, home, away))
            duplicate = db.scalar(
                select(models.Match).where(
                    models.Match.data_source == row_source,
                    models.Match.external_id == fingerprint,
                )
            )
            if duplicate:
                errors.append(
                    {"row": index, "code": "duplicate", "severity": "warning", "message": "Registro omitido por deduplicación"}
                )
                continue
            home_goals = _optional_int(row, "home_goals")
            away_goals = _optional_int(row, "away_goals")
            if (home_goals is None) != (away_goals is None):
                raise ImportValidationError("home_goals y away_goals deben venir juntos")
            halftime_home = _optional_int(row, "halftime_home_goals")
            halftime_away = _optional_int(row, "halftime_away_goals")
            if halftime_home is not None and home_goals is not None and halftime_home > home_goals:
                raise ImportValidationError("halftime_home_goals supera home_goals")
            if halftime_away is not None and away_goals is not None and halftime_away > away_goals:
                raise ImportValidationError("halftime_away_goals supera away_goals")
            referee = _get_or_create_referee(
                db, _clean_value(row.get("referee") or row.get("referee_name"))
            )
            match = models.Match(
                external_id=fingerprint,
                competition_id=competition.id,
                season_id=season.id,
                home_team_id=home.id,
                away_team_id=away.id,
                match_date=match_date,
                status="finished" if home_goals is not None else "scheduled",
                home_score=home_goals,
                away_score=away_goals,
                halftime_home_score=halftime_home,
                halftime_away_score=halftime_away,
                referee_id=referee.id if referee else None,
                ingestion_run_id=run.id,
                venue=_clean_value(row.get("venue")),
                round_name=_clean_value(row.get("round_name") or row.get("round")),
                data_source=row_source,
                source_updated_at=row_source_updated_at,
                is_mock_data=row_is_mock,
            )
            db.add(match)
            db.flush()
            _add_statistics(
                db,
                match,
                home,
                row,
                "home",
                data_source=row_source,
                is_mock_data=row_is_mock,
            )
            _add_statistics(
                db,
                match,
                away,
                row,
                "away",
                data_source=row_source,
                is_mock_data=row_is_mock,
            )
            db.flush()
            valid += 1
        except Exception as exc:
            db.rollback()
            # Reload run after rollback; prior flushed rows remain only after explicit commit below,
            # so importing is intentionally all-or-partial by validated batches.
            run = db.get(models.DataIngestionRun, run.id)  # type: ignore[assignment]
            competition = _get_or_create_competition(db, competition_name, run.id)
            season = _get_or_create_season(db, competition, season_name)
            rejected += 1
            errors.append({"row": index, "code": "invalid_row", "severity": "error", "message": str(exc)})
        else:
            db.commit()
            run = db.get(models.DataIngestionRun, run.id)  # type: ignore[assignment]
            competition = db.get(models.Competition, competition.id)  # type: ignore[assignment]
            season = db.get(models.Season, season.id)  # type: ignore[assignment]
    run = db.get(models.DataIngestionRun, run.id)  # type: ignore[assignment]
    run.downloaded_records = len(rows)
    run.valid_records = valid
    run.rejected_records = rejected
    run.errors = errors


def process_import(
    db: Session,
    run: models.DataIngestionRun,
    *,
    content: bytes,
    filename: str,
    competition: str,
    season: str,
    settings: Settings | None = None,
) -> models.DataIngestionRun:
    settings = settings or get_settings()
    started = time.monotonic()
    run.status = "running"
    run.started_at = datetime.now(UTC)
    run.errors = []
    db.commit()
    try:
        rows = parse_local_rows(filename, content, settings.max_import_bytes)
        persist_match_rows(
            db,
            rows,
            competition_name=competition,
            season_name=season,
            run=run,
        )
        run = db.get(models.DataIngestionRun, run.id)  # type: ignore[assignment]
        run.status = "completed_with_errors" if run.rejected_records else "completed"
    except Exception as exc:
        db.rollback()
        run = db.get(models.DataIngestionRun, run.id)  # type: ignore[assignment]
        run.status = "failed"
        run.errors = [{"row": None, "code": "file_error", "severity": "error", "message": str(exc)}]
    run.completed_at = datetime.now(UTC)
    run.duration_seconds = round(time.monotonic() - started, 6)
    db.commit()
    db.refresh(run)
    return run


def create_import_from_bytes(
    db: Session,
    *,
    filename: str,
    content: bytes,
    competition: str,
    season: str,
    source: str = "local_upload",
    settings: Settings | None = None,
) -> models.DataIngestionRun:
    settings = settings or get_settings()
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name:
        raise ImportValidationError("Nombre de archivo no seguro")
    if len(content) > settings.max_import_bytes:
        raise ImportValidationError("El archivo supera el límite permitido")
    digest = hashlib.sha256(content).hexdigest()
    run = models.DataIngestionRun(
        source=source[:50],
        original_filename=safe_name,
        file_hash=digest,
        import_options={"competition": competition, "season": season, "entity_type": "matches"},
        pipeline_version="1.0.0",
        is_mock_data=False,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    settings.import_dir.mkdir(parents=True, exist_ok=True)
    stored = settings.import_dir / f"{run.id}{Path(safe_name).suffix.casefold()}"
    stored.write_bytes(content)
    run.stored_path = str(stored.resolve())
    db.commit()
    return process_import(
        db,
        run,
        content=content,
        filename=safe_name,
        competition=competition,
        season=season,
        settings=settings,
    )


def create_import_from_path(
    db: Session,
    *,
    file_path: Path,
    competition: str,
    season: str,
    source: str = "local_file",
    settings: Settings | None = None,
) -> models.DataIngestionRun:
    settings = settings or get_settings()
    resolved = file_path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ImportValidationError("La ruta no apunta a un archivo")
    if resolved.stat().st_size > settings.max_import_bytes:
        raise ImportValidationError("El archivo supera el límite permitido")
    return create_import_from_bytes(
        db,
        filename=resolved.name,
        content=resolved.read_bytes(),
        competition=competition,
        season=season,
        source=source,
        settings=settings,
    )


def create_import_from_records(
    db: Session,
    *,
    filename: str,
    content: bytes,
    records: list[dict[str, Any]],
    competition: str,
    season: str,
    source: str,
    settings: Settings | None = None,
) -> models.DataIngestionRun:
    """Persist already-normalized records while retaining their original local file."""

    settings = settings or get_settings()
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name:
        raise ImportValidationError("Nombre de archivo no seguro")
    if len(content) > settings.max_import_bytes:
        raise ImportValidationError("El archivo supera el límite permitido")
    started = time.monotonic()
    run = models.DataIngestionRun(
        source=source[:50],
        original_filename=safe_name,
        file_hash=hashlib.sha256(content).hexdigest(),
        import_options={"competition": competition, "season": season, "entity_type": "matches"},
        status="running",
        pipeline_version="1.0.0",
        is_mock_data=any(_boolean(row.get("is_mock_data")) for row in records),
    )
    db.add(run)
    db.commit()
    settings.import_dir.mkdir(parents=True, exist_ok=True)
    stored = settings.import_dir / f"{run.id}{Path(safe_name).suffix.casefold()}"
    stored.write_bytes(content)
    run.stored_path = str(stored.resolve())
    db.commit()
    try:
        persist_match_rows(
            db,
            records,
            competition_name=competition,
            season_name=season,
            run=run,
        )
        run = db.get(models.DataIngestionRun, run.id)  # type: ignore[assignment]
        run.status = "completed_with_errors" if run.rejected_records else "completed"
    except Exception as exc:
        db.rollback()
        run = db.get(models.DataIngestionRun, run.id)  # type: ignore[assignment]
        run.status = "failed"
        run.errors = [{"row": None, "code": "file_error", "severity": "error", "message": str(exc)}]
    run.completed_at = datetime.now(UTC)
    run.duration_seconds = round(time.monotonic() - started, 6)
    db.commit()
    db.refresh(run)
    return run


def reprocess_import(db: Session, run: models.DataIngestionRun) -> models.DataIngestionRun:
    if not run.stored_path:
        raise ImportValidationError("La importación no conserva un archivo reprocesable")
    path = Path(run.stored_path)
    if not path.is_file():
        raise ImportValidationError("El archivo original ya no existe")
    try:
        db.execute(delete(models.Match).where(models.Match.ingestion_run_id == run.id))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ImportValidationError(
            "No se reprocesa una importación con predicciones auditables vinculadas"
        ) from exc
    options = run.import_options
    if run.source == "football_data_local_file":
        from app.providers.football_data import FootballDataLocalProvider

        dataset = FootballDataLocalProvider().import_file(
            path,
            competition=str(options["competition"]),
            season=str(options["season"]),
        )
        run.status = "running"
        run.started_at = datetime.now(UTC)
        persist_match_rows(
            db,
            list(dataset.records),
            competition_name=str(options["competition"]),
            season_name=str(options["season"]),
            run=run,
        )
        run = db.get(models.DataIngestionRun, run.id)  # type: ignore[assignment]
        run.status = "completed_with_errors" if run.rejected_records else "completed"
        run.completed_at = datetime.now(UTC)
        db.commit()
        db.refresh(run)
        return run
    return process_import(
        db,
        run,
        content=path.read_bytes(),
        filename=run.original_filename or path.name,
        competition=str(options["competition"]),
        season=str(options["season"]),
    )


def delete_import(db: Session, run: models.DataIngestionRun, *, delete_records: bool = True) -> None:
    stored_path = Path(run.stored_path) if run.stored_path else None
    try:
        if delete_records:
            db.execute(delete(models.Match).where(models.Match.ingestion_run_id == run.id))
        db.delete(run)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ImportValidationError(
            "No se elimina una importación con predicciones auditables vinculadas"
        ) from exc
    if stored_path and stored_path.is_file():
        stored_path.unlink()


def _resolve_player(db: Session, name: str, team: str) -> int | None:
    team_obj = db.scalar(
        select(models.Team).where(models.Team.name.ilike(team.strip())).limit(1)
    )
    if not team_obj:
        return None
    player = db.scalar(
        select(models.Player).where(
            models.Player.name.ilike(name.strip()),
            models.Player.current_team_id == team_obj.id,
        ).limit(1)
    )
    if player:
        return player.id
    player = db.scalar(
        select(models.Player).where(
            models.Player.name.ilike(name.strip()),
        ).limit(1)
    )
    return player.id if player else None


def import_player_matches(
    db: Session, items: list[dict],
) -> dict[str, Any]:
    imported = 0
    errors: list[str] = []
    for idx, item in enumerate(items):
        match_id = item.get("match_id")
        player_name = item.get("player_name", "").strip()
        team = item.get("team", "").strip()
        if not player_name or not team:
            errors.append(f"Fila {idx}: faltan player_name o team")
            continue
        player_id = _resolve_player(db, player_name, team)
        if player_id is None:
            errors.append(f"Fila {idx}: jugador '{player_name}' no encontrado en '{team}'")
            continue
        existing = db.scalar(
            select(models.PlayerMatch).where(
                models.PlayerMatch.match_id == match_id,
                models.PlayerMatch.player_id == player_id,
            ).limit(1)
        )
        team_obj = db.scalar(
            select(models.Team).where(models.Team.name.ilike(team.strip())).limit(1)
        )
        team_id = team_obj.id if team_obj else None
        if existing:
            for field in ("minutes_played", "goals", "assists", "shots", "shots_on_target",
                          "xg", "yellow_cards", "red_cards", "fouls", "started", "position"):
                val = item.get(field)
                if val is not None:
                    setattr(existing, field, val)
            existing.data_source = "manual"
            existing.source_updated_at = datetime.now(UTC)
        else:
            record = models.PlayerMatch(
                match_id=match_id,
                player_id=player_id,
                team_id=team_id,
                started=item.get("started", True),
                minutes_played=item.get("minutes_played"),
                position=item.get("position"),
                goals=item.get("goals", 0),
                assists=item.get("assists", 0),
                shots=item.get("shots", 0),
                shots_on_target=item.get("shots_on_target", 0),
                xg=item.get("xg"),
                yellow_cards=item.get("yellow_cards", 0),
                red_cards=item.get("red_cards", 0),
                fouls=item.get("fouls", 0),
                data_source="manual",
                source_updated_at=datetime.now(UTC),
                is_mock_data=False,
            )
            db.add(record)
        imported += 1
    db.commit()
    return {"imported": imported, "errors": errors}


def _resolve_team(db: Session, name: str) -> models.Team | None:
    return db.scalar(
        select(models.Team).where(models.Team.name.ilike(name.strip())).limit(1)
    )


def import_match_events(
    db: Session, items: list[dict],
) -> dict[str, Any]:
    imported = 0
    errors: list[str] = []
    for idx, item in enumerate(items):
        match_id = item.get("match_id")
        event_type = item.get("event_type", "").strip()
        if not event_type:
            errors.append(f"Fila {idx}: falta event_type")
            continue
        team_id = None
        team_name = item.get("team")
        if team_name:
            team = _resolve_team(db, team_name)
            if team:
                team_id = team.id
        player_id = None
        player_name = item.get("player_name")
        if player_name:
            player = db.scalar(
                select(models.Player).where(
                    models.Player.name.ilike(player_name.strip())
                ).limit(1)
            )
            if player:
                player_id = player.id
        event = models.MatchEvent(
            match_id=match_id,
            team_id=team_id,
            player_id=player_id,
            event_type=event_type,
            minute=item.get("minute"),
            second=item.get("second"),
            period=item.get("period"),
            payload=item.get("payload", {}),
            data_source="manual",
            source_updated_at=datetime.now(UTC),
            is_mock_data=False,
        )
        db.add(event)
        imported += 1
    db.commit()
    return {"imported": imported, "errors": errors}


CSV_TEMPLATES = {
    "matches": "competition,season,match_date,home_team,away_team,home_goals,away_goals,home_yellow_cards,away_yellow_cards,home_red_cards,away_red_cards,home_corners,away_corners\n",
    "players": "player_name,team,position,active,penalty_taker,free_kick_taker\n",
    "player_matches": "match_id,player_name,team,started,minutes_played,goals,shots,shots_on_target,xg,yellow_cards,red_cards\n",
    "upcoming_matches": "competition,season,match_date,home_team,away_team,venue,referee\n",
}
