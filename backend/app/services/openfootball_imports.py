"""Transactional, offline-only persistence for OpenFootball datasets."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.config import Settings, get_settings
from app.data_sources.openfootball.entity_resolver import resolve_openfootball_name
from app.data_sources.openfootball.importer import (
    OpenFootballImportError,
    OpenFootballRepositoryResult,
    import_openfootball_repository,
    serialize_openfootball_match,
)
from app.data_sources.openfootball.validators import (
    normalize_openfootball_season,
    normalize_openfootball_team,
)
from app.services.entity_resolution import normalize_entity_name
from app.services.openfootball_catalogs import (
    OpenFootballCatalogBundle,
    discover_openfootball_catalogs,
    persist_openfootball_catalogs,
)


SOURCE_NAME = "openfootball"
PIPELINE_VERSION = "openfootball-1.0.0"
MAX_PREVIEW_MATCHES = 200
MAX_STORED_RAW_BYTES = 64 * 1024
_CANONICAL_SEASON = re.compile(r"\d{4}(?:-\d{2})?")


class OpenFootballPersistenceError(ValueError):
    """Raised when a staged OpenFootball import cannot be safely processed."""


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_upload_path(value: str) -> PurePosixPath:
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts or not path.name:
        raise OpenFootballPersistenceError(f"Nombre de archivo no seguro: {value!r}")
    if path.suffix.casefold() not in {".json", ".txt", ".zip"}:
        raise OpenFootballPersistenceError("Solo se permiten archivos ZIP, JSON o Football.TXT")
    return path


def _upload_digest(files: Sequence[tuple[PurePosixPath, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(files, key=lambda item: item[0].as_posix()):
        digest.update(name.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()


def _bounded_json(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        payload: Any = dict(value)
    elif value is None:
        payload = {}
    else:
        payload = {"value": value}
    encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    if len(encoded) <= MAX_STORED_RAW_BYTES:
        return json.loads(encoded)
    return {
        "truncated": True,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "original_bytes": len(encoded),
    }


def _run_envelope(run: models.DataIngestionRun) -> dict[str, Any]:
    payload = dict(run.preview_payload or {})
    payload.update(
        {
            "import_id": run.id,
            "status": run.status,
            "metrics": dict(run.import_metrics or payload.get("metrics") or {}),
            "errors": list(run.errors or payload.get("errors") or []),
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        }
    )
    payload.setdefault("detection", {})
    payload.setdefault("preview_matches", [])
    payload.setdefault("quality_by_competition", [])
    payload.setdefault("warnings", [])
    return payload


def _canonical_requested_season(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OpenFootballPersistenceError("La temporada debe ser texto")
    if not value.strip():
        return None
    canonical = normalize_openfootball_season(value)
    if canonical is None or _CANONICAL_SEASON.fullmatch(canonical) is None:
        raise OpenFootballPersistenceError(
            "La temporada debe incluir un año: YYYY, YYYY-YY, YYYY/YYYY o YYYY-YYYY"
        )
    return canonical


def _record_value(record: Any, field: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(field)
    return getattr(record, field, None)


def _ensure_result_identity(result: OpenFootballRepositoryResult) -> None:
    """Apply the same identity gate before preview and before persistence."""

    problems: list[str] = []
    for index, record in enumerate(result.matches, start=1):
        source_file = str(_record_value(record, "source_file") or "dataset")
        competition = str(_record_value(record, "competition") or "").strip()
        raw_season = str(_record_value(record, "season") or "").strip()
        canonical_season = normalize_openfootball_season(raw_season)
        if not competition or competition.casefold() in {"unknown", "unknown competition", "n/a"}:
            problems.append(f"{source_file} registro {index}: falta competition")
        if (
            not raw_season
            or canonical_season is None
            or _CANONICAL_SEASON.fullmatch(canonical_season) is None
        ):
            problems.append(
                f"{source_file} registro {index}: falta season con año válido"
            )
        if len(problems) >= 10:
            break
    if problems:
        raise OpenFootballPersistenceError(
            "OpenFootball preview/import rechazado por identidad incompleta: "
            + "; ".join(problems)
        )


def _result_preview_payload(
    result: OpenFootballRepositoryResult, *, preview_limit: int
) -> dict[str, Any]:
    payload = result.to_dict(preview_limit=preview_limit)
    for record in payload.get("preview_matches", []):
        if isinstance(record, dict) and record.get("season"):
            record["season"] = normalize_openfootball_season(str(record["season"]))
    detection = payload.get("detection")
    if isinstance(detection, dict):
        detection["seasons"] = sorted(
            {
                canonical
                for value in detection.get("seasons", [])
                if (canonical := normalize_openfootball_season(str(value)))
            }
        )
    for quality in payload.get("quality_by_competition", []):
        if isinstance(quality, dict):
            quality["seasons_available"] = sorted(
                {
                    canonical
                    for value in quality.get("seasons_available", [])
                    if (canonical := normalize_openfootball_season(str(value)))
                }
            )
    return payload


def _catalog_metrics(
    bundle: OpenFootballCatalogBundle, *, imported: int = 0
) -> dict[str, int]:
    counts = {"leagues": 0, "clubs": 0, "players": 0}
    for catalog in bundle.catalogs:
        counts[catalog.kind] += len(catalog.records)
    return {
        "catalog_files_scanned": bundle.files_scanned,
        "catalog_records_found": bundle.records_found,
        "catalog_records_imported": imported,
        "leagues_found": counts["leagues"],
        "clubs_found": counts["clubs"],
        "players_found": counts["players"],
    }


def _catalog_summary(bundle: OpenFootballCatalogBundle) -> dict[str, Any]:
    metrics = _catalog_metrics(bundle)
    return {
        "files_scanned": metrics["catalog_files_scanned"],
        "records_found": metrics["catalog_records_found"],
        "leagues_found": metrics["leagues_found"],
        "clubs_found": metrics["clubs_found"],
        "players_found": metrics["players_found"],
        "identity_only": True,
    }


def _create_preview_run(
    db: Session,
    *,
    stored_path: Path,
    original_filename: str,
    file_hash: str,
    result: OpenFootballRepositoryResult,
    competition: str | None,
    season: str | None,
    preview_limit: int,
) -> models.DataIngestionRun:
    preview_limit = max(1, min(preview_limit, MAX_PREVIEW_MATCHES))
    _ensure_result_identity(result)
    catalogs = discover_openfootball_catalogs(
        stored_path, repository=result.detection.source_repository
    )
    metrics = {**result.metrics, **_catalog_metrics(catalogs)}
    run = models.DataIngestionRun(
        source=SOURCE_NAME,
        original_filename=original_filename[:255],
        stored_path=str(stored_path),
        file_hash=file_hash,
        import_options={
            "competition": competition,
            "season": season,
            "preview_limit": preview_limit,
            "offline": True,
        },
        import_metrics=metrics,
        status="previewed",
        downloaded_records=len(result.matches) + catalogs.records_found,
        valid_records=0,
        rejected_records=len(result.errors),
        errors=[error.to_dict() for error in result.errors],
        pipeline_version=PIPELINE_VERSION,
        is_mock_data=False,
    )
    db.add(run)
    db.flush()
    preview = _result_preview_payload(result, preview_limit=preview_limit)
    preview["metrics"] = metrics
    preview["catalogs"] = _catalog_summary(catalogs)
    preview["warnings"] = list(
        dict.fromkeys([*result.warnings, *catalogs.warnings])
    )
    preview["import_id"] = run.id
    preview["status"] = run.status
    preview["started_at"] = run.started_at.isoformat()
    preview["completed_at"] = None
    run.preview_payload = preview
    db.commit()
    db.refresh(run)
    return run


def preview_openfootball_uploads(
    db: Session,
    *,
    uploads: Sequence[tuple[str, bytes]],
    relative_paths: Sequence[str] | None = None,
    competition: str | None = None,
    season: str | None = None,
    preview_limit: int = 50,
    settings: Settings | None = None,
) -> models.DataIngestionRun:
    """Stage bounded web uploads, parse them offline and persist only a compact preview."""

    settings = settings or get_settings()
    competition = competition.strip() if competition and competition.strip() else None
    season = _canonical_requested_season(season)
    if not uploads:
        raise OpenFootballPersistenceError("Debes subir al menos un archivo")
    if relative_paths is not None and len(relative_paths) != len(uploads):
        raise OpenFootballPersistenceError("relative_paths debe corresponder uno a uno con files")

    staged: list[tuple[PurePosixPath, bytes]] = []
    total_bytes = 0
    seen: set[str] = set()
    for index, (filename, content) in enumerate(uploads):
        candidate = relative_paths[index] if relative_paths is not None else filename
        safe = _safe_upload_path(candidate)
        key = safe.as_posix().casefold()
        if key in seen:
            raise OpenFootballPersistenceError(f"Ruta de archivo duplicada: {safe}")
        seen.add(key)
        total_bytes += len(content)
        if total_bytes > settings.max_import_bytes:
            raise OpenFootballPersistenceError("La carga agregada supera el límite permitido")
        staged.append((safe, content))

    zip_count = sum(name.suffix.casefold() == ".zip" for name, _ in staged)
    if zip_count and len(staged) != 1:
        raise OpenFootballPersistenceError("Un ZIP debe subirse solo; no puede mezclarse con otros archivos")

    # Create a temporary run id before writing so every upload has an isolated root.
    placeholder = models.DataIngestionRun(
        source=SOURCE_NAME,
        original_filename=staged[0][0].name if len(staged) == 1 else "openfootball-upload",
        file_hash=_upload_digest(staged),
        import_options={"competition": competition, "season": season, "offline": True},
        status="staging",
        pipeline_version=PIPELINE_VERSION,
        is_mock_data=False,
    )
    db.add(placeholder)
    db.commit()
    db.refresh(placeholder)

    stage_root = settings.import_dir / SOURCE_NAME / placeholder.id
    try:
        stage_root.mkdir(parents=True, exist_ok=False)
        for relative, content in staged:
            destination = stage_root.joinpath(*relative.parts)
            resolved_parent = destination.parent.resolve()
            if stage_root.resolve() not in (resolved_parent, *resolved_parent.parents):
                raise OpenFootballPersistenceError("La ruta de carga escapa del directorio asignado")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        parse_path = stage_root.joinpath(*staged[0][0].parts) if len(staged) == 1 else stage_root
        result = import_openfootball_repository(
            parse_path,
            competition=competition,
            season=season,
        )
        _ensure_result_identity(result)
        catalogs = discover_openfootball_catalogs(
            parse_path, repository=result.detection.source_repository
        )
        metrics = {**result.metrics, **_catalog_metrics(catalogs)}
        placeholder.stored_path = str(parse_path.resolve())
        placeholder.import_metrics = metrics
        placeholder.downloaded_records = len(result.matches) + catalogs.records_found
        placeholder.rejected_records = len(result.errors)
        placeholder.errors = [error.to_dict() for error in result.errors]
        placeholder.status = "previewed"
        preview_limit = max(1, min(preview_limit, MAX_PREVIEW_MATCHES))
        placeholder.import_options = {
            "competition": competition,
            "season": season,
            "preview_limit": preview_limit,
            "offline": True,
        }
        preview = _result_preview_payload(result, preview_limit=preview_limit)
        preview["metrics"] = metrics
        preview["catalogs"] = _catalog_summary(catalogs)
        preview["warnings"] = list(
            dict.fromkeys([*result.warnings, *catalogs.warnings])
        )
        preview.update(
            {
                "import_id": placeholder.id,
                "status": placeholder.status,
                "started_at": placeholder.started_at.isoformat(),
                "completed_at": None,
            }
        )
        placeholder.preview_payload = preview
        db.commit()
        db.refresh(placeholder)
        return placeholder
    except Exception as exc:
        db.rollback()
        run = db.get(models.DataIngestionRun, placeholder.id)
        if run is not None:
            run.status = "failed"
            run.errors = [{"source_file": "", "code": "preview_error", "message": str(exc)}]
            db.commit()
        shutil.rmtree(stage_root, ignore_errors=True)
        if isinstance(exc, (OpenFootballPersistenceError, OpenFootballImportError)):
            raise
        raise OpenFootballPersistenceError(str(exc)) from exc


def preview_openfootball_path(
    db: Session,
    path: str | Path,
    *,
    competition: str | None = None,
    season: str | None = None,
    preview_limit: int = 50,
) -> models.DataIngestionRun:
    """Preview an explicit local CLI path. This function is never exposed over HTTP."""

    competition = competition.strip() if competition and competition.strip() else None
    season = _canonical_requested_season(season)
    source = Path(path).expanduser()
    if source.is_symlink():
        raise OpenFootballPersistenceError("La ruta raíz no puede ser un enlace simbólico")
    resolved = source.resolve(strict=True)
    result = import_openfootball_repository(
        resolved,
        competition=competition,
        season=season,
    )
    return _create_preview_run(
        db,
        stored_path=resolved,
        original_filename=resolved.name,
        file_hash=result.detection.content_hash,
        result=result,
        competition=competition,
        season=season,
        preview_limit=preview_limit,
    )


def _mapping(
    db: Session, entity_type: str, repository: str, normalized: str
) -> models.OpenFootballEntityMapping | None:
    return db.scalar(
        select(models.OpenFootballEntityMapping).where(
            models.OpenFootballEntityMapping.entity_type == entity_type,
            models.OpenFootballEntityMapping.source_repository == repository,
            models.OpenFootballEntityMapping.normalized_name == normalized,
        )
    )


def _save_mapping(
    db: Session,
    *,
    entity_type: str,
    original: str,
    normalized: str,
    repository: str,
    entity_id: int | None,
    confidence: float,
    status: str,
    notes: str | None = None,
) -> models.OpenFootballEntityMapping:
    existing = _mapping(db, entity_type, repository, normalized)
    if existing is not None:
        existing.original_name = original
        existing.internal_entity_id = entity_id
        existing.confidence = confidence
        existing.resolution_status = status
        existing.resolution_notes = notes
        return existing
    entity = models.OpenFootballEntityMapping(
        entity_type=entity_type,
        original_name=original,
        normalized_name=normalized,
        internal_entity_id=entity_id,
        source_repository=repository,
        confidence=confidence,
        resolution_status=status,
        resolution_notes=notes,
    )
    db.add(entity)
    return entity


def _record_resolution_conflict(
    db: Session,
    *,
    entity_type: str,
    original: str,
    normalized: str,
    repository: str,
    candidates: Sequence[int],
    score: float,
) -> None:
    duplicate = db.scalar(
        select(models.EntityResolutionConflict).where(
            models.EntityResolutionConflict.entity_type == entity_type,
            models.EntityResolutionConflict.provider == SOURCE_NAME,
            models.EntityResolutionConflict.source_repository == repository,
            models.EntityResolutionConflict.normalized_name == normalized,
            models.EntityResolutionConflict.status == "pending",
        )
    )
    if duplicate is None:
        db.add(
            models.EntityResolutionConflict(
                entity_type=entity_type,
                provider=SOURCE_NAME,
                source_repository=repository,
                source_name=original,
                normalized_name=normalized,
                candidate_ids=list(dict.fromkeys(candidates)),
                best_score=score,
                status="pending",
                resolution_notes=(
                    "OpenFootball mapping requires manual review; "
                    f"source_repository={repository}"
                ),
            )
        )


def _resolve_competition(
    db: Session, name: str, repository: str
) -> models.Competition | None:
    normalized = normalize_entity_name(name)
    saved = _mapping(db, "competition", repository, normalized)
    if saved is not None:
        if saved.resolution_status in {"ambiguous", "manual_review"}:
            return None
        if saved.internal_entity_id is not None:
            entity = db.get(models.Competition, saved.internal_entity_id)
            if entity is not None and not entity.is_mock_data:
                return entity

    entities = list(
        db.scalars(select(models.Competition).where(models.Competition.is_mock_data.is_(False))).all()
    )
    candidates = [(entity.id, entity.name) for entity in entities]
    for alias, entity_id in db.execute(
        select(models.CompetitionAlias.alias, models.CompetitionAlias.competition_id)
        .join(models.Competition, models.Competition.id == models.CompetitionAlias.competition_id)
        .where(models.Competition.is_mock_data.is_(False))
    ):
        candidates.append((entity_id, alias))
    resolution = resolve_openfootball_name(name, candidates)
    if resolution.status == "ambiguous":
        _save_mapping(
            db,
            entity_type="competition",
            original=name,
            normalized=normalized,
            repository=repository,
            entity_id=None,
            confidence=resolution.confidence,
            status="ambiguous",
        )
        _record_resolution_conflict(
            db,
            entity_type="competition",
            original=name,
            normalized=normalized,
            repository=repository,
            candidates=resolution.candidate_ids,
            score=resolution.confidence,
        )
        return None
    if resolution.internal_entity_id is not None:
        entity = db.get(models.Competition, resolution.internal_entity_id)
        assert entity is not None
        status = resolution.status
    else:
        entity = models.Competition(
            external_id=f"of-{_hash_text(repository + '|' + normalized)[:20]}",
            name=name,
            data_source=SOURCE_NAME,
            source_updated_at=datetime.now(UTC),
            is_mock_data=False,
        )
        db.add(entity)
        db.flush()
        status = "created"
    _save_mapping(
        db,
        entity_type="competition",
        original=name,
        normalized=normalized,
        repository=repository,
        entity_id=entity.id,
        confidence=max(resolution.confidence, 1.0 if status == "created" else 0.0),
        status=status,
    )
    alias = db.scalar(
        select(models.CompetitionAlias).where(
            models.CompetitionAlias.provider == SOURCE_NAME,
            models.CompetitionAlias.normalized_alias == normalized,
        )
    )
    if alias is None:
        db.add(
            models.CompetitionAlias(
                competition_id=entity.id,
                provider=SOURCE_NAME,
                alias=name,
                normalized_alias=normalized,
                confidence=max(resolution.confidence, 1.0 if status == "created" else 0.0),
                review_status="approved",
            )
        )
    return entity


def _resolve_team(db: Session, name: str, repository: str) -> models.Team | None:
    normalized = normalize_openfootball_team(name)
    saved = _mapping(db, "team", repository, normalized)
    if saved is not None:
        if saved.resolution_status in {"ambiguous", "manual_review"}:
            return None
        if saved.internal_entity_id is not None:
            entity = db.get(models.Team, saved.internal_entity_id)
            if entity is not None and not entity.is_mock_data:
                return entity

    entities = list(db.scalars(select(models.Team).where(models.Team.is_mock_data.is_(False))).all())
    candidates = [(entity.id, normalize_openfootball_team(entity.name)) for entity in entities]
    for alias, entity_id in db.execute(
        select(models.TeamAlias.alias, models.TeamAlias.team_id)
        .join(models.Team, models.Team.id == models.TeamAlias.team_id)
        .where(models.Team.is_mock_data.is_(False))
    ):
        candidates.append((entity_id, normalize_openfootball_team(alias)))
    resolution = resolve_openfootball_name(normalized, candidates)
    if resolution.status == "ambiguous":
        _save_mapping(
            db,
            entity_type="team",
            original=name,
            normalized=normalized,
            repository=repository,
            entity_id=None,
            confidence=resolution.confidence,
            status="ambiguous",
        )
        _record_resolution_conflict(
            db,
            entity_type="team",
            original=name,
            normalized=normalized,
            repository=repository,
            candidates=resolution.candidate_ids,
            score=resolution.confidence,
        )
        return None
    if resolution.internal_entity_id is not None:
        entity = db.get(models.Team, resolution.internal_entity_id)
        assert entity is not None
        status = resolution.status
    else:
        entity = models.Team(
            external_id=f"of-{_hash_text(repository + '|' + normalized)[:20]}",
            name=name,
            data_source=SOURCE_NAME,
            source_updated_at=datetime.now(UTC),
            is_mock_data=False,
        )
        db.add(entity)
        db.flush()
        status = "created"
    _save_mapping(
        db,
        entity_type="team",
        original=name,
        normalized=normalized,
        repository=repository,
        entity_id=entity.id,
        confidence=max(resolution.confidence, 1.0 if status == "created" else 0.0),
        status=status,
    )
    alias = db.scalar(
        select(models.TeamAlias).where(
            models.TeamAlias.provider == SOURCE_NAME,
            models.TeamAlias.normalized_alias == normalized,
        )
    )
    if alias is None:
        db.add(
            models.TeamAlias(
                team_id=entity.id,
                provider=SOURCE_NAME,
                alias=name,
                normalized_alias=normalized,
                confidence=max(resolution.confidence, 1.0 if status == "created" else 0.0),
                review_status="approved",
            )
        )
    return entity


def _season(db: Session, competition: models.Competition, name: str) -> models.Season:
    canonical = _canonical_requested_season(name)
    if canonical is None:
        raise OpenFootballPersistenceError("Cada partido requiere una temporada con año")
    entity = db.scalar(
        select(models.Season).where(
            models.Season.competition_id == competition.id,
            models.Season.name == canonical,
            models.Season.is_mock_data.is_(False),
        )
    )
    if entity is not None:
        return entity
    equivalents = [
        candidate
        for candidate in db.scalars(
            select(models.Season).where(
                models.Season.competition_id == competition.id,
                models.Season.is_mock_data.is_(False),
            )
        )
        if normalize_openfootball_season(candidate.name) == canonical
    ]
    if len(equivalents) > 1:
        raise OpenFootballPersistenceError(
            f"Existen varias temporadas equivalentes a {canonical}; requieren consolidación manual"
        )
    if equivalents:
        equivalents[0].name = canonical
        db.flush()
        return equivalents[0]
    entity = models.Season(
        external_id=f"of-{competition.id}-{_hash_text(normalize_entity_name(canonical))[:16]}",
        competition_id=competition.id,
        name=canonical,
        data_source=SOURCE_NAME,
        source_updated_at=datetime.now(UTC),
        is_mock_data=False,
    )
    db.add(entity)
    db.flush()
    return entity


def _match_datetime(record: Mapping[str, Any]) -> tuple[datetime, dict[str, Any]]:
    raw_date = record.get("date")
    if not raw_date:
        raise OpenFootballPersistenceError("Falta una fecha ISO completa para el partido")
    try:
        match_day = date.fromisoformat(str(raw_date))
    except ValueError as exc:
        raise OpenFootballPersistenceError(f"Fecha inválida: {raw_date}") from exc
    kickoff = str(record.get("kickoff_time") or "").strip()
    if not kickoff:
        # Match.match_date is non-nullable in the normalized schema. Midnight is
        # only a date representation, explicitly marked as unknown—not kickoff.
        return datetime.combine(match_day, clock_time.min, tzinfo=UTC), {
            "kickoff_time_known": False,
            "date_precision": "day",
        }
    matched = re.fullmatch(
        r"(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)"
        r"(?:\s+(?:UTC|GMT)\s*(?P<sign>[+-])\s*(?P<offset_hour>\d{1,2})"
        r"(?::(?P<offset_minute>[0-5]\d))?)?",
        kickoff,
        re.IGNORECASE,
    )
    if matched is None:
        raise OpenFootballPersistenceError(f"Hora inválida: {kickoff}")
    parsed_time = clock_time(int(matched["hour"]), int(matched["minute"]))
    if matched["sign"] is None:
        return datetime.combine(match_day, parsed_time, tzinfo=UTC), {
            "kickoff_time_known": True,
            "timezone_known": False,
            "kickoff_time_source": kickoff,
        }
    offset_minutes = int(matched["offset_hour"]) * 60 + int(matched["offset_minute"] or 0)
    if offset_minutes > 14 * 60:
        raise OpenFootballPersistenceError(f"Zona horaria inválida: {kickoff}")
    if matched["sign"] == "-":
        offset_minutes *= -1
    source_zone = timezone(timedelta(minutes=offset_minutes))
    localized = datetime.combine(match_day, parsed_time, tzinfo=source_zone)
    return localized.astimezone(UTC), {
        "kickoff_time_known": True,
        "timezone_known": True,
        "kickoff_time_source": kickoff,
        "stored_timezone": "UTC",
    }


def _optional_score(record: Mapping[str, Any], key: str) -> int | None:
    value = record.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise OpenFootballPersistenceError(f"{key} debe ser entero")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise OpenFootballPersistenceError(f"{key} debe ser entero") from exc
    if not 0 <= parsed <= 99:
        raise OpenFootballPersistenceError(f"{key} está fuera de rango")
    return parsed


def _source_identity(record: Mapping[str, Any], repository: str) -> tuple[str, str]:
    normalized = {key: value for key, value in record.items() if key != "raw_payload"}
    content = json.dumps(
        {"normalized": normalized, "raw": record.get("raw_payload")},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    semantic = "|".join(
        str(record.get(key) or "")
        for key in (
            "source_file",
            "source_match_id",
            "competition",
            "season",
            "date",
            "home_team",
            "away_team",
            "round",
            "leg",
        )
    )
    source_record_id = _hash_text(f"{repository}|{semantic}|{content_hash}")
    return source_record_id, content_hash


def _existing_match(
    db: Session,
    *,
    competition_id: int,
    season_id: int,
    home_team_id: int,
    away_team_id: int,
    source_date: date,
) -> models.Match | None:
    # Offsets close to midnight can move the UTC calendar day. Search a narrow
    # window and compare the original source date retained in result_details.
    day_start = datetime.combine(source_date - timedelta(days=1), clock_time.min, tzinfo=UTC)
    day_end = day_start + timedelta(days=3)
    candidates = db.scalars(
        select(models.Match).where(
            models.Match.competition_id == competition_id,
            models.Match.season_id == season_id,
            models.Match.home_team_id == home_team_id,
            models.Match.away_team_id == away_team_id,
            models.Match.match_date >= day_start,
            models.Match.match_date < day_end,
            models.Match.is_mock_data.is_(False),
        )
    ).all()
    source_iso = source_date.isoformat()
    for candidate in candidates:
        details = candidate.result_details or {}
        if details.get("source_date") == source_iso:
            return candidate
        if "source_date" not in details and candidate.match_date.date() == source_date:
            return candidate
    return None


def _persist_record(
    db: Session,
    *,
    record: Mapping[str, Any],
    run: models.DataIngestionRun,
    repository: str,
) -> str:
    home_name = str(record.get("home_team") or "").strip()
    away_name = str(record.get("away_team") or "").strip()
    competition_name = str(record.get("competition") or "").strip()
    season_name = str(record.get("season") or "").strip()
    if not all((home_name, away_name, competition_name, season_name)):
        raise OpenFootballPersistenceError(
            "Cada partido requiere competition, season, home_team y away_team"
        )
    match_date, kickoff_metadata = _match_datetime(record)
    source_date = date.fromisoformat(str(record["date"]))
    source_record_id, content_hash = _source_identity(record, repository)
    duplicate = db.scalar(
        select(models.MatchSourceRecord).where(
            models.MatchSourceRecord.source_name == SOURCE_NAME,
            models.MatchSourceRecord.source_repository == repository,
            models.MatchSourceRecord.source_record_id == source_record_id,
        )
    )
    if duplicate is not None:
        return "conflict" if duplicate.conflict_status == "conflict" else "duplicate"

    competition = _resolve_competition(db, competition_name, repository)
    home = _resolve_team(db, home_name, repository)
    away = _resolve_team(db, away_name, repository)
    if competition is None or home is None or away is None:
        return "resolution_conflict"
    if home.id == away.id:
        raise OpenFootballPersistenceError("Local y visitante resuelven al mismo equipo")
    season = _season(db, competition, season_name)

    fulltime_home = _optional_score(record, "fulltime_home_goals")
    fulltime_away = _optional_score(record, "fulltime_away_goals")
    extra_home = _optional_score(record, "extra_time_home_goals")
    extra_away = _optional_score(record, "extra_time_away_goals")
    # Core score columns feed 90-minute models. Keep ET and shoot-outs only in
    # result_details/provenance; use ET here solely for legacy records lacking FT.
    final_home = fulltime_home if fulltime_home is not None else extra_home
    final_away = fulltime_away if fulltime_away is not None else extra_away
    halftime_home = _optional_score(record, "halftime_home_goals")
    halftime_away = _optional_score(record, "halftime_away_goals")
    status = str(record.get("status") or ("finished" if final_home is not None else "scheduled"))
    incoming_details = {
        key: record.get(key)
        for key in (
            "fulltime_home_goals",
            "fulltime_away_goals",
            "extra_time_home_goals",
            "extra_time_away_goals",
            "penalty_home_goals",
            "penalty_away_goals",
            "aggregate_home_goals",
            "aggregate_away_goals",
            "leg",
            "group",
            "attendance",
            "kickoff_time",
        )
        if record.get(key) is not None
    }
    incoming_details.update(kickoff_metadata)
    incoming_details["source_date"] = source_date.isoformat()

    match = _existing_match(
        db,
        competition_id=competition.id,
        season_id=season.id,
        home_team_id=home.id,
        away_team_id=away.id,
        source_date=source_date,
    )
    matched_existing = match is not None
    semantic_fingerprint = _hash_text(
        f"{competition.id}|{season.id}|{source_date.isoformat()}|{home.id}|{away.id}"
    )
    if match is None:
        match = models.Match(
            external_id=semantic_fingerprint,
            competition_id=competition.id,
            season_id=season.id,
            home_team_id=home.id,
            away_team_id=away.id,
            match_date=match_date,
            venue=str(record.get("venue"))[:180] if record.get("venue") else None,
            status=status[:30],
            home_score=final_home,
            away_score=final_away,
            halftime_home_score=halftime_home,
            halftime_away_score=halftime_away,
            ingestion_run_id=run.id,
            round_name=str(record.get("round"))[:100] if record.get("round") else None,
            notes=str(record.get("notes"))[:2000] if record.get("notes") else None,
            result_details=incoming_details or None,
            data_source=SOURCE_NAME,
            source_updated_at=datetime.now(UTC),
            is_mock_data=False,
        )
        db.add(match)
        db.flush()

    conflicts: list[dict[str, Any]] = []
    core_final_conflict = False
    for home_field, away_field, incoming_home, incoming_away in (
        ("home_score", "away_score", final_home, final_away),
        (
            "halftime_home_score",
            "halftime_away_score",
            halftime_home,
            halftime_away,
        ),
    ):
        if incoming_home is None and incoming_away is None:
            continue
        if (incoming_home is None) != (incoming_away is None):
            raise OpenFootballPersistenceError(
                f"{home_field} y {away_field} deben importarse juntos"
            )
        current_home = getattr(match, home_field)
        current_away = getattr(match, away_field)
        pair_conflict = any(
            current is not None and current != incoming
            for current, incoming in (
                (current_home, incoming_home),
                (current_away, incoming_away),
            )
        )
        if pair_conflict:
            conflicts.extend(
                (
                    {
                        "field": home_field,
                        "existing": current_home,
                        "incoming": incoming_home,
                        "resolution_group": "final_score"
                        if home_field == "home_score"
                        else "halftime_score",
                    },
                    {
                        "field": away_field,
                        "existing": current_away,
                        "incoming": incoming_away,
                        "resolution_group": "final_score"
                        if home_field == "home_score"
                        else "halftime_score",
                    },
                )
            )
            if home_field == "home_score":
                core_final_conflict = True
        else:
            if current_home is None:
                setattr(match, home_field, incoming_home)
            if current_away is None:
                setattr(match, away_field, incoming_away)

    for field, incoming in {
        "venue": str(record.get("venue"))[:180] if record.get("venue") else None,
        "round_name": str(record.get("round"))[:100] if record.get("round") else None,
    }.items():
        if incoming is None:
            continue
        current = getattr(match, field)
        if current is None:
            setattr(match, field, incoming)
        elif current != incoming:
            conflicts.append({"field": field, "existing": current, "incoming": incoming})

    details = dict(match.result_details or {})
    processed_detail_fields: set[str] = set()
    canonical_detail_pair = (
        ("fulltime_home_goals", "fulltime_away_goals")
        if fulltime_home is not None
        else ("extra_time_home_goals", "extra_time_away_goals")
    )
    for home_field, away_field in (
        ("fulltime_home_goals", "fulltime_away_goals"),
        ("extra_time_home_goals", "extra_time_away_goals"),
        ("penalty_home_goals", "penalty_away_goals"),
        ("aggregate_home_goals", "aggregate_away_goals"),
    ):
        incoming_home = incoming_details.get(home_field)
        incoming_away = incoming_details.get(away_field)
        if incoming_home is None and incoming_away is None:
            continue
        processed_detail_fields.update((home_field, away_field))
        if (incoming_home is None) != (incoming_away is None):
            raise OpenFootballPersistenceError(
                f"result_details.{home_field} y result_details.{away_field} deben importarse juntos"
            )
        current_home = details.get(home_field)
        current_away = details.get(away_field)
        pair_conflict = (
            core_final_conflict and (home_field, away_field) == canonical_detail_pair
        ) or any(
            current is not None and current != incoming
            for current, incoming in (
                (current_home, incoming_home),
                (current_away, incoming_away),
            )
        )
        if pair_conflict:
            group = (
                "final_score"
                if (home_field, away_field) == canonical_detail_pair
                else home_field.removesuffix("_home_goals")
            )
            conflicts.extend(
                (
                    {
                        "field": f"result_details.{home_field}",
                        "existing": current_home,
                        "incoming": incoming_home,
                        "resolution_group": group,
                    },
                    {
                        "field": f"result_details.{away_field}",
                        "existing": current_away,
                        "incoming": incoming_away,
                        "resolution_group": group,
                    },
                )
            )
        else:
            if current_home is None:
                details[home_field] = incoming_home
            if current_away is None:
                details[away_field] = incoming_away

    for field, incoming in incoming_details.items():
        if field in processed_detail_fields:
            continue
        if field not in details or details[field] is None:
            details[field] = incoming
        elif details[field] != incoming:
            conflicts.append(
                {"field": f"result_details.{field}", "existing": details[field], "incoming": incoming}
            )
    match.result_details = details or None
    current_status = match.status
    if status not in {"unknown", current_status}:
        if current_status in {"unknown", "scheduled"}:
            match.status = status
        elif status == "finished" and current_status in {
            "postponed",
            "cancelled",
            "abandoned",
        }:
            match.status = "finished"
        elif status != "scheduled":
            conflicts.append(
                {"field": "status", "existing": current_status, "incoming": status}
            )

    normalized_payload = {key: value for key, value in record.items() if key != "raw_payload"}
    provenance_label = f"{SOURCE_NAME}:{repository}:{record.get('source_file') or ''}"
    field_provenance = {
        key: provenance_label
        for key, value in normalized_payload.items()
        if value not in (None, "", [], {})
    }
    db.add(
        models.MatchSourceRecord(
            match_id=match.id,
            ingestion_run_id=run.id,
            source_name=SOURCE_NAME,
            source_repository=repository[:120],
            source_record_id=source_record_id,
            source_file=str(record.get("source_file"))[:500] if record.get("source_file") else None,
            raw_payload=_bounded_json(record.get("raw_payload")),
            normalized_payload=_bounded_json(normalized_payload),
            field_provenance=field_provenance,
            content_hash=content_hash,
            conflict_status="conflict" if conflicts else "none",
            conflict_details={"fields": conflicts} if conflicts else None,
        )
    )
    if conflicts:
        return "conflict"
    return "matched_duplicate" if matched_existing else "persisted"


def _refresh_coverage(db: Session, repository: str) -> list[dict[str, Any]]:
    rows = list(
        db.execute(
            select(models.MatchSourceRecord, models.Match, models.Season)
            .join(models.Match, models.Match.id == models.MatchSourceRecord.match_id)
            .join(models.Season, models.Season.id == models.Match.season_id)
            .where(
                models.MatchSourceRecord.source_name == SOURCE_NAME,
                models.MatchSourceRecord.source_repository == repository,
                models.Match.is_mock_data.is_(False),
            )
        ).all()
    )
    grouped: dict[int, list[tuple[models.MatchSourceRecord, models.Match, models.Season]]] = defaultdict(list)
    for source_record, match, season in rows:
        grouped[match.competition_id].append((source_record, match, season))
    output: list[dict[str, Any]] = []
    now = datetime.now(UTC)
    for competition_id, records in grouped.items():
        by_match: dict[int, models.Match] = {match.id: match for _, match, _ in records}
        matches = list(by_match.values())
        seasons = sorted({season.name for _, _, season in records})
        fields = sorted(
            {
                key
                for source_record, _, _ in records
                for key, value in (source_record.normalized_payload or {}).items()
                if value not in (None, "", [], {})
            }
        )
        coverage = db.scalar(
            select(models.CompetitionSourceCoverage).where(
                models.CompetitionSourceCoverage.competition_id == competition_id,
                models.CompetitionSourceCoverage.source_name == SOURCE_NAME,
                models.CompetitionSourceCoverage.source_repository == repository,
            )
        )
        if coverage is None:
            coverage = models.CompetitionSourceCoverage(
                competition_id=competition_id,
                source_name=SOURCE_NAME,
                source_repository=repository,
            )
            db.add(coverage)
        dates = [match.match_date for match in matches]
        coverage.first_match_date = min(dates) if dates else None
        coverage.last_match_date = max(dates) if dates else None
        coverage.total_matches = len(matches)
        coverage.finished_matches = sum(match.status == "finished" for match in matches)
        coverage.scheduled_matches = sum(match.status == "scheduled" for match in matches)
        coverage.seasons_available = seasons
        coverage.fields_available = fields
        coverage.last_imported_at = now
        db.flush()
        competition = db.get(models.Competition, competition_id)
        output.append(
            {
                "competition": competition.name if competition else str(competition_id),
                "competition_id": competition_id,
                "source_repository": repository,
                "first_match_date": coverage.first_match_date.isoformat() if coverage.first_match_date else None,
                "last_match_date": coverage.last_match_date.isoformat() if coverage.last_match_date else None,
                "total_matches": coverage.total_matches,
                "finished_matches": coverage.finished_matches,
                "scheduled_matches": coverage.scheduled_matches,
                "seasons_available": seasons,
                "fields_available": fields,
                "last_imported_at": now.isoformat(),
            }
        )
    return sorted(output, key=lambda item: str(item["competition"]))


def confirm_openfootball_import(
    db: Session,
    run: models.DataIngestionRun,
    *,
    force: bool = False,
) -> models.DataIngestionRun:
    """Persist a preview. Repeated confirmation is a no-op with the same run id."""

    if run.source != SOURCE_NAME:
        raise OpenFootballPersistenceError("La importación no pertenece a OpenFootball")
    if run.status in {"completed", "completed_with_errors"} and not force:
        return run
    if not run.stored_path:
        raise OpenFootballPersistenceError("La importación no conserva una ruta reprocesable")
    stored = Path(run.stored_path)
    if not stored.exists():
        raise OpenFootballPersistenceError("Los archivos originales ya no existen")

    options = dict(run.import_options or {})
    started = time.monotonic()
    run.status = "running"
    run.started_at = datetime.now(UTC)
    run.completed_at = None
    db.commit()
    try:
        result = import_openfootball_repository(
            stored,
            competition=options.get("competition"),
            season=_canonical_requested_season(options.get("season")),
        )
        _ensure_result_identity(result)
        repository = result.detection.source_repository[:120]
        catalog_bundle = discover_openfootball_catalogs(
            stored, repository=result.detection.source_repository
        )
        catalog_result = persist_openfootball_catalogs(db, catalog_bundle)
        duplicates = catalog_result.duplicates
        conflicts = catalog_result.conflicts
        rejected = 0
        errors = [error.to_dict() for error in result.errors]
        for index, match in enumerate(result.matches, start=1):
            record = serialize_openfootball_match(match, include_raw=True)
            try:
                outcome = _persist_record(
                    db,
                    record=record,
                    run=run,
                    repository=repository,
                )
            except Exception as exc:
                rejected += 1
                errors.append(
                    {
                        "source_file": str(record.get("source_file") or ""),
                        "line": record.get("source_line"),
                        "row": index,
                        "code": "record_error",
                        "message": str(exc),
                    }
                )
                continue
            if outcome in {"duplicate", "matched_duplicate"}:
                duplicates += 1
            elif outcome in {"conflict", "resolution_conflict"}:
                conflicts += 1
                if outcome == "resolution_conflict":
                    rejected += 1
                    errors.append(
                        {
                            "source_file": str(record.get("source_file") or ""),
                            "line": record.get("source_line"),
                            "row": index,
                            "code": "entity_resolution_conflict",
                            "message": "Una entidad requiere resolución manual",
                        }
                    )
        db.flush()
        quality = _refresh_coverage(db, repository)
        metrics = dict(result.metrics)
        metrics.update(
            _catalog_metrics(
                catalog_bundle, imported=catalog_result.records_imported
            )
        )
        metrics.update(
            {
                "duplicates": duplicates,
                "conflicts": conflicts,
                "errors": len(errors),
            }
        )
        run.import_metrics = metrics
        run.file_hash = result.detection.content_hash
        run.downloaded_records = len(result.matches) + catalog_bundle.records_found
        run.valid_records = (
            max(0, len(result.matches) - rejected)
            + catalog_result.records_imported
        )
        run.rejected_records = rejected + len(result.errors)
        run.errors = errors
        run.status = "completed_with_errors" if errors or conflicts else "completed"
        run.completed_at = datetime.now(UTC)
        run.duration_seconds = round(time.monotonic() - started, 6)
        preview = _result_preview_payload(
            result, preview_limit=int(options.get("preview_limit") or 50)
        )
        preview["metrics"] = metrics
        preview["catalogs"] = _catalog_summary(catalog_bundle)
        preview["quality_by_competition"] = quality
        preview["errors"] = errors
        preview["warnings"] = list(
            dict.fromkeys([*result.warnings, *catalog_result.warnings])
        )
        preview["import_id"] = run.id
        preview["status"] = run.status
        preview["started_at"] = run.started_at.isoformat()
        preview["completed_at"] = run.completed_at.isoformat()
        run.preview_payload = preview
        db.commit()
        db.refresh(run)
        return run
    except Exception as exc:
        db.rollback()
        current = db.get(models.DataIngestionRun, run.id)
        if current is not None:
            current.status = "failed"
            current.completed_at = datetime.now(UTC)
            current.duration_seconds = round(time.monotonic() - started, 6)
            current.errors = [{"source_file": "", "code": "import_error", "message": str(exc)}]
            db.commit()
        if isinstance(exc, (OpenFootballPersistenceError, OpenFootballImportError)):
            raise
        raise OpenFootballPersistenceError(str(exc)) from exc


def reprocess_openfootball_import(db: Session, run: models.DataIngestionRun) -> models.DataIngestionRun:
    return confirm_openfootball_import(db, run, force=True)


DATA_CATEGORIES = [
    ("resultados", lambda db, comp_id: _has_field(db, models.Match.home_score, comp_id)),
    ("alineaciones", lambda db, comp_id: _count_relation(db, models.Lineup, comp_id) > 0),
    ("eventos", lambda db, comp_id: _count_relation(db, models.MatchEvent, comp_id) > 0),
    ("tarjetas", lambda db, comp_id: _count_relation(db, models.MatchEvent, comp_id, models.MatchEvent.event_type == "Card") > 0),
    ("árbitros", lambda db, comp_id: _count_relation(db, models.Match, comp_id, models.Match.referee_id.is_not(None)) > 0),
    ("estadísticas avanzadas", lambda db, comp_id: _count_relation(db, models.TeamMatchStatistics, comp_id, models.TeamMatchStatistics.xg.is_not(None)) > 0),
]


def _has_field(db: Session, column: Any, competition_id: int) -> bool:
    return db.scalar(
        select(column).where(
            models.Match.competition_id == competition_id,
            column.is_not(None),
        ).limit(1)
    ) is not None


def _count_relation(
    db: Session, model: type, competition_id: int, *extra_filters: Any
) -> int:
    if model is models.Match:
        stmt = select(model).where(
            models.Match.competition_id == competition_id
        )
    else:
        stmt = select(model).join(
            models.Match, model.match_id == models.Match.id
        ).where(models.Match.competition_id == competition_id)
    for f in extra_filters:
        stmt = stmt.where(f)
    return 1 if db.scalar(stmt.limit(1)) is not None else 0


def list_openfootball_quality(db: Session) -> list[dict[str, Any]]:
    rows = list(
        db.execute(
            select(models.CompetitionSourceCoverage, models.Competition.name)
            .join(models.Competition, models.Competition.id == models.CompetitionSourceCoverage.competition_id)
            .where(models.CompetitionSourceCoverage.source_name == SOURCE_NAME)
            .order_by(models.Competition.name, models.CompetitionSourceCoverage.source_repository)
        ).all()
    )
    result: list[dict[str, Any]] = []
    for row, competition_name in rows:
        data_categories: dict[str, bool] = {}
        for category_name, check_fn in DATA_CATEGORIES:
            try:
                data_categories[category_name] = check_fn(db, row.competition_id)
            except Exception:
                data_categories[category_name] = False
        missing = [name for name, present in data_categories.items() if not present]
        coverage_status = "complete" if not missing else "incomplete"
        result.append(
            {
                "competition": competition_name,
                "competition_id": row.competition_id,
                "source_repository": row.source_repository,
                "first_match_date": row.first_match_date.isoformat() if row.first_match_date else None,
                "last_match_date": row.last_match_date.isoformat() if row.last_match_date else None,
                "total_matches": row.total_matches,
                "finished_matches": row.finished_matches,
                "scheduled_matches": row.scheduled_matches,
                "seasons_available": list(row.seasons_available or []),
                "fields_available": list(row.fields_available or []),
                "last_imported_at": row.last_imported_at.isoformat(),
                "data_categories": data_categories,
                "coverage_status": coverage_status,
                "missing_categories": missing,
            }
        )
    return result


def delete_openfootball_import(
    db: Session,
    run: models.DataIngestionRun,
    *,
    settings: Settings | None = None,
) -> None:
    settings = settings or get_settings()
    stored = Path(run.stored_path) if run.stored_path else None
    db.delete(run)
    db.commit()
    if stored is None:
        return
    managed_root = (settings.import_dir / SOURCE_NAME).resolve()
    try:
        resolved = stored.resolve()
        resolved.relative_to(managed_root)
    except (OSError, ValueError):
        return
    run_root = managed_root / run.id
    shutil.rmtree(run_root, ignore_errors=True)


__all__ = [
    "MAX_PREVIEW_MATCHES",
    "OpenFootballPersistenceError",
    "confirm_openfootball_import",
    "delete_openfootball_import",
    "list_openfootball_quality",
    "preview_openfootball_path",
    "preview_openfootball_uploads",
    "reprocess_openfootball_import",
    "_run_envelope",
]
