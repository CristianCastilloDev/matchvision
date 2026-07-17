"""Provider-neutral records produced by the offline OpenFootball parsers.

The score fields deliberately follow OpenFootball's JSON semantics: ``ft`` is
the score after 90 minutes, ``et`` after 120 minutes, and ``p`` is the penalty
shoot-out.  Shoot-out goals are therefore never added to match goals.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class OpenFootballError(ValueError):
    """Base exception raised for an unusable OpenFootball input."""


class OpenFootballParseError(OpenFootballError):
    """The input is readable but is not a supported OpenFootball dataset."""


class MatchStatus(StrEnum):
    SCHEDULED = "scheduled"
    FINISHED = "finished"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class OpenFootballMatch:
    """One normalized fixture/result while retaining its source provenance."""

    home_team: str
    away_team: str
    competition: str | None = None
    season: str | None = None
    round: str | None = None
    matchday: int | None = None
    date: str | None = None
    kickoff_time: str | None = None
    fulltime_home_goals: int | None = None
    fulltime_away_goals: int | None = None
    halftime_home_goals: int | None = None
    halftime_away_goals: int | None = None
    extra_time_home_goals: int | None = None
    extra_time_away_goals: int | None = None
    penalty_home_goals: int | None = None
    penalty_away_goals: int | None = None
    aggregate_home_goals: int | None = None
    aggregate_away_goals: int | None = None
    leg: str | None = None
    group: str | None = None
    venue: str | None = None
    attendance: int | None = None
    notes: str | None = None
    status: MatchStatus = MatchStatus.UNKNOWN
    source_file: str | None = None
    source_repository: str | None = None
    source_line: int | None = None
    source_match_id: str | None = None
    raw_payload: Mapping[str, Any] | str | None = field(default=None, repr=False)

    @property
    def final_home_goals(self) -> int | None:
        """Return the match score after ET when played, otherwise after 90'."""

        return (
            self.extra_time_home_goals
            if self.extra_time_home_goals is not None
            else self.fulltime_home_goals
        )

    @property
    def final_away_goals(self) -> int | None:
        return (
            self.extra_time_away_goals
            if self.extra_time_away_goals is not None
            else self.fulltime_away_goals
        )

    def to_dict(self, *, include_raw: bool = True) -> dict[str, Any]:
        record = asdict(self)
        record["status"] = self.status.value
        record["final_home_goals"] = self.final_home_goals
        record["final_away_goals"] = self.final_away_goals
        if not include_raw:
            record.pop("raw_payload", None)
        return record


@dataclass(frozen=True, slots=True)
class OpenFootballDataset:
    """Parsed contents of one local JSON or Football.TXT file."""

    matches: tuple[OpenFootballMatch, ...]
    competition: str | None = None
    season: str | None = None
    source_file: str | None = None
    source_repository: str | None = None
    warnings: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def records(self) -> tuple[OpenFootballMatch, ...]:
        """Compatibility alias for importers that call datasets records."""

        return self.matches

    @property
    def total_matches(self) -> int:
        return len(self.matches)

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        return {
            "competition": self.competition,
            "season": self.season,
            "matches": [match.to_dict(include_raw=include_raw) for match in self.matches],
            "source_file": self.source_file,
            "source_repository": self.source_repository,
            "warnings": list(self.warnings),
            "metadata": dict(self.metadata),
            "total_matches": self.total_matches,
            "data_source": "openfootball_local_file",
        }


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


__all__ = [
    "MatchStatus",
    "OpenFootballDataset",
    "OpenFootballError",
    "OpenFootballMatch",
    "OpenFootballParseError",
    "ValidationResult",
]
