"""Manual, auditable resolution of OpenFootball import conflicts.

The service deliberately operates only on conflicts already recorded by the
offline OpenFootball ingestion pipeline. It never accepts arbitrary model
field names or arbitrary entity identifiers from callers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


SOURCE_NAME = "openfootball"

_PENDING_MAPPING_STATUSES = ("ambiguous", "manual_review", "pending")
_ENTITY_MODELS: dict[str, type[models.Competition] | type[models.Team] | type[models.Player]] = {
    "competition": models.Competition,
    "team": models.Team,
    "player": models.Player,
}

_MATCH_SCORE_FIELDS = {
    "home_score",
    "away_score",
    "halftime_home_score",
    "halftime_away_score",
}
_MATCH_STRING_LIMITS = {"venue": 180, "round_name": 100}
_MATCH_STATUSES = {
    "scheduled",
    "finished",
    "postponed",
    "cancelled",
    "abandoned",
    "unknown",
    "void",
}
_RESULT_SCORE_FIELDS = {
    "fulltime_home_goals",
    "fulltime_away_goals",
    "extra_time_home_goals",
    "extra_time_away_goals",
    "penalty_home_goals",
    "penalty_away_goals",
    "aggregate_home_goals",
    "aggregate_away_goals",
}
_RESULT_STRING_LIMITS = {
    "leg": 100,
    "group": 100,
    "kickoff_time": 100,
    "kickoff_time_source": 100,
    "stored_timezone": 50,
    "date_precision": 30,
    "source_date": 10,
}
_RESULT_BOOLEAN_FIELDS = {"kickoff_time_known", "timezone_known"}
_SCORE_DECISION_GROUPS = (
    (
        "home_score",
        "away_score",
        "result_details.fulltime_home_goals",
        "result_details.fulltime_away_goals",
        "result_details.extra_time_home_goals",
        "result_details.extra_time_away_goals",
    ),
    ("halftime_home_score", "halftime_away_score"),
    ("result_details.penalty_home_goals", "result_details.penalty_away_goals"),
    ("result_details.aggregate_home_goals", "result_details.aggregate_away_goals"),
)


class OpenFootballConflictError(ValueError):
    """Base class for invalid OpenFootball conflict operations."""


class OpenFootballConflictNotFound(LookupError):
    """Raised when the requested OpenFootball conflict does not exist."""


class OpenFootballConflictStateError(OpenFootballConflictError):
    """Raised when a conflict is malformed or no longer pending."""


def _entity_model(entity_type: str):
    model = _ENTITY_MODELS.get(entity_type)
    if model is None:
        raise OpenFootballConflictStateError(
            f"Tipo de entidad OpenFootball no soportado: {entity_type}"
        )
    return model


def _candidate_ids(value: Any) -> list[int]:
    if not isinstance(value, list):
        raise OpenFootballConflictStateError("candidate_ids no es una lista válida")
    output: list[int] = []
    for candidate_id in value:
        if isinstance(candidate_id, bool) or not isinstance(candidate_id, int):
            raise OpenFootballConflictStateError("candidate_ids contiene un identificador inválido")
        if candidate_id not in output:
            output.append(candidate_id)
    if not output:
        raise OpenFootballConflictStateError("El conflicto no contiene candidatos")
    return output


def _mapping_scope(
    db: Session,
    conflict: models.EntityResolutionConflict,
    *,
    require_resolvable: bool = False,
) -> tuple[list[models.OpenFootballEntityMapping], str, list[str]]:
    statement = (
        select(models.OpenFootballEntityMapping)
        .where(
            models.OpenFootballEntityMapping.entity_type == conflict.entity_type,
            models.OpenFootballEntityMapping.normalized_name == conflict.normalized_name,
            models.OpenFootballEntityMapping.resolution_status.in_(_PENDING_MAPPING_STATUSES),
        )
        .order_by(models.OpenFootballEntityMapping.id)
    )
    if conflict.source_repository:
        rows = list(
            db.scalars(
                statement.where(
                    models.OpenFootballEntityMapping.source_repository
                    == conflict.source_repository
                )
            ).all()
        )
        status = "exact" if rows else "missing_mapping"
        if require_resolvable and not rows:
            raise OpenFootballConflictStateError(
                "No existe un mapping pendiente en el repositorio exacto del conflicto"
            )
        return rows, status, [conflict.source_repository]

    potential = list(db.scalars(statement).all())
    repositories = sorted({mapping.source_repository for mapping in potential})
    if len(potential) == 1:
        return potential, "legacy_single_mapping", repositories
    if require_resolvable:
        if not potential:
            raise OpenFootballConflictStateError(
                "El conflicto legacy no tiene un mapping OpenFootball pendiente"
            )
        raise OpenFootballConflictStateError(
            "El conflicto legacy abarca varios repositorios; debe reimportarse para obtener scope exacto"
        )
    return [], "legacy_ambiguous" if potential else "missing_mapping", repositories


def _entity_conflict_out(
    db: Session, conflict: models.EntityResolutionConflict
) -> dict[str, Any]:
    candidate_ids = _candidate_ids(conflict.candidate_ids)
    model = _entity_model(conflict.entity_type)
    candidates: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        entity = db.get(model, candidate_id)
        candidates.append(
            {
                "id": candidate_id,
                "name": getattr(entity, "name", None),
                "available": entity is not None and not bool(getattr(entity, "is_mock_data", True)),
            }
        )
    mappings, scope_status, potential_repositories = _mapping_scope(db, conflict)
    return {
        "id": conflict.id,
        "entity_type": conflict.entity_type,
        "source_name": conflict.source_name,
        "normalized_name": conflict.normalized_name,
        "candidate_ids": candidate_ids,
        "candidates": candidates,
        "best_score": conflict.best_score,
        "status": conflict.status,
        "source_repository": conflict.source_repository,
        "scope_status": scope_status,
        "source_repositories": sorted({mapping.source_repository for mapping in mappings}),
        "potential_source_repositories": potential_repositories,
        "mapping_ids": [mapping.id for mapping in mappings],
        "resolution_notes": conflict.resolution_notes,
        "created_at": conflict.created_at,
        "updated_at": conflict.updated_at,
    }


def _conflict_fields(record: models.MatchSourceRecord) -> list[dict[str, Any]]:
    details = record.conflict_details
    if not isinstance(details, dict) or not isinstance(details.get("fields"), list):
        raise OpenFootballConflictStateError(
            f"El conflicto de partido {record.id} no contiene fields válidos"
        )
    fields: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in details["fields"]:
        if not isinstance(item, dict):
            raise OpenFootballConflictStateError(
                f"El conflicto de partido {record.id} contiene una entrada inválida"
            )
        field = item.get("field")
        if not isinstance(field, str) or not field or field in seen:
            raise OpenFootballConflictStateError(
                f"El conflicto de partido {record.id} contiene un campo inválido o repetido"
            )
        if "existing" not in item or "incoming" not in item:
            raise OpenFootballConflictStateError(
                f"El conflicto de partido {record.id} no conserva ambos valores"
            )
        _validate_field_value(field, item["incoming"])
        seen.add(field)
        fields.append(
            {"field": field, "existing": item["existing"], "incoming": item["incoming"]}
        )
    if not fields:
        raise OpenFootballConflictStateError(
            f"El conflicto de partido {record.id} no contiene campos pendientes"
        )
    return fields


def _match_conflict_out(record: models.MatchSourceRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "match_id": record.match_id,
        "source_repository": record.source_repository,
        "source_record_id": record.source_record_id,
        "source_file": record.source_file,
        "conflict_status": record.conflict_status,
        "fields": _conflict_fields(record),
        "imported_at": record.imported_at,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def list_pending_openfootball_conflicts(
    db: Session, *, offset: int = 0, limit: int = 100
) -> dict[str, Any]:
    """List pending entity and match-field conflicts without exposing raw payloads."""

    if offset < 0 or not 1 <= limit <= 200:
        raise OpenFootballConflictError("Paginación fuera de rango")
    entity_conflicts = list(
        db.scalars(
            select(models.EntityResolutionConflict)
            .where(
                models.EntityResolutionConflict.provider == SOURCE_NAME,
                models.EntityResolutionConflict.status == "pending",
            )
            .order_by(models.EntityResolutionConflict.created_at, models.EntityResolutionConflict.id)
            .offset(offset)
            .limit(limit)
        ).all()
    )
    match_conflicts = list(
        db.scalars(
            select(models.MatchSourceRecord)
            .where(
                models.MatchSourceRecord.source_name == SOURCE_NAME,
                models.MatchSourceRecord.conflict_status == "conflict",
            )
            .order_by(models.MatchSourceRecord.imported_at, models.MatchSourceRecord.id)
            .offset(offset)
            .limit(limit)
        ).all()
    )
    return {
        "entity_conflicts": [_entity_conflict_out(db, item) for item in entity_conflicts],
        "match_conflicts": [_match_conflict_out(item) for item in match_conflicts],
    }


def resolve_openfootball_entity_conflict(
    db: Session,
    conflict_id: int,
    *,
    candidate_id: int,
    notes: str | None = None,
) -> dict[str, Any]:
    """Resolve a pending entity conflict using one of its recorded candidates."""

    conflict = db.scalar(
        select(models.EntityResolutionConflict)
        .where(
            models.EntityResolutionConflict.id == conflict_id,
            models.EntityResolutionConflict.provider == SOURCE_NAME,
        )
        .with_for_update()
    )
    if conflict is None:
        raise OpenFootballConflictNotFound("Conflicto de entidad OpenFootball no encontrado")
    if conflict.status != "pending":
        raise OpenFootballConflictStateError("El conflicto de entidad ya no está pendiente")
    candidate_ids = _candidate_ids(conflict.candidate_ids)
    if (
        isinstance(candidate_id, bool)
        or not isinstance(candidate_id, int)
        or candidate_id not in candidate_ids
    ):
        raise OpenFootballConflictError("candidate_id no pertenece a candidate_ids")

    model = _entity_model(conflict.entity_type)
    entity = db.get(model, candidate_id)
    if entity is None or bool(getattr(entity, "is_mock_data", True)):
        raise OpenFootballConflictError("El candidato seleccionado no es una entidad real válida")
    mappings, scope_status, _potential_repositories = _mapping_scope(
        db, conflict, require_resolvable=True
    )
    if conflict.source_repository is None:
        # Safe legacy upgrade: one and only one pending mapping identifies the
        # repository. Persist it so the resolution scope remains auditable.
        conflict.source_repository = mappings[0].source_repository

    now = datetime.now(UTC)
    clean_notes = _clean_notes(notes)
    audit_note = (
        f"Resolución manual OpenFootball: candidate_id={candidate_id}; "
        f"source_repository={conflict.source_repository}; "
        f"mapping_ids={','.join(str(mapping.id) for mapping in mappings)}; "
        f"scope={scope_status}; resolved_at={now.isoformat()}"
    )
    if clean_notes:
        audit_note = f"{audit_note}; notes={clean_notes}"
    for mapping in mappings:
        mapping.internal_entity_id = candidate_id
        mapping.confidence = 1.0
        mapping.manually_verified = True
        mapping.resolution_status = "manually_resolved"
        mapping.resolution_notes = audit_note
    conflict.status = "resolved"
    conflict.resolution_notes = audit_note

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(conflict)
    return {
        "id": conflict.id,
        "entity_type": conflict.entity_type,
        "status": conflict.status,
        "selected_candidate_id": candidate_id,
        "selected_candidate_name": entity.name,
        "updated_mapping_ids": [mapping.id for mapping in mappings],
        "source_repositories": sorted({mapping.source_repository for mapping in mappings}),
        "manually_verified": True,
        "resolution_notes": conflict.resolution_notes,
        "resolved_at": now,
    }


def _validate_score(field: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 99:
        raise OpenFootballConflictStateError(f"Valor incoming inválido para {field}")


def _clean_notes(notes: str | None) -> str | None:
    if notes is None:
        return None
    if not isinstance(notes, str):
        raise OpenFootballConflictError("notes debe ser texto")
    clean = notes.strip()
    if len(clean) > 2_000:
        raise OpenFootballConflictError("notes supera 2000 caracteres")
    return clean or None


def _validate_string(field: str, value: Any, max_length: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise OpenFootballConflictStateError(f"Valor incoming inválido para {field}")


def _validate_field_value(field: str, value: Any) -> None:
    if field in _MATCH_SCORE_FIELDS:
        _validate_score(field, value)
        return
    if field in _MATCH_STRING_LIMITS:
        _validate_string(field, value, _MATCH_STRING_LIMITS[field])
        return
    if field == "status":
        if not isinstance(value, str) or value not in _MATCH_STATUSES:
            raise OpenFootballConflictStateError("Valor incoming inválido para status")
        return
    prefix = "result_details."
    if not field.startswith(prefix):
        raise OpenFootballConflictStateError(f"Campo de partido no permitido: {field}")
    detail_field = field.removeprefix(prefix)
    if detail_field in _RESULT_SCORE_FIELDS:
        _validate_score(field, value)
        return
    if detail_field in _RESULT_STRING_LIMITS:
        _validate_string(field, value, _RESULT_STRING_LIMITS[detail_field])
        return
    if detail_field in _RESULT_BOOLEAN_FIELDS:
        if not isinstance(value, bool):
            raise OpenFootballConflictStateError(f"Valor incoming inválido para {field}")
        return
    if detail_field == "attendance":
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000_000:
            raise OpenFootballConflictStateError(f"Valor incoming inválido para {field}")
        return
    raise OpenFootballConflictStateError(f"Campo result_details no permitido: {field}")


def _normalized_decisions(
    decisions: Mapping[str, str], fields: Sequence[Mapping[str, Any]]
) -> dict[str, str]:
    expected = {str(item["field"]) for item in fields}
    if not isinstance(decisions, Mapping):
        raise OpenFootballConflictError("decisions debe ser un objeto")
    normalized: dict[str, str] = {}
    for field, choice in decisions.items():
        if not isinstance(field, str) or choice not in {"existing", "incoming"}:
            raise OpenFootballConflictError(
                "Cada decisión debe elegir explícitamente existing o incoming"
            )
        normalized[field] = choice
    missing = sorted(expected - normalized.keys())
    unexpected = sorted(normalized.keys() - expected)
    if missing or unexpected:
        parts: list[str] = []
        if missing:
            parts.append(f"faltan decisiones para: {', '.join(missing)}")
        if unexpected:
            parts.append(f"campos inesperados: {', '.join(unexpected)}")
        raise OpenFootballConflictError("; ".join(parts))
    return normalized


def _validate_decision_groups(choices: Mapping[str, str]) -> None:
    for group in _SCORE_DECISION_GROUPS:
        selected = {choices[field] for field in group if field in choices}
        if len(selected) > 1:
            raise OpenFootballConflictError(
                "Los dos lados de un marcador en conflicto deben elegirse del mismo origen: "
                + ", ".join(field for field in group if field in choices)
            )


def _validate_pair(label: str, home: Any, away: Any) -> None:
    if (home is None) != (away is None):
        raise OpenFootballConflictError(f"{label} requiere ambos marcadores o ninguno")


def _validate_final_match_state(
    match_values: Mapping[str, Any], result_details: Mapping[str, Any]
) -> None:
    home_score = match_values["home_score"]
    away_score = match_values["away_score"]
    halftime_home = match_values["halftime_home_score"]
    halftime_away = match_values["halftime_away_score"]
    status = str(match_values["status"])
    _validate_pair("Marcador final", home_score, away_score)
    _validate_pair("Marcador de medio tiempo", halftime_home, halftime_away)
    for field, value in (
        ("home_score", home_score),
        ("away_score", away_score),
        ("halftime_home_score", halftime_home),
        ("halftime_away_score", halftime_away),
    ):
        if value is not None:
            _validate_score(field, value)
    if halftime_home is not None:
        if home_score is None or away_score is None:
            raise OpenFootballConflictError(
                "Un marcador de medio tiempo requiere marcador final"
            )
        if halftime_home > home_score or halftime_away > away_score:
            raise OpenFootballConflictError(
                "El marcador de medio tiempo no puede exceder el marcador final"
            )

    detail_pairs = (
        ("fulltime", "fulltime_home_goals", "fulltime_away_goals"),
        ("extra_time", "extra_time_home_goals", "extra_time_away_goals"),
        ("penalty", "penalty_home_goals", "penalty_away_goals"),
        ("aggregate", "aggregate_home_goals", "aggregate_away_goals"),
    )
    for label, home_field, away_field in detail_pairs:
        home_value = result_details.get(home_field)
        away_value = result_details.get(away_field)
        _validate_pair(label, home_value, away_value)
        if home_value is not None:
            _validate_score(f"result_details.{home_field}", home_value)
            _validate_score(f"result_details.{away_field}", away_value)

    detail_ft_home = result_details.get("fulltime_home_goals")
    detail_ft_away = result_details.get("fulltime_away_goals")
    detail_et_home = result_details.get("extra_time_home_goals")
    detail_et_away = result_details.get("extra_time_away_goals")
    canonical_home = detail_ft_home if detail_ft_home is not None else detail_et_home
    canonical_away = detail_ft_away if detail_ft_away is not None else detail_et_away
    if canonical_home is not None and (home_score, away_score) != (
        canonical_home,
        canonical_away,
    ):
        raise OpenFootballConflictError(
            "El marcador normalizado debe coincidir con FT, o con ET cuando FT no existe"
        )
    if all(
        value is not None
        for value in (detail_ft_home, detail_ft_away, detail_et_home, detail_et_away)
    ) and (detail_ft_home > detail_et_home or detail_ft_away > detail_et_away):
        raise OpenFootballConflictError("El marcador FT no puede exceder el marcador ET")

    if status == "finished" and home_score is None:
        raise OpenFootballConflictError("Un partido finished requiere marcador final")
    if status in {"scheduled", "postponed", "cancelled", "void"} and home_score is not None:
        raise OpenFootballConflictError(f"Un partido {status} no puede conservar marcador final")
    if status not in _MATCH_STATUSES:
        raise OpenFootballConflictError(f"Estado final no soportado: {status}")


def resolve_openfootball_match_conflict(
    db: Session,
    source_record_id: int,
    *,
    decisions: Mapping[str, str],
    notes: str | None = None,
) -> dict[str, Any]:
    """Apply explicit per-field choices and retain the original conflict as audit data."""

    record = db.scalar(
        select(models.MatchSourceRecord)
        .where(
            models.MatchSourceRecord.id == source_record_id,
            models.MatchSourceRecord.source_name == SOURCE_NAME,
        )
        .with_for_update()
    )
    if record is None:
        raise OpenFootballConflictNotFound("Conflicto de partido OpenFootball no encontrado")
    if record.conflict_status != "conflict":
        raise OpenFootballConflictStateError("El conflicto de partido ya no está pendiente")
    match = db.get(models.Match, record.match_id)
    if match is None:
        raise OpenFootballConflictStateError("El partido asociado al conflicto ya no existe")

    fields = _conflict_fields(record)
    choices = _normalized_decisions(decisions, fields)
    _validate_decision_groups(choices)
    result_details = dict(match.result_details or {})
    match_values = {
        field: getattr(match, field)
        for field in (*_MATCH_SCORE_FIELDS, *_MATCH_STRING_LIMITS, "status")
    }
    before: dict[str, Any] = {}
    applied: list[str] = []
    kept: list[str] = []
    for item in fields:
        field = str(item["field"])
        choice = choices[field]
        if field.startswith("result_details."):
            detail_field = field.removeprefix("result_details.")
            before[field] = result_details.get(detail_field)
            if choice == "incoming":
                result_details[detail_field] = item["incoming"]
                applied.append(field)
            else:
                kept.append(field)
            continue
        before[field] = getattr(match, field)
        if choice == "incoming":
            match_values[field] = item["incoming"]
            applied.append(field)
        else:
            kept.append(field)

    _validate_final_match_state(match_values, result_details)
    for field in applied:
        if not field.startswith("result_details."):
            setattr(match, field, match_values[field])
    if any(field.startswith("result_details.") for field in applied):
        match.result_details = result_details or None
    now = datetime.now(UTC)
    if applied:
        match.source_updated_at = now
    original_audit = dict(record.conflict_details or {})
    original_audit["resolution"] = {
        "strategy": "manual_field_selection",
        "resolved_at": now.isoformat(),
        "decisions": choices,
        "values_before_resolution": before,
        "applied_incoming_fields": applied,
        "kept_existing_fields": kept,
        "notes": _clean_notes(notes),
    }
    record.conflict_details = original_audit
    record.conflict_status = "resolved"

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(record)
    db.refresh(match)
    return {
        "id": record.id,
        "match_id": match.id,
        "conflict_status": record.conflict_status,
        "decisions": choices,
        "applied_incoming_fields": applied,
        "kept_existing_fields": kept,
        "resolution": record.conflict_details["resolution"],
        "updated_at": record.updated_at,
    }


__all__ = [
    "OpenFootballConflictError",
    "OpenFootballConflictNotFound",
    "OpenFootballConflictStateError",
    "list_pending_openfootball_conflicts",
    "resolve_openfootball_entity_conflict",
    "resolve_openfootball_match_conflict",
]
