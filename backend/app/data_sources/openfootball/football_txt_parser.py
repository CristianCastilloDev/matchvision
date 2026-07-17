"""Offline parser for OpenFootball's Football.TXT mini-language.

Score semantics mirror OpenFootball rather than presentation shorthand: ft is
the result after 90 minutes, et after extra time (including golden-goal
variants), p the shoot-out only, and agg the tie aggregate. Missing period
scores remain missing; shoot-out goals never become match goals.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from pathlib import Path

from .schemas import MatchStatus, OpenFootballDataset, OpenFootballMatch, OpenFootballParseError
from .validators import normalize_openfootball_season


MAX_TXT_BYTES = 25 * 1024 * 1024
_SCORE = re.compile(r"(?<!\d)(\d{1,2})\s*[-:–—]\s*(\d{1,2})(?!\d)")
_AET = re.compile(
    r"^(?:"
    r"a\.?\s*e\.?\s*t\.?"
    r"(?:\s*/\s*(?:g\.?\s*g\.?|s\.?\s*g\.?|golden\s+goal|silver\s+goal))?"
    r"|after\s+extra\s+time(?:\s*/\s*(?:golden|silver)\s+goal)?"
    r"|g\.?\s*g\.?|golden\s+goal"
    r")\.?(?=\s|[,;()]|$)",
    re.I,
)
_PEN_MARKER = re.compile(
    r"^pen(?:alty|alties|s)?\.?(?=\s|[,;()]|$)",
    re.I,
)
_AGG_MARKER = re.compile(
    r"^(?:on\s+)?agg(?:regate)?\.?(?=\s|[,;()]|$)",
    re.I,
)
_SCORE_VALUE = r"(?P<home>\d{1,2})\s*[-:–—]\s*(?P<away>\d{1,2})"
_SCORE_LABEL = (
    r"(?:h\.?\s*t\.?|half[- ]?time|f\.?\s*t\.?|full[- ]?time|"
    r"a\.?\s*e\.?\s*t\.?|e\.?\s*t\.?|extra[- ]?time|"
    r"pen(?:alty|alties|s)?|(?:on\s+)?agg(?:regate)?)"
)
_LABELLED_PREFIX = re.compile(
    rf"^(?P<label>{_SCORE_LABEL})\.?\s*[:=]?\s*{_SCORE_VALUE}"
    r"(?=\s|[,;()]|$)",
    re.I,
)
_LABELLED_SUFFIX = re.compile(
    rf"^{_SCORE_VALUE}\s*(?P<label>{_SCORE_LABEL})\.?"
    r"(?=\s|[,;()]|$)",
    re.I,
)
_TIME = re.compile(
    r"^(?P<hour>[01]?\d|2[0-3])[:.](?P<minute>[0-5]\d)"
    r"(?:\s*(?P<zone>(?:UTC|GMT)\s*[+-]\s*\d{1,2}(?::[0-5]\d)?))?\b",
    re.I,
)
_MATCH_ID = re.compile(r"^\(([A-Za-z0-9][A-Za-z0-9_.:/-]*)\)\s+")
_SEASON = re.compile(r"(?<!\d)(\d{4})(?:\s*[-/]\s*(\d{2,4}))?(?!\d)")
_STATUS_PATTERNS: tuple[tuple[MatchStatus, re.Pattern[str]], ...] = (
    (MatchStatus.POSTPONED, re.compile(r"\b(?:postponed|postp\.?|ppd\.?|p\.?\s*p\.?)\b", re.I)),
    (MatchStatus.CANCELLED, re.compile(r"\b(?:cancelled|canceled|cancel)\b", re.I)),
    (MatchStatus.ABANDONED, re.compile(r"\b(?:abandoned|suspended|aborted)\b", re.I)),
)
_ROUND_HEADING = re.compile(
    r"^(?:group\s+[a-z0-9]+|matchday\s+\d+|round\s+(?:of\s+)?[\w -]+|"
    r"quarter[ -]?finals?|semi[ -]?finals?|final|match\s+for\s+third\s+place|"
    r"third[ -]?place|play[ -]?offs?)(?::)?$",
    re.I,
)
_MONTHS = {
    "jan": 1, "january": 1, "ene": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3, "mär": 3, "maer": 3,
    "apr": 4, "april": 4, "abr": 4,
    "may": 5, "mai": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8, "ago": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "okt": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "dez": 12, "dic": 12, "december": 12,
}
_WEEKDAY = (
    r"(?:Mon|Tue|Tues|Wed|Thu|Thur|Fri|Sat|Sun|"
    r"Mo|Tu|We|Th|Fr|Sa|Su)(?:day)?"
)
_WEEKDAY_PREFIX = rf"(?:(?:{_WEEKDAY})\s*,?\s+)?"
_DATE = re.compile(
    rf"^{_WEEKDAY_PREFIX}(?P<month>[A-Za-zÀ-ÿ]+)[ /.-]+(?P<day>\d{{1,2}})"
    r"(?:\s*,?\s*(?P<year>\d{2}|\d{4}))?(?=\s|,|$)\s*,?",
    re.I,
)
_DATE_REVERSED = re.compile(
    rf"^{_WEEKDAY_PREFIX}(?P<day>\d{{1,2}})\s+(?P<month>[A-Za-zÀ-ÿ]+)"
    r"(?:\s*,?\s*(?P<year>\d{2}|\d{4}))?(?=\s|,|$)\s*,?",
    re.I,
)
_NUMERIC_DATE = re.compile(
    rf"^{_WEEKDAY_PREFIX}(?P<day>\d{{1,2}})\s*(?P<separator>[./-])\s*"
    r"(?P<month>\d{1,2})\s*(?P=separator)\s*(?P<year>\d{2}|\d{4})\.?(?=\s|,|$)\s*,?",
    re.I,
)
_ISO_DATE = re.compile(
    r"^(?P<year>\d{4})[-/](?P<month>\d{1,2})[-/](?P<day>\d{1,2})\b"
)
_INLINE_MATCHDAY = re.compile(r"^(?:▪|::)\s*(?P<number>\d+)\b\s*")
_TEAM_SIDE = re.compile(r"^\((?P<side>[han])\)\s+", re.I)
_REPOSITORIES = (
    "england", "espana", "deutschland", "italy", "europe",
    "south-america", "worldcup", "world", "football.json",
)


def _season_value(value: str | None) -> str | None:
    if not value:
        return None
    match = _SEASON.search(value)
    if not match:
        return value.strip() or None
    start, end = match.groups()
    return start if end is None else normalize_openfootball_season(f"{start}-{end}")


def _season_years(season: str | None) -> tuple[int | None, int | None]:
    if not season or not (match := _SEASON.search(season)):
        return None, None
    start = int(match.group(1))
    raw_end = match.group(2)
    if raw_end is None:
        return start, start
    end = int(raw_end)
    if len(raw_end) == 2:
        end = (start // 100) * 100 + end
        if end < start:
            end += 100
    return start, end


def _consume_date(
    text: str, *, season: str | None, current_year: int | None
) -> tuple[str | None, int | None, int]:
    for pattern in (_ISO_DATE, _NUMERIC_DATE, _DATE, _DATE_REVERSED):
        match = pattern.match(text)
        if not match:
            continue
        groups = match.groupdict()
        month_raw = groups["month"]
        month = int(month_raw) if month_raw.isdigit() else _MONTHS.get(
            month_raw.casefold().rstrip("."), 0
        )
        start, end = _season_years(season)
        if groups.get("year"):
            raw_year = groups["year"]
            year = int(raw_year)
            if len(raw_year) == 2:
                reference = current_year or start or 2000
                year += (reference // 100) * 100
        elif start is not None and end is not None:
            year = start if start == end or month >= 7 else end
        else:
            year = current_year
        if not month or year is None:
            # Do not consume arbitrary words as date headings (for example the
            # national team ``France`` before a score).
            return None, current_year, 0
        try:
            parsed = date(year, month, int(groups["day"]))
        except ValueError:
            return None, current_year, 0
        return parsed.isoformat(), year, match.end()
    return None, current_year, 0


def _consume_time(text: str) -> tuple[str | None, int]:
    match = _TIME.match(text)
    if not match:
        return None, 0
    value = f"{int(match.group('hour')):02d}:{int(match.group('minute')):02d}"
    if match.group("zone"):
        value += " " + re.sub(r"\s+", "", match.group("zone").upper())
    return value, match.end()


def _pair(value: str) -> tuple[int, int] | None:
    match = _SCORE.fullmatch(value.strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


def _label_target(label: str) -> str:
    normalized = re.sub(r"[^a-z]", "", label.casefold())
    if normalized in {"ht", "halftime"}:
        return "ht"
    if normalized in {"ft", "fulltime"}:
        return "ft"
    if normalized in {"et", "aet", "extratime"}:
        return "et"
    if normalized.startswith("pen"):
        return "p"
    return "agg"


def _tagged_score(text: str) -> tuple[str, tuple[int, int], int] | None:
    """Consume either LABEL score or score LABEL from the start of text."""

    match = _LABELLED_PREFIX.match(text) or _LABELLED_SUFFIX.match(text)
    if not match:
        return None
    return (
        _label_target(match.group("label")),
        (int(match.group("home")), int(match.group("away"))),
        match.end(),
    )


def _period_block(
    content: str,
) -> tuple[dict[str, tuple[int, int]], list[tuple[int, int]]]:
    labelled: dict[str, tuple[int, int]] = {}
    positional: list[tuple[int, int]] = []
    for part in re.split(r"\s*[,;]\s*", content):
        part = part.strip()
        if not part:
            continue
        tagged = _tagged_score(part)
        if tagged and not part[tagged[2] :].strip():
            target, score, _ = tagged
            labelled[target] = score
        elif score := _pair(part):
            positional.append(score)
    return labelled, positional


def _positional_targets(
    main_kind: str, periods: list[tuple[int, int]]
) -> tuple[str, ...]:
    """Return only period fields made explicit by Football.TXT shorthand.

    A penalty result with three parenthesized scores carries ET, FT and HT.
    With two scores (the form used by Liga MX play-ins without extra time),
    the scores are FT and HT.  This distinction prevents a shoot-out from
    manufacturing an extra-time result.
    """

    if main_kind == "p":
        if len(periods) >= 3:
            return "et", "ft", "ht"
        if len(periods) == 2:
            return "ft", "ht"
        return ("ft",) if periods else ()
    return {
        "ft": ("ht",),
        "et": ("ft", "ht"),
        "agg": ("ft", "ht"),
    }[main_kind]


def _score_expression(
    text: str,
) -> tuple[dict[str, tuple[int, int] | None], int] | None:
    main = _SCORE.match(text)
    if not main:
        return None
    cursor = main.end()
    values: dict[str, tuple[int, int] | None] = {
        "ft": None, "ht": None, "et": None, "p": None, "agg": None
    }
    main_pair = (int(main.group(1)), int(main.group(2)))

    def skip_space() -> None:
        nonlocal cursor
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1

    skip_space()
    main_kind = "ft"
    marker = _PEN_MARKER.match(text[cursor:])
    if marker:
        main_kind = "p"
    else:
        marker = _AET.match(text[cursor:])
        if marker:
            main_kind = "et"
        else:
            marker = _AGG_MARKER.match(text[cursor:])
            if marker:
                main_kind = "agg"
    values[main_kind] = main_pair
    if marker:
        cursor += marker.end()
        skip_space()

    labelled: dict[str, tuple[int, int]] = {}
    periods: list[tuple[int, int]] = []
    if cursor < len(text) and text[cursor] == "(":
        close = text.find(")", cursor + 1)
        if close >= 0:
            labelled, periods = _period_block(text[cursor + 1 : close])
            if labelled or periods:
                cursor = close + 1
                skip_space()
    values.update(labelled)

    # OpenFootball's positional fuller forms run from the final non-shoot-out
    # result backwards through earlier periods.
    positional_targets = _positional_targets(main_kind, periods)
    for target, score in zip(positional_targets, periods, strict=False):
        if values[target] is None:
            values[target] = score

    while True:
        saved = cursor
        skip_space()
        if cursor < len(text) and text[cursor] in ",;":
            cursor += 1
            skip_space()
        remainder = text[cursor:]
        tagged = _tagged_score(remainder)
        if tagged is None:
            cursor = saved
            break
        target, score, consumed = tagged
        values[target] = score
        cursor += consumed
        skip_space()
        if cursor < len(text) and text[cursor] == "(":
            close = text.find(")", cursor + 1)
            if close >= 0:
                nested_labelled, nested_periods = _period_block(
                    text[cursor + 1 : close]
                )
                if nested_labelled or nested_periods:
                    values.update(nested_labelled)
                    for period_target, period_score in zip(
                        _positional_targets(target, nested_periods),
                        nested_periods,
                        strict=False,
                    ):
                        if values[period_target] is None:
                            values[period_target] = period_score
                    cursor = close + 1
    return values, cursor


def _status(text: str, *, has_score: bool, fixture: bool) -> MatchStatus:
    for status, pattern in _STATUS_PATTERNS:
        if pattern.search(text):
            return status
    if has_score:
        return MatchStatus.FINISHED
    return MatchStatus.SCHEDULED if fixture else MatchStatus.UNKNOWN


def _strip_status(text: str) -> str:
    for _, pattern in _STATUS_PATTERNS:
        text = pattern.sub("", text)
    return re.sub(r"\s+", " ", text).strip(" ,;[]()")


def _leg(text: str) -> str | None:
    if re.search(r"\b(?:1st|first)\s+leg\b|\bleg\s*1\b|\bida\b", text, re.I):
        return "first"
    if re.search(r"\b(?:2nd|second)\s+leg\b|\bleg\s*2\b|\bvuelta\b", text, re.I):
        return "second"
    return None


def _clean_team(text: str) -> str:
    text = _strip_status(text)
    text = re.sub(
        r"\s+\[?(?:1st|2nd|first|second)\s+leg\]?\s*$", "", text, flags=re.I
    )
    return re.sub(r"\s+", " ", text).strip(" -|,;")


def _split_venue(text: str) -> tuple[str, str | None, int | None]:
    match = re.search(r"\s+@\s*", text)
    if match:
        body, venue = text[: match.start()], text[match.end() :]
    else:
        body, venue = text, ""
    combined = f"{body} {venue}"
    attendance = None
    att = re.search(
        r"\batt(?:endance)?\.?\s*[:=]?\s*([\d][\d,._ ]*)\b", combined, re.I
    )
    if att:
        digits = re.sub(r"\D", "", att.group(1))
        attendance = int(digits) if digits else None
        pattern = r"\batt(?:endance)?\.?\s*[:=]?\s*[\d][\d,._ ]*\b"
        body = re.sub(pattern, "", body, flags=re.I).strip()
        venue = re.sub(pattern, "", venue, flags=re.I).strip(" ,;()")
    return body.strip(), venue.strip() or None, attendance


def _annotations_only(text: str) -> bool:
    text = _strip_status(text)
    text = re.sub(r"(?:1st|2nd|first|second)\s+leg", "", text, flags=re.I)
    return not re.sub(r"[\[\](){}.,;\s]", "", text)


def _teams_and_score(
    body: str,
) -> tuple[str, str, dict[str, tuple[int, int] | None], bool] | None:
    empty: dict[str, tuple[int, int] | None] = {
        "ft": None, "ht": None, "et": None, "p": None, "agg": None
    }
    versus = re.search(r"\s+(?:(?:v|vs|versus)\.?|-)\s+", body, re.I)
    if versus:
        home = _clean_team(body[: versus.start()])
        tail = body[versus.end() :].strip()
        for token in _SCORE.finditer(tail):
            parsed = _score_expression(tail[token.start() :])
            if parsed:
                scores, consumed = parsed
                if _annotations_only(tail[token.start() + consumed :]):
                    away = _clean_team(tail[: token.start()])
                    return (home, away, scores, True) if home and away else None
        away = _clean_team(tail)
        return (home, away, empty, True) if home and away else None

    for token in _SCORE.finditer(body):
        home = _clean_team(body[: token.start()])
        parsed = _score_expression(body[token.start() :])
        if not home or parsed is None:
            continue
        scores, consumed = parsed
        away = _clean_team(body[token.start() + consumed :])
        if away:
            return home, away, scores, False
    return None


def _team_based_match(
    body: str, team: str | None
) -> tuple[str, str, dict[str, tuple[int, int] | None], bool] | None:
    """Parse the 2026 ``competition | team`` schedule shorthand."""

    if not team or not (marker := _TEAM_SIDE.match(body)):
        return None
    side = marker.group("side").casefold()
    tail = body[marker.end() :].strip()
    empty: dict[str, tuple[int, int] | None] = {
        "ft": None, "ht": None, "et": None, "p": None, "agg": None
    }
    opponent = tail
    scores = empty
    for token in _SCORE.finditer(tail):
        parsed = _score_expression(tail[token.start() :])
        if parsed is None:
            continue
        candidate_scores, consumed = parsed
        if not _annotations_only(tail[token.start() + consumed :]):
            continue
        opponent = tail[: token.start()]
        scores = candidate_scores
        break
    opponent = _clean_team(opponent)
    if not opponent:
        return None

    # Football.TXT scores keep home-away order.  The marker supplies the team
    # omitted by this shorthand; neutral fixtures retain the listed team first.
    if side == "a":
        home, away = opponent, team
    else:
        home, away = team, opponent
    return home, away, scores, True


_SCORE_FIELDS = {
    "ft": ("fulltime_home_goals", "fulltime_away_goals"),
    "ht": ("halftime_home_goals", "halftime_away_goals"),
    "et": ("extra_time_home_goals", "extra_time_away_goals"),
    "p": ("penalty_home_goals", "penalty_away_goals"),
    "agg": ("aggregate_home_goals", "aggregate_away_goals"),
}


def _merge_period_continuation(
    previous: OpenFootballMatch, text: str
) -> OpenFootballMatch | None:
    """Merge a standalone explicit ``(FT, HT)``-style continuation."""

    block = re.fullmatch(r"\(\s*([^()]*)\s*\)\s*", text)
    if not block:
        return None
    parts = [part.strip() for part in re.split(r"\s*[,;]\s*", block.group(1))]
    if not parts or any(not part for part in parts):
        return None
    for part in parts:
        tagged = _tagged_score(part)
        if tagged:
            if part[tagged[2] :].strip():
                return None
        elif _pair(part) is None:
            return None

    labelled, periods = _period_block(block.group(1))
    if previous.penalty_home_goals is not None:
        main_kind = "p"
    elif previous.extra_time_home_goals is not None:
        main_kind = "et"
    elif previous.aggregate_home_goals is not None:
        main_kind = "agg"
    elif previous.fulltime_home_goals is not None:
        main_kind = "ft"
    else:
        return None

    explicit = dict(labelled)
    for target, score in zip(
        _positional_targets(main_kind, periods), periods, strict=False
    ):
        explicit.setdefault(target, score)

    updates: dict[str, int] = {}
    for target, score in explicit.items():
        home_field, away_field = _SCORE_FIELDS[target]
        if getattr(previous, home_field) is None and getattr(previous, away_field) is None:
            updates[home_field], updates[away_field] = score
    return replace(previous, **updates) if updates else previous


def _matchday(round_name: str | None) -> int | None:
    if not round_name:
        return None
    match = re.search(r"(?:matchday|round|jornada|spieltag)\s*(\d+)", round_name, re.I)
    return int(match.group(1)) if match else None


def _identity(
    text: str, competition: str | None, season: str | None
) -> tuple[str | None, str | None, str | None]:
    title = next(
        (
            line.strip()[1:].split("#", 1)[0].strip()
            for line in text.splitlines()
            if line.strip().startswith("=")
        ),
        None,
    )
    title_competition, separator, schedule_team = (title or "").partition("|")
    title_competition = title_competition.strip()
    schedule_team = schedule_team.strip() if separator else None
    if season is not None:
        season = _season_value(season)
    elif title_competition and _SEASON.search(title_competition):
        season = _season_value(title_competition)
    else:
        season = None
    if competition is None and title_competition:
        competition = (
            _SEASON.sub("", title_competition).strip(" -/") or title_competition
        )
    return competition, season, schedule_team


def _repository(source_file: str | None) -> str:
    text = (source_file or "").casefold().replace("_", "-")
    return next((repo for repo in _REPOSITORIES if repo in text), "openfootball-local")


def parse_football_txt_data(
    text: str,
    *,
    source_file: str | None = None,
    competition: str | None = None,
    season: str | None = None,
    source_repository: str | None = None,
) -> OpenFootballDataset:
    """Parse in-memory Football.TXT text, including its shorthand inheritance."""

    if not isinstance(text, str):
        raise TypeError("Football.TXT input must be text")
    if len(text.encode("utf-8")) > MAX_TXT_BYTES:
        raise OpenFootballParseError("Football.TXT exceeds the size limit")
    competition, season, schedule_team = _identity(text, competition, season)
    repository = source_repository or _repository(source_file)
    current_year, _ = _season_years(season)
    current_date: str | None = None
    current_time: str | None = None
    current_round: str | None = None
    matches: list[OpenFootballMatch] = []
    annotation_depth = 0
    ignored = 0

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip().lstrip("\ufeff")
        if not stripped or stripped.startswith(("#", "=")):
            continue
        line, hash_mark, inline_comment = stripped.partition("#")
        line = line.strip()
        inline_comment = inline_comment.strip() if hash_mark else ""
        if not line:
            continue
        if line[0] in {"▪", "»", "•", "◆"}:
            heading = line[1:].strip().split("|", 1)[0].strip().rstrip(":")
            if heading:
                current_round = heading
            continue
        if (
            "|" in line
            and not _SCORE.search(line)
            and not re.search(r"\s+v(?:s)?\.?\s+", line, re.I)
        ):
            continue
        if _ROUND_HEADING.fullmatch(line.rstrip(":")):
            current_round = line.rstrip(":").strip()
            continue

        # Handle score-period and goal-scorer continuations before the generic
        # ``(source-id)`` prefix.  Official annotation lines also start with a
        # parenthesis and must never be mistaken for record identifiers.
        if matches and not _MATCH_ID.match(line) and not _TEAM_SIDE.match(line) and (
            line.startswith(("(", "[")) or annotation_depth
        ):
            merged = _merge_period_continuation(matches[-1], line)
            if merged is not None:
                matches[-1] = merged
                annotation_depth = 0
                continue
            note = " ".join(filter(None, (matches[-1].notes, line, inline_comment)))
            matches[-1] = replace(matches[-1], notes=note)
            annotation_depth = max(
                0, annotation_depth + line.count("(") - line.count(")")
            )
            continue

        source_match_id = None
        if (id_match := _MATCH_ID.match(line)) and not (
            schedule_team and _TEAM_SIDE.match(line)
        ):
            source_match_id = id_match.group(1).strip()
            line = line[id_match.end() :].lstrip()

        parsed_date, parsed_year, date_end = _consume_date(
            line, season=season, current_year=current_year
        )
        if date_end:
            remainder = line[date_end:].strip()
            if parsed_date:
                if parsed_date != current_date:
                    current_time = None
                current_date = parsed_date
            current_year = parsed_year
            if not remainder:
                continue
            line = remainder

        kickoff, time_end = _consume_time(line)
        if time_end:
            current_time = kickoff
            line = line[time_end:].strip()
        else:
            kickoff = current_time

        if inline_round := _INLINE_MATCHDAY.match(line):
            current_round = f"Matchday {int(inline_round.group('number'))}"
            line = line[inline_round.end() :].strip()

        body, venue, attendance = _split_venue(line)
        parsed = _team_based_match(body, schedule_team) or _teams_and_score(body)
        if parsed is None:
            ignored += 1
            continue

        annotation_depth = 0
        home, away, scores, fixture_syntax = parsed
        has_score = any(scores[key] is not None for key in ("ft", "et", "p"))
        match_status = _status(body, has_score=has_score, fixture=fixture_syntax)
        notes = [inline_comment] if inline_comment else []
        if match_status in {
            MatchStatus.POSTPONED, MatchStatus.CANCELLED, MatchStatus.ABANDONED
        }:
            notes.append(match_status.value)
        values = {
            key: scores[key] or (None, None)
            for key in ("ft", "ht", "et", "p", "agg")
        }
        matches.append(
            OpenFootballMatch(
                competition=competition,
                season=season,
                round=current_round,
                matchday=_matchday(current_round),
                date=current_date,
                kickoff_time=kickoff,
                home_team=home,
                away_team=away,
                fulltime_home_goals=values["ft"][0],
                fulltime_away_goals=values["ft"][1],
                halftime_home_goals=values["ht"][0],
                halftime_away_goals=values["ht"][1],
                extra_time_home_goals=values["et"][0],
                extra_time_away_goals=values["et"][1],
                penalty_home_goals=values["p"][0],
                penalty_away_goals=values["p"][1],
                aggregate_home_goals=values["agg"][0],
                aggregate_away_goals=values["agg"][1],
                leg=_leg(f"{current_round or ''} {body}"),
                group=(
                    current_round
                    if current_round and current_round.casefold().startswith("group ")
                    else None
                ),
                venue=venue,
                attendance=attendance,
                notes="; ".join(notes) or None,
                status=match_status,
                source_file=source_file,
                source_repository=repository,
                source_line=line_number,
                source_match_id=source_match_id,
                raw_payload=raw_line,
            )
        )

    warnings = (f"ignored {ignored} non-match data line(s)",) if ignored else ()
    return OpenFootballDataset(
        matches=tuple(matches),
        competition=competition,
        season=season,
        source_file=source_file,
        source_repository=repository,
        warnings=warnings,
        metadata={
            "format": "football-txt",
            "input_lines": len(text.splitlines()),
            "ignored_data_lines": ignored,
        },
    )


def parse_football_txt(
    path: str | Path,
    *,
    competition: str | None = None,
    season: str | None = None,
    source_repository: str | None = None,
) -> OpenFootballDataset:
    """Parse one local Football.TXT file; no network access occurs."""

    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise OpenFootballParseError("Football.TXT path must be a file")
    if source.stat().st_size > MAX_TXT_BYTES:
        raise OpenFootballParseError("Football.TXT exceeds the size limit")
    payload = source.read_bytes()
    text = None
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            pass
    if text is None:
        raise OpenFootballParseError("Football.TXT encoding is not supported")
    return parse_football_txt_data(
        text,
        source_file=str(source),
        competition=competition,
        season=season,
        source_repository=source_repository,
    )


__all__ = ["MAX_TXT_BYTES", "parse_football_txt", "parse_football_txt_data"]
