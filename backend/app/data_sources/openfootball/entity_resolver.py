"""Conservative OpenFootball entity resolution primitives.

Only exact normalized matches and very high-confidence, unambiguous fuzzy matches
may resolve automatically.  Display names are never destructively rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.services.entity_resolution import normalize_entity_name, resolve_name


@dataclass(frozen=True, slots=True)
class OpenFootballResolution:
    status: str
    internal_entity_id: int | None
    normalized_name: str
    confidence: float
    candidate_ids: tuple[int, ...]

    @property
    def requires_review(self) -> bool:
        return self.status in {"ambiguous", "manual_review"}


def resolve_openfootball_name(
    original_name: str,
    candidates: Iterable[tuple[int, str]],
) -> OpenFootballResolution:
    normalized = normalize_entity_name(original_name)
    materialized = list(candidates)
    exact_ids = tuple(
        dict.fromkeys(
            entity_id
            for entity_id, candidate_name in materialized
            if normalize_entity_name(candidate_name) == normalized
        )
    )
    if len(exact_ids) > 1:
        return OpenFootballResolution(
            status="ambiguous",
            internal_entity_id=None,
            normalized_name=normalized,
            confidence=1.0,
            candidate_ids=exact_ids,
        )
    result = resolve_name(
        original_name,
        materialized,
        auto_threshold=0.97,
        ambiguity_margin=0.06,
    )
    status = result.status
    if status == "unmatched":
        status = "new"
    elif status == "manual_review":
        # Never merge a merely similar name. Creating a separate identity is
        # safer; only multiple exact canonical IDs are blocked as ambiguous.
        status = "new"
    return OpenFootballResolution(
        status=status,
        internal_entity_id=result.entity_id,
        normalized_name=normalized,
        confidence=result.score,
        candidate_ids=tuple(candidate.entity_id for candidate in result.candidates),
    )


def openfootball_mapping_key(
    entity_type: str, source_repository: str, original_name: str
) -> tuple[str, str, str]:
    return (
        entity_type.casefold().strip(),
        source_repository.casefold().strip(),
        normalize_entity_name(original_name),
    )


__all__ = [
    "OpenFootballResolution",
    "openfootball_mapping_key",
    "resolve_openfootball_name",
]
