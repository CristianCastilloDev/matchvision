"""Validation and conservative team-name normalization for OpenFootball."""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Mapping
from datetime import date
from typing import Any

from .schemas import MatchStatus, OpenFootballMatch, ValidationResult


_PAIR_FIELDS = (
    ("fulltime_home_goals", "fulltime_away_goals"),
    ("halftime_home_goals", "halftime_away_goals"),
    ("extra_time_home_goals", "extra_time_away_goals"),
    ("penalty_home_goals", "penalty_away_goals"),
    ("aggregate_home_goals", "aggregate_away_goals"),
)
_TRAILING_CLUB_SUFFIXES = {"fc", "cf", "sc", "ac", "afc"}


def normalize_openfootball_season(value: str | None) -> str | None:
    """Canonicalize equivalent season labels to ``YYYY-YY``."""

    if value is None:
        return None
    text = value.strip()
    match = re.fullmatch(r"(\d{4})\s*[-/]\s*(\d{2}|\d{4})", text)
    if match is None:
        return text or None
    start = int(match.group(1))
    raw_end = match.group(2)
    end = int(raw_end)
    if len(raw_end) == 2:
        end = (start // 100) * 100 + end
        if end < start:
            end += 100
    return f"{start}-{end % 100:02d}"


def normalize_openfootball_team(name: str) -> str:
    """Return a comparison key, never an asserted entity match.

    The function normalizes Unicode, accents, punctuation, ``Utd`` and a single
    trailing club-type suffix.  Only the complete, explicitly required alias
    ``Man United`` expands to ``Manchester United``; other short names remain
    unchanged and require catalog alias resolution.
    """

    if not isinstance(name, str):
        raise TypeError("team name must be a string")
    value = html.unescape(unicodedata.normalize("NFKC", name)).strip()
    value = re.sub(r"\((?:[A-Z]{3}|[A-Z]{2,3}-[A-Z]{2,3})\)\s*$", "", value)
    decomposed = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in decomposed if not unicodedata.combining(char))
    tokens = re.findall(r"[a-z0-9]+", value.casefold())
    tokens = ["united" if token == "utd" else token for token in tokens]
    if len(tokens) > 1 and tokens[-1] in _TRAILING_CLUB_SUFFIXES:
        tokens.pop()
    # Explicit alias required by the OpenFootball integration contract.  Keep
    # this allow-list narrow: a generic expansion of every "Man" would merge
    # unrelated clubs, whereas these complete keys are unambiguous catalog
    # aliases for Manchester United.
    if tokens == ["man", "united"]:
        tokens = ["manchester", "united"]
    return " ".join(tokens)


def _read(record: OpenFootballMatch | Mapping[str, Any], name: str) -> Any:
    return getattr(record, name) if isinstance(record, OpenFootballMatch) else record.get(name)


def validate_openfootball_match(
    record: OpenFootballMatch | Mapping[str, Any],
) -> ValidationResult:
    """Validate one normalized record without changing or filling values."""

    errors: list[str] = []
    warnings: list[str] = []
    home = str(_read(record, "home_team") or "").strip()
    away = str(_read(record, "away_team") or "").strip()
    if not home:
        errors.append("home_team is required")
    if not away:
        errors.append("away_team is required")
    if home and away and normalize_openfootball_team(home) == normalize_openfootball_team(away):
        errors.append("home_team and away_team must be different")

    raw_status = _read(record, "status")
    try:
        status = raw_status if isinstance(raw_status, MatchStatus) else MatchStatus(str(raw_status))
    except ValueError:
        status = MatchStatus.UNKNOWN
        errors.append(f"unsupported status: {raw_status}")

    for home_field, away_field in _PAIR_FIELDS:
        home_value = _read(record, home_field)
        away_value = _read(record, away_field)
        if (home_value is None) != (away_value is None):
            errors.append(f"{home_field} and {away_field} must be provided together")
        for field_name, value in ((home_field, home_value), (away_field, away_value)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                errors.append(f"{field_name} must be an integer or null")
            elif isinstance(value, int) and not 0 <= value <= 99:
                errors.append(f"{field_name} must be between 0 and 99")

    ft_home = _read(record, "fulltime_home_goals")
    ft_away = _read(record, "fulltime_away_goals")
    ht_home = _read(record, "halftime_home_goals")
    ht_away = _read(record, "halftime_away_goals")
    et_home = _read(record, "extra_time_home_goals")
    et_away = _read(record, "extra_time_away_goals")
    pen_home = _read(record, "penalty_home_goals")
    pen_away = _read(record, "penalty_away_goals")

    if all(isinstance(value, int) for value in (ht_home, ht_away, ft_home, ft_away)):
        if ht_home > ft_home or ht_away > ft_away:
            errors.append("halftime score cannot exceed fulltime score")
    if all(isinstance(value, int) for value in (ft_home, ft_away, et_home, et_away)):
        if ft_home > et_home or ft_away > et_away:
            errors.append("fulltime score cannot exceed extra-time score")
    if pen_home is not None and pen_away is not None:
        deciding_home = et_home if et_home is not None else ft_home
        deciding_away = et_away if et_away is not None else ft_away
        if deciding_home is None or deciding_away is None:
            warnings.append("penalty score has no preceding match score")
        elif deciding_home != deciding_away:
            warnings.append("penalty shoot-out follows a non-drawn match score")

    has_completed_score = ft_home is not None or et_home is not None
    if status == MatchStatus.FINISHED and not has_completed_score:
        errors.append("finished match requires a fulltime or extra-time score")
    if status == MatchStatus.SCHEDULED and has_completed_score:
        errors.append("scheduled match cannot have a completed score")

    date_value = _read(record, "date")
    if date_value is None:
        errors.append("date is required")
    else:
        try:
            date.fromisoformat(str(date_value))
        except ValueError:
            errors.append("date must use YYYY-MM-DD")
    time_value = _read(record, "kickoff_time")
    if time_value is not None and not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d(?:\s+(?:UTC|GMT)[+-]\d{1,2}(?::[0-5]\d)?)?",
        str(time_value),
        re.IGNORECASE,
    ):
        warnings.append("kickoff_time is not a recognized HH:MM[/UTC offset] value")

    attendance = _read(record, "attendance")
    if attendance is not None and (
        isinstance(attendance, bool) or not isinstance(attendance, int) or attendance < 0
    ):
        errors.append("attendance must be a non-negative integer or null")
    return ValidationResult(not errors, tuple(errors), tuple(warnings))


def ensure_valid_openfootball_match(
    record: OpenFootballMatch | Mapping[str, Any],
) -> None:
    result = validate_openfootball_match(record)
    if not result.valid:
        raise ValueError("; ".join(result.errors))


__all__ = [
    "ensure_valid_openfootball_match",
    "normalize_openfootball_season",
    "normalize_openfootball_team",
    "validate_openfootball_match",
]
