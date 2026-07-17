"""Discovery and persistence for local OpenFootball identity catalogs."""

from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.data_sources.openfootball.catalog_parser import (
    CATALOG_KINDS,
    CatalogKind,
    OpenFootballCatalog,
    OpenFootballClub,
    OpenFootballLeague,
    OpenFootballPlayer,
    detect_openfootball_catalog_kind,
    is_openfootball_catalog_auxiliary,
    parse_openfootball_catalog_data,
    sniff_openfootball_catalog_kind,
)
from app.data_sources.openfootball.importer import (
    IGNORED_PARTS,
    _directory_files,
    _zip_infos,
)
from app.data_sources.openfootball.validators import normalize_openfootball_team
from app.services.entity_resolution import normalize_entity_name


SOURCE_NAME = "openfootball"


@dataclass(frozen=True, slots=True)
class OpenFootballCatalogBundle:
    catalogs: tuple[OpenFootballCatalog, ...]
    files_scanned: int
    records_found: int
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OpenFootballCatalogPersistence:
    records_found: int
    records_imported: int
    duplicates: int
    conflicts: int
    leagues_imported: int
    clubs_imported: int
    players_imported: int
    warnings: tuple[str, ...]


def _kind_hint(name: str, repository: str | None) -> CatalogKind | None:
    return detect_openfootball_catalog_kind(name, repository)


def _decode(payload: bytes) -> str:
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return payload.decode("cp1252")


def discover_openfootball_catalogs(
    path: str | Path, *, repository: str | None = None
) -> OpenFootballCatalogBundle:
    """Read only catalog files from a folder, ZIP or TXT path.

    Dataset type is inferred from an explicit leagues/clubs/players path
    component.  Match files are never guessed into a catalog.
    """

    source = Path(path).expanduser().resolve(strict=True)
    parsed: list[OpenFootballCatalog] = []
    warnings: list[str] = []
    files_scanned = 0

    def consume(name: str, payload: bytes, inherited: str | None = None) -> None:
        nonlocal files_scanned
        if PurePosixPath(name).suffix.casefold() != ".txt":
            return
        if is_openfootball_catalog_auxiliary(name):
            warnings.append(
                f"{name}: ignored auxiliary OpenFootball catalog file; no canonical entities created"
            )
            return
        kind = _kind_hint(name, inherited or repository)
        text = _decode(payload)
        kind = kind or sniff_openfootball_catalog_kind(text)
        if kind is None:
            return
        files_scanned += 1
        catalog = parse_openfootball_catalog_data(text, kind, source_file=name)
        for record in catalog.records:
            record.source_repository = kind
        parsed.append(catalog)
        warnings.extend(catalog.warnings)

    if source.is_dir():
        # Only the actual root name may become a repository-wide fallback.
        # A mixed openfootball/ tree must rely on its concrete subdirectories.
        inherited = _kind_hint(source.name, None)
        for file in _directory_files(source):
            if file.suffix.casefold() == ".txt":
                consume(file.relative_to(source).as_posix(), file.read_bytes(), inherited)
    elif source.suffix.casefold() == ".zip":
        inherited = _kind_hint(source.stem, None)
        with zipfile.ZipFile(source) as archive:
            for info in _zip_infos(source):
                member = PurePosixPath(info.filename)
                if any(part.casefold() in IGNORED_PARTS for part in member.parts):
                    continue
                consume(member.as_posix(), archive.read(info), inherited)
    elif source.suffix.casefold() == ".txt":
        consume(source.name, source.read_bytes(), repository or _kind_hint(source.parent.name, None))

    return OpenFootballCatalogBundle(
        catalogs=tuple(parsed),
        files_scanned=files_scanned,
        records_found=sum(len(catalog.records) for catalog in parsed),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _normalizer(entity_type: str, value: str) -> str:
    return normalize_openfootball_team(value) if entity_type == "team" else normalize_entity_name(value)


def _mapping(
    db: Session, *, entity_type: str, repository: str, normalized: str
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
    repository: str,
    original: str,
    entity_id: int | None,
    status: str,
    notes: str | None = None,
) -> None:
    normalized = _normalizer(entity_type, original)
    mapping = _mapping(
        db, entity_type=entity_type, repository=repository, normalized=normalized
    )
    if mapping is None:
        mapping = models.OpenFootballEntityMapping(
            entity_type=entity_type,
            source_repository=repository,
            original_name=original,
            normalized_name=normalized,
        )
        db.add(mapping)
    elif mapping.manually_verified or mapping.resolution_status.casefold().startswith(
        "manual"
    ):
        # A catalog reimport must never undo a human decision.  In particular,
        # do not clear the selected entity, verification bit, notes or status
        # when the same source name is encountered again.
        return
    mapping.internal_entity_id = entity_id
    mapping.confidence = 1.0 if entity_id is not None else 0.0
    mapping.manually_verified = False
    mapping.resolution_status = status
    mapping.resolution_notes = notes
    db.flush()


def _record_ambiguity(
    db: Session,
    *,
    entity_type: str,
    repository: str,
    original: str,
    normalized: str,
    candidate_ids: Sequence[int],
) -> None:
    mapping = _mapping(
        db,
        entity_type=entity_type,
        repository=repository,
        normalized=normalized,
    )
    if mapping is not None and (
        mapping.manually_verified
        or mapping.resolution_status.casefold().startswith("manual")
    ):
        # The ambiguity already has a human-approved answer.  Reimports may
        # refresh catalog metadata, but must not reopen the resolved conflict.
        return
    existing = db.scalar(
        select(models.EntityResolutionConflict).where(
            models.EntityResolutionConflict.entity_type == entity_type,
            models.EntityResolutionConflict.provider == SOURCE_NAME,
            models.EntityResolutionConflict.normalized_name == normalized,
            models.EntityResolutionConflict.status == "pending",
        )
    )
    if existing is None:
        db.add(
            models.EntityResolutionConflict(
                entity_type=entity_type,
                provider=SOURCE_NAME,
                source_repository=repository,
                source_name=original,
                normalized_name=normalized,
                candidate_ids=list(dict.fromkeys(candidate_ids)),
                best_score=1.0,
                status="pending",
                resolution_notes="OpenFootball catalog identity is ambiguous",
            )
        )
        db.flush()


def _competition_candidates(db: Session, names: Iterable[str], country: str | None) -> list[int]:
    normalized = {_normalizer("competition", value) for value in names if value}
    candidates: set[int] = set()
    for entity in db.scalars(
        select(models.Competition).where(models.Competition.is_mock_data.is_(False))
    ):
        if _normalizer("competition", entity.name) in normalized:
            if country is None or entity.country is None or normalize_entity_name(entity.country) == normalize_entity_name(country):
                candidates.add(entity.id)
    for alias, entity_id, entity_country in db.execute(
        select(models.CompetitionAlias.alias, models.CompetitionAlias.competition_id, models.Competition.country)
        .join(models.Competition, models.Competition.id == models.CompetitionAlias.competition_id)
        .where(models.Competition.is_mock_data.is_(False))
    ):
        if _normalizer("competition", alias) in normalized:
            if country is None or entity_country is None or normalize_entity_name(entity_country) == normalize_entity_name(country):
                candidates.add(entity_id)
    return sorted(candidates)


def _team_candidates(db: Session, names: Iterable[str], country: str | None) -> list[int]:
    normalized = {_normalizer("team", value) for value in names if value}
    candidates: set[int] = set()
    for entity in db.scalars(select(models.Team).where(models.Team.is_mock_data.is_(False))):
        if _normalizer("team", entity.name) in normalized:
            if country is None or entity.country is None or normalize_entity_name(entity.country) == normalize_entity_name(country):
                candidates.add(entity.id)
    for alias, entity_id, entity_country in db.execute(
        select(models.TeamAlias.alias, models.TeamAlias.team_id, models.Team.country)
        .join(models.Team, models.Team.id == models.TeamAlias.team_id)
        .where(models.Team.is_mock_data.is_(False))
    ):
        if _normalizer("team", alias) in normalized:
            if country is None or entity_country is None or normalize_entity_name(entity_country) == normalize_entity_name(country):
                candidates.add(entity_id)
    return sorted(candidates)


def _player_candidates(
    db: Session,
    names: Iterable[str],
    nationality: str | None,
    birth_date: date | None = None,
) -> list[int]:
    normalized = {_normalizer("player", value) for value in names if value}
    candidates: set[int] = set()
    for entity in db.scalars(select(models.Player).where(models.Player.is_mock_data.is_(False))):
        if _normalizer("player", entity.name) in normalized:
            if nationality is None or entity.nationality is None or normalize_entity_name(entity.nationality) == normalize_entity_name(nationality):
                candidates.add(entity.id)
    for alias, entity_id, entity_nationality in db.execute(
        select(models.PlayerAlias.alias, models.PlayerAlias.player_id, models.Player.nationality)
        .join(models.Player, models.Player.id == models.PlayerAlias.player_id)
        .where(models.Player.is_mock_data.is_(False))
    ):
        if _normalizer("player", alias) in normalized:
            if nationality is None or entity_nationality is None or normalize_entity_name(entity_nationality) == normalize_entity_name(nationality):
                candidates.add(entity_id)
    candidate_ids = sorted(candidates)
    if birth_date is None or not candidate_ids:
        return candidate_ids

    candidate_entities = [db.get(models.Player, candidate_id) for candidate_id in candidate_ids]
    exact = [
        entity.id
        for entity in candidate_entities
        if entity is not None and entity.birth_date == birth_date
    ]
    if exact:
        return sorted(exact)

    unknown = [
        entity.id
        for entity in candidate_entities
        if entity is not None and entity.birth_date is None
    ]
    if len(unknown) == 1 and len(candidate_ids) == 1:
        # A single legacy identity with no DOB can be enriched safely.
        return unknown
    if unknown:
        # At least one candidate lacks the discriminator, so selecting one or
        # creating another identity would be a guess.  Let the caller record a
        # conservative ambiguity.
        return candidate_ids

    # Every matching name has a known, different DOB: this is an unequivocal
    # homonym and must become a distinct player rather than a silent merge.
    return []


def _catalog_external_id(repository: str, *parts: str | None) -> str:
    identity = "|".join(value or "" for value in parts)
    return f"of-{repository}-{hashlib.sha256(identity.encode()).hexdigest()[:20]}"


def _merge_metadata(entity: Any, values: dict[str, Any]) -> None:
    metadata = dict(getattr(entity, "catalog_metadata", None) or {})
    for key, value in values.items():
        if value not in (None, "", [], {}):
            metadata.setdefault(key, value)
    entity.catalog_metadata = metadata or None


def _add_alias(
    db: Session,
    *,
    entity_type: str,
    entity_id: int,
    value: str,
) -> int | None:
    normalized = _normalizer(entity_type, value)
    if entity_type == "competition":
        alias_model = models.CompetitionAlias
        id_name = "competition_id"
    elif entity_type == "team":
        alias_model = models.TeamAlias
        id_name = "team_id"
    else:
        alias_model = models.PlayerAlias
        id_name = "player_id"
    existing = db.scalar(
        select(alias_model).where(
            alias_model.provider == SOURCE_NAME,
            alias_model.normalized_alias == normalized,
        )
    )
    if existing is not None:
        existing_id = int(getattr(existing, id_name))
        return None if existing_id == entity_id else existing_id
    db.add(
        alias_model(
            **{id_name: entity_id},
            provider=SOURCE_NAME,
            alias=value,
            normalized_alias=normalized,
            confidence=1.0,
            review_status="approved",
        )
    )
    db.flush()
    return None


def _persist_league(
    db: Session, record: OpenFootballLeague
) -> tuple[bool, bool, int, list[str]]:
    names = [record.name, *record.aliases]
    candidates = _competition_candidates(db, names, record.country)
    if len(candidates) > 1:
        normalized = _normalizer("competition", record.name)
        _record_ambiguity(
            db,
            entity_type="competition",
            repository=record.source_repository,
            original=record.name,
            normalized=normalized,
            candidate_ids=candidates,
        )
        _save_mapping(
            db,
            entity_type="competition",
            repository=record.source_repository,
            original=record.name,
            entity_id=None,
            status="ambiguous",
            notes=f"Candidates: {candidates}",
        )
        return False, True, 1, []
    duplicate = bool(candidates)
    entity = db.get(models.Competition, candidates[0]) if candidates else None
    if entity is None:
        entity = models.Competition(
            external_id=_catalog_external_id(
                record.source_repository, record.country, record.code, record.name
            ),
            name=record.name,
            country=record.country,
            data_source=SOURCE_NAME,
            source_updated_at=datetime.now(UTC),
            is_mock_data=False,
        )
        db.add(entity)
        db.flush()
    warnings: list[str] = []
    alias_conflicts = 0
    for field, value in (
        ("country", record.country),
        ("catalog_code", record.code),
        ("competition_level", record.division),
        ("competition_type", record.competition_type),
    ):
        if value in (None, ""):
            continue
        current = getattr(entity, field, None)
        if current is None:
            setattr(entity, field, value)
        elif current != value:
            warnings.append(f"{record.name}: kept existing {field}={current!r}; catalog={value!r}")
    _merge_metadata(
        entity,
        {"source_file": record.source_file, "source_repository": record.source_repository},
    )
    for value in names:
        conflicting_id = _add_alias(
            db, entity_type="competition", entity_id=entity.id, value=value
        )
        if conflicting_id is not None:
            alias_conflicts += 1
            warnings.append(f"{record.name}: ambiguous alias {value!r} was not reassigned")
            normalized = _normalizer("competition", value)
            _record_ambiguity(
                db,
                entity_type="competition",
                repository=record.source_repository,
                original=value,
                normalized=normalized,
                candidate_ids=[entity.id, conflicting_id],
            )
            _save_mapping(
                db,
                entity_type="competition",
                repository=record.source_repository,
                original=value,
                entity_id=None,
                status="ambiguous",
                notes=f"Candidates: {[entity.id, conflicting_id]}",
            )
        else:
            _save_mapping(
                db,
                entity_type="competition",
                repository=record.source_repository,
                original=value,
                entity_id=entity.id,
                status="catalog",
            )
    return True, duplicate, alias_conflicts, warnings


def _persist_club(
    db: Session, record: OpenFootballClub
) -> tuple[bool, bool, int, list[str]]:
    names = [record.name, *record.aliases]
    candidates = _team_candidates(db, names, record.country)
    if len(candidates) > 1:
        normalized = _normalizer("team", record.name)
        _record_ambiguity(
            db,
            entity_type="team",
            repository=record.source_repository,
            original=record.name,
            normalized=normalized,
            candidate_ids=candidates,
        )
        _save_mapping(
            db,
            entity_type="team",
            repository=record.source_repository,
            original=record.name,
            entity_id=None,
            status="ambiguous",
            notes=f"Candidates: {candidates}",
        )
        return False, True, 1, []
    duplicate = bool(candidates)
    entity = db.get(models.Team, candidates[0]) if candidates else None
    if entity is None:
        entity = models.Team(
            external_id=_catalog_external_id(
                record.source_repository, record.country, record.name
            ),
            name=record.name,
            country=record.country,
            data_source=SOURCE_NAME,
            source_updated_at=datetime.now(UTC),
            is_mock_data=False,
        )
        db.add(entity)
        db.flush()
    warnings: list[str] = []
    alias_conflicts = 0
    for field, value in (
        ("country", record.country),
        ("stadium", record.stadium),
        ("city", record.city),
        ("founded_year", record.founded_year),
    ):
        current = getattr(entity, field, None)
        if current is None and value is not None:
            setattr(entity, field, value)
        elif value is not None and current != value:
            warnings.append(f"{record.name}: kept existing {field}={current!r}; catalog={value!r}")
    _merge_metadata(
        entity,
        {"source_file": record.source_file, "source_repository": record.source_repository},
    )
    for value in names:
        conflicting_id = _add_alias(db, entity_type="team", entity_id=entity.id, value=value)
        if conflicting_id is not None:
            alias_conflicts += 1
            warnings.append(f"{record.name}: ambiguous alias {value!r} was not reassigned")
            normalized = _normalizer("team", value)
            _record_ambiguity(
                db,
                entity_type="team",
                repository=record.source_repository,
                original=value,
                normalized=normalized,
                candidate_ids=[entity.id, conflicting_id],
            )
            _save_mapping(
                db,
                entity_type="team",
                repository=record.source_repository,
                original=value,
                entity_id=None,
                status="ambiguous",
                notes=f"Candidates: {[entity.id, conflicting_id]}",
            )
        else:
            _save_mapping(
                db,
                entity_type="team",
                repository=record.source_repository,
                original=value,
                entity_id=entity.id,
                status="catalog",
            )
    return True, duplicate, alias_conflicts, warnings


def _persist_player(
    db: Session, record: OpenFootballPlayer
) -> tuple[bool, bool, int, list[str]]:
    names = [record.name, *record.aliases]
    candidates = _player_candidates(
        db, names, record.nationality, birth_date=record.birth_date
    )
    if len(candidates) > 1:
        normalized = _normalizer("player", record.name)
        _record_ambiguity(
            db,
            entity_type="player",
            repository=record.source_repository,
            original=record.name,
            normalized=normalized,
            candidate_ids=candidates,
        )
        _save_mapping(
            db,
            entity_type="player",
            repository=record.source_repository,
            original=record.name,
            entity_id=None,
            status="ambiguous",
            notes=f"Candidates: {candidates}",
        )
        return False, True, 1, []
    duplicate = bool(candidates)
    entity = db.get(models.Player, candidates[0]) if candidates else None
    if entity is None:
        entity = models.Player(
            external_id=_catalog_external_id(
                record.source_repository,
                record.nationality,
                record.name,
                record.birth_date.isoformat() if record.birth_date else None,
            ),
            name=record.name,
            nationality=record.nationality,
            primary_position=record.position,
            birth_date=record.birth_date,
            data_source=SOURCE_NAME,
            source_updated_at=datetime.now(UTC),
            is_mock_data=False,
            active=True,
        )
        db.add(entity)
        db.flush()
    warnings: list[str] = []
    alias_conflicts = 0
    for field, value in (
        ("nationality", record.nationality),
        ("primary_position", record.position),
        ("birth_date", record.birth_date),
        ("height_m", record.height_m),
        ("birthplace", record.birthplace),
    ):
        current = getattr(entity, field, None)
        if current is None and value is not None:
            setattr(entity, field, value)
        elif value is not None and current != value:
            warnings.append(f"{record.name}: kept existing {field}={current!r}; catalog={value!r}")
    _merge_metadata(
        entity,
        {
            "source_file": record.source_file,
            "source_repository": record.source_repository,
            "identity_only": True,
        },
    )
    for value in names:
        conflicting_id = _add_alias(
            db, entity_type="player", entity_id=entity.id, value=value
        )
        if conflicting_id is not None:
            alias_conflicts += 1
            warnings.append(f"{record.name}: ambiguous alias {value!r} was not reassigned")
            normalized = _normalizer("player", value)
            _record_ambiguity(
                db,
                entity_type="player",
                repository=record.source_repository,
                original=value,
                normalized=normalized,
                candidate_ids=[entity.id, conflicting_id],
            )
            _save_mapping(
                db,
                entity_type="player",
                repository=record.source_repository,
                original=value,
                entity_id=None,
                status="ambiguous",
                notes=f"Candidates: {[entity.id, conflicting_id]}",
            )
        else:
            _save_mapping(
                db,
                entity_type="player",
                repository=record.source_repository,
                original=value,
                entity_id=entity.id,
                status="catalog",
            )
    return True, duplicate, alias_conflicts, warnings


def persist_openfootball_catalogs(
    db: Session, bundle: OpenFootballCatalogBundle
) -> OpenFootballCatalogPersistence:
    imported = duplicates = conflicts = 0
    counts = {"leagues": 0, "clubs": 0, "players": 0}
    warnings = list(bundle.warnings)
    for catalog in bundle.catalogs:
        for record in catalog.records:
            if isinstance(record, OpenFootballLeague):
                success, duplicate, record_conflicts, messages = _persist_league(db, record)
            elif isinstance(record, OpenFootballClub):
                success, duplicate, record_conflicts, messages = _persist_club(db, record)
            elif isinstance(record, OpenFootballPlayer):
                success, duplicate, record_conflicts, messages = _persist_player(db, record)
            else:  # pragma: no cover - union exhaustiveness guard
                continue
            warnings.extend(messages)
            conflicts += record_conflicts
            if success:
                imported += 1
                counts[catalog.kind] += 1
                duplicates += int(duplicate)
    db.flush()
    return OpenFootballCatalogPersistence(
        records_found=bundle.records_found,
        records_imported=imported,
        duplicates=duplicates,
        conflicts=conflicts,
        leagues_imported=counts["leagues"],
        clubs_imported=counts["clubs"],
        players_imported=counts["players"],
        warnings=tuple(dict.fromkeys(warnings)),
    )


__all__ = [
    "OpenFootballCatalogBundle",
    "OpenFootballCatalogPersistence",
    "discover_openfootball_catalogs",
    "persist_openfootball_catalogs",
]
