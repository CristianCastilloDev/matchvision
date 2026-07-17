from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable


def normalize_entity_name(value: str) -> str:
    """Normalize only for comparison; the original display name is always preserved."""

    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    alphanumeric = re.sub(r"[^a-z0-9]+", " ", without_accents.casefold())
    return " ".join(alphanumeric.split())


@dataclass(frozen=True)
class ResolutionCandidate:
    entity_id: int
    name: str
    score: float


@dataclass(frozen=True)
class ResolutionResult:
    status: str
    entity_id: int | None
    score: float
    candidates: tuple[ResolutionCandidate, ...]


def resolve_name(
    value: str,
    candidates: Iterable[tuple[int, str]],
    *,
    auto_threshold: float = 0.94,
    ambiguity_margin: float = 0.04,
) -> ResolutionResult:
    """Resolve exact/safe fuzzy matches; ambiguous results require manual review."""

    normalized = normalize_entity_name(value)
    ranked = sorted(
        (
            ResolutionCandidate(
                entity_id=entity_id,
                name=name,
                score=SequenceMatcher(None, normalized, normalize_entity_name(name)).ratio(),
            )
            for entity_id, name in candidates
        ),
        key=lambda item: item.score,
        reverse=True,
    )
    if not ranked:
        return ResolutionResult("unmatched", None, 0.0, ())
    best = ranked[0]
    if best.score == 1.0:
        return ResolutionResult("exact", best.entity_id, best.score, tuple(ranked[:5]))
    second_score = ranked[1].score if len(ranked) > 1 else 0.0
    if best.score >= auto_threshold and best.score - second_score >= ambiguity_margin:
        return ResolutionResult("fuzzy_approved", best.entity_id, best.score, tuple(ranked[:5]))
    status = "manual_review" if best.score >= 0.70 else "unmatched"
    return ResolutionResult(status, None, best.score, tuple(ranked[:5]))
