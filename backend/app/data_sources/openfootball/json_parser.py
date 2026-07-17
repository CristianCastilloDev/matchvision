"""Offline parser for generated ``openfootball/football.json`` datasets."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .schemas import MatchStatus, OpenFootballDataset, OpenFootballMatch, OpenFootballParseError
from .validators import normalize_openfootball_season


MAX_JSON_BYTES = 25 * 1024 * 1024
_SEASON_RE = re.compile(r"(?<!\d)(\d{4}(?:[-/]\d{2,4})?)(?!\d)")
_REPOSITORIES = (
    "football.json",
    "england",
    "espana",
    "deutschland",
    "italy",
    "europe",
    "south-america",
    "worldcup",
    "world",
    "clubs",
    "players",
    "leagues",
)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _first(record: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in record and record[name] not in (None, ""):
            return record[name]
    return None


def _pair(value: Any) -> tuple[int | None, int | None]:
    if isinstance(value, Mapping):
        home = _first(value, "home", "team1", "h", "home_goals", "score1")
        away = _first(value, "away", "team2", "a", "away_goals", "score2")
        return _integer(home), _integer(away)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes) and len(value) >= 2:
        return _integer(value[0]), _integer(value[1])
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+)\s*[-:–—]\s*(\d+)\s*", value)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None, None


def _integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 and value.is_integer() else None
    text = str(value).strip().replace(",", "")
    return int(text) if text.isdigit() else None


def _score(record: Mapping[str, Any]) -> dict[str, tuple[int | None, int | None]]:
    score = record.get("score")
    score_map = score if isinstance(score, Mapping) else {}
    values: dict[str, tuple[int | None, int | None]] = {}
    aliases = {
        "ft": ("ft", "fulltime", "full_time", "regular", "regulation"),
        "ht": ("ht", "halftime", "half_time"),
        "et": ("et", "aet", "extra_time", "extratime"),
        "p": ("p", "pen", "pens", "penalties", "shootout"),
        "agg": ("agg", "aggregate", "aggregate_score"),
    }
    for target, names in aliases.items():
        value = _first(score_map, *names)
        values[target] = _pair(value)
    if not score_map and score is not None:
        values["ft"] = _pair(score)

    direct = {
        "ft": (
            ("fulltime_home_goals", "home_goals", "score1"),
            ("fulltime_away_goals", "away_goals", "score2"),
        ),
        "ht": (("halftime_home_goals", "ht_home"), ("halftime_away_goals", "ht_away")),
        "et": (("extra_time_home_goals", "et_home"), ("extra_time_away_goals", "et_away")),
        "p": (("penalty_home_goals", "pen_home"), ("penalty_away_goals", "pen_away")),
        "agg": (("aggregate_home_goals", "agg_home"), ("aggregate_away_goals", "agg_away")),
    }
    for target, (home_names, away_names) in direct.items():
        if values[target] == (None, None):
            values[target] = (_integer(_first(record, *home_names)), _integer(_first(record, *away_names)))
    return values


def _normalize_status(value: Any, *, has_score: bool) -> MatchStatus:
    if value is None:
        return MatchStatus.FINISHED if has_score else MatchStatus.SCHEDULED
    token = re.sub(r"[^a-z]+", " ", str(value).casefold()).strip()
    if token in {"finished", "complete", "completed", "ft", "full time", "aet", "pen"}:
        return MatchStatus.FINISHED
    if token in {"scheduled", "fixture", "not started", "pending", "tbd", "upcoming"}:
        return MatchStatus.SCHEDULED
    if token in {"postponed", "postp", "ppd", "p p"}:
        return MatchStatus.POSTPONED
    if token in {"cancelled", "canceled", "cancel"}:
        return MatchStatus.CANCELLED
    if token in {"abandoned", "suspended", "aborted"}:
        return MatchStatus.ABANDONED
    return MatchStatus.UNKNOWN


def _date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _time(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    match = re.match(
        r"^(\d{1,2}):(\d{2})(?::\d{2})?(?:\s*((?:UTC|GMT)\s*[+-]\s*\d{1,2}(?::\d{2})?))?",
        text,
        re.IGNORECASE,
    )
    if not match:
        return text
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return text
    offset = re.sub(r"\s+", "", match.group(3).upper()) if match.group(3) else None
    return f"{hour:02d}:{minute:02d}" + (f" {offset}" if offset else "")


def _matchday(round_name: str | None, explicit: Any = None) -> int | None:
    value = _integer(explicit)
    if value is not None:
        return value
    if round_name:
        match = re.search(r"(?:matchday|round|jornada|spieltag)\s*(\d+)", round_name, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _leg(record: Mapping[str, Any], round_name: str | None) -> str | None:
    value = _text(_first(record, "leg", "leg_number"))
    haystack = " ".join(filter(None, (value, round_name, _text(record.get("notes")))))
    if re.search(r"\b(?:1st|first|ida|leg\s*1)\b", haystack, re.IGNORECASE):
        return "first"
    if re.search(r"\b(?:2nd|second|vuelta|leg\s*2)\b", haystack, re.IGNORECASE):
        return "second"
    return value


def _attendance(value: Any) -> int | None:
    if isinstance(value, str):
        value = re.sub(r"[^0-9]", "", value)
    return _integer(value)


def _derive_identity(
    name: str | None,
    source_file: str | None,
    competition: str | None,
    season: str | None,
) -> tuple[str | None, str | None]:
    season = normalize_openfootball_season(season)
    candidates = [name or "", source_file or ""]
    if season is None:
        for candidate in candidates:
            match = _SEASON_RE.search(candidate)
            if match:
                season = normalize_openfootball_season(match.group(1))
                break
    if competition is None and name:
        competition = _SEASON_RE.sub("", name).strip(" -/") or name.strip()
    if competition is None and source_file:
        competition = Path(source_file).stem
    return competition, season


def _source_repository(source_file: str | None) -> str | None:
    text = (source_file or "").casefold().replace("_", "-")
    return next((repo for repo in _REPOSITORIES if repo in text), None)


def _containers(payload: Any) -> tuple[list[tuple[Mapping[str, Any], str | None]], str | None, bool]:
    """Return match mappings, inherited rounds, and whether a dataset shape existed."""

    output: list[tuple[Mapping[str, Any], str | None]] = []
    shaped = False
    if isinstance(payload, list):
        shaped = True
        for item in payload:
            if isinstance(item, Mapping):
                output.append((item, None))
        return output, None, shaped
    if not isinstance(payload, Mapping):
        return output, None, shaped
    name = _text(_first(payload, "name", "competition", "league"))
    for key in ("matches", "games", "fixtures"):
        rows = payload.get(key)
        if isinstance(rows, list):
            shaped = True
            output.extend((row, None) for row in rows if isinstance(row, Mapping))
            return output, name, shaped
    rounds = payload.get("rounds")
    if isinstance(rounds, Mapping):
        shaped = True
        for round_name, rows in rounds.items():
            if isinstance(rows, list):
                output.extend((row, str(round_name)) for row in rows if isinstance(row, Mapping))
    elif isinstance(rounds, list):
        shaped = True
        for round_item in rounds:
            if isinstance(round_item, Mapping):
                inherited = _text(_first(round_item, "name", "round", "title"))
                rows = _first(round_item, "matches", "games", "fixtures")
                if isinstance(rows, list):
                    output.extend((row, inherited) for row in rows if isinstance(row, Mapping))
            elif isinstance(round_item, list):
                output.extend((row, None) for row in round_item if isinstance(row, Mapping))
    if output or shaped:
        return output, name, shaped
    # A single match object is accepted for manual/local interoperability.
    if _first(payload, "team1", "home_team", "home", "team_1") is not None:
        return [(payload, None)], name, True
    return output, name, False


def _parse_match(
    record: Mapping[str, Any],
    *,
    inherited_round: str | None,
    competition: str | None,
    season: str | None,
    source_file: str | None,
    source_repository: str | None,
) -> OpenFootballMatch | None:
    home = _text(_first(record, "team1", "home_team", "home", "team_1", "homeTeam"))
    away = _text(_first(record, "team2", "away_team", "away", "team_2", "awayTeam"))
    if not home or not away:
        return None
    round_name = _text(_first(record, "round", "round_name", "stage")) or inherited_round
    scores = _score(record)
    ft_home, ft_away = scores["ft"]
    et_home, et_away = scores["et"]
    has_score = (ft_home is not None and ft_away is not None) or (
        et_home is not None and et_away is not None
    )
    status = _normalize_status(_first(record, "status", "match_status", "state"), has_score=has_score)
    notes = _text(_first(record, "notes", "note", "comment", "remarks"))
    return OpenFootballMatch(
        competition=_text(_first(record, "competition", "league")) or competition,
        season=normalize_openfootball_season(_text(record.get("season")) or season),
        round=round_name,
        matchday=_matchday(round_name, _first(record, "matchday", "match_day")),
        date=_date(_first(record, "date", "match_date", "day")),
        kickoff_time=_time(_first(record, "time", "kickoff_time", "kick_off")),
        home_team=home,
        away_team=away,
        fulltime_home_goals=ft_home,
        fulltime_away_goals=ft_away,
        halftime_home_goals=scores["ht"][0],
        halftime_away_goals=scores["ht"][1],
        extra_time_home_goals=et_home,
        extra_time_away_goals=et_away,
        penalty_home_goals=scores["p"][0],
        penalty_away_goals=scores["p"][1],
        aggregate_home_goals=scores["agg"][0],
        aggregate_away_goals=scores["agg"][1],
        leg=_leg(record, round_name),
        group=_text(record.get("group")),
        venue=_text(_first(record, "ground", "venue", "stadium", "location")),
        attendance=_attendance(record.get("attendance")),
        notes=notes,
        status=status,
        source_file=source_file,
        source_repository=source_repository,
        source_match_id=_text(_first(record, "id", "match_id", "fixture_id", "num")),
        raw_payload=dict(record),
    )


def parse_openfootball_json_data(
    payload: bytes | str | Mapping[str, Any] | list[Any],
    *,
    source_file: str | None = None,
    competition: str | None = None,
    season: str | None = None,
    source_repository: str | None = None,
) -> OpenFootballDataset:
    """Parse in-memory JSON; used by the safe ZIP/repository orchestrator."""

    if isinstance(payload, bytes):
        if len(payload) > MAX_JSON_BYTES:
            raise OpenFootballParseError("OpenFootball JSON exceeds the size limit")
        try:
            payload = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise OpenFootballParseError("OpenFootball JSON must be UTF-8") from exc
    if isinstance(payload, str):
        if len(payload.encode("utf-8")) > MAX_JSON_BYTES:
            raise OpenFootballParseError("OpenFootball JSON exceeds the size limit")
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise OpenFootballParseError(f"Invalid OpenFootball JSON: {exc}") from exc
    else:
        decoded = payload

    rows, dataset_name, shaped = _containers(decoded)
    if not shaped:
        raise OpenFootballParseError("JSON has no supported matches/rounds/list shape")
    competition, season = _derive_identity(dataset_name, source_file, competition, season)
    repository = source_repository or _source_repository(source_file) or "openfootball-local"
    warnings: list[str] = []
    matches: list[OpenFootballMatch] = []
    for index, (record, inherited_round) in enumerate(rows, start=1):
        parsed = _parse_match(
            record,
            inherited_round=inherited_round,
            competition=competition,
            season=season,
            source_file=source_file,
            source_repository=repository,
        )
        if parsed is None:
            warnings.append(f"record {index}: missing home or away team")
        else:
            matches.append(parsed)
    metadata = {
        "name": dataset_name,
        "format": "openfootball-json",
        "input_records": len(rows),
        "skipped_records": len(rows) - len(matches),
    }
    return OpenFootballDataset(
        matches=tuple(matches),
        competition=competition,
        season=season,
        source_file=source_file,
        source_repository=repository,
        warnings=tuple(warnings),
        metadata=metadata,
    )


def parse_openfootball_json(
    path: str | Path,
    *,
    competition: str | None = None,
    season: str | None = None,
    source_repository: str | None = None,
) -> OpenFootballDataset:
    """Parse one local OpenFootball JSON file.  This function performs no I/O beyond it."""

    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise OpenFootballParseError("OpenFootball JSON path must be a file")
    if source.stat().st_size > MAX_JSON_BYTES:
        raise OpenFootballParseError("OpenFootball JSON exceeds the size limit")
    return parse_openfootball_json_data(
        source.read_bytes(),
        source_file=str(source),
        competition=competition,
        season=season,
        source_repository=source_repository,
    )


__all__ = ["MAX_JSON_BYTES", "parse_openfootball_json", "parse_openfootball_json_data"]
