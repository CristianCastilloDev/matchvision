"""Conservative parsers for the OpenFootball identity catalog repositories.

The leagues, clubs and players repositories use small Football.TXT-style
catalogs.  They are identity metadata only: this module deliberately exposes
no performance or form fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal


CatalogKind = Literal["leagues", "clubs", "players"]
CATALOG_KINDS: tuple[CatalogKind, ...] = ("leagues", "clubs", "players")

MAX_CATALOG_BYTES = 25 * 1024 * 1024
_HEADING = re.compile(r"^=+\s*(?P<label>.*?)\s*=*$")
_YEAR = re.compile(r"^(?:18|19|20)\d{2}$")
_HEIGHT = re.compile(r"(?<!\d)(?P<height>[12](?:[.,]\d{1,2})?)\s*m\b", re.I)
_HEIGHT_CM = re.compile(r"(?P<height>1[2-9]\d|2[0-3]\d)\s*(?:cm)?", re.I)
_BIRTH = re.compile(
    r"(?<!\w)(?:b\.?\s*)?(?P<day>\d{1,2})\s+"
    r"(?P<month>[A-Za-zÀ-ÿ.]+)\s+(?P<year>\d{4})\b",
    re.I,
)
_AUXILIARY_CATALOG_SUFFIXES = (
    ".props.txt",
    ".history.txt",
    ".stadiums.txt",
    ".seasons.txt",
)
_AUXILIARY_CATALOG_NAMES = {"seasons.txt"}
_MONTHS = {
    "jan": 1,
    "january": 1,
    "ene": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "mär": 3,
    "maer": 3,
    "apr": 4,
    "april": 4,
    "abr": 4,
    "may": 5,
    "mai": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "ago": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "okt": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "dez": 12,
    "dic": 12,
    "december": 12,
}
_POSITIONS = {
    "G": "goalkeeper",
    "GK": "goalkeeper",
    "D": "defender",
    "DF": "defender",
    "M": "midfielder",
    "MF": "midfielder",
    "F": "forward",
    "FW": "forward",
}


def detect_openfootball_catalog_kind(
    name: str, repository: str | None = None
) -> CatalogKind | None:
    """Infer a catalog kind from a concrete path, then an optional fallback."""

    if is_openfootball_catalog_auxiliary(name):
        return None
    values = [*Path(name.replace("\\", "/")).parts, repository or ""]
    for value in values:
        token = value.casefold().replace("_", "-")
        for kind in CATALOG_KINDS:
            if token == kind or token.startswith(f"{kind}-") or token.startswith(f"{kind}."):
                return kind
    return None


def is_openfootball_catalog_auxiliary(name: str) -> bool:
    """Return whether ``name`` is catalog support data, not canonical entities.

    OpenFootball repositories keep properties, history, stadium and season
    sidecars next to the canonical identity files.  Their row shapes can look
    deceptively similar to club or league records, so callers must route them
    separately instead of guessing entities from them.
    """

    basename = Path(name.replace("\\", "/")).name.casefold()
    return basename in _AUXILIARY_CATALOG_NAMES or basename.endswith(
        _AUXILIARY_CATALOG_SUFFIXES
    )


def sniff_openfootball_catalog_kind(text: str) -> CatalogKind | None:
    """Recognize a standalone catalog only from strong, non-match signatures."""

    lines = [_meaningful_line(line) for line in text.splitlines()]
    lines = [line for line in lines if line and _heading(line) is None]
    if any(_looks_like_player_row(line) for line in lines):
        return "players"
    if any(
        re.search(r",\s*(?:18|19|20)\d{2}\s*,\s*@\s*[^,]+", line)
        for line in lines
    ):
        return "clubs"
    has_inline_league_aliases = any(
        "|" in line and not line.lstrip().startswith("|") and "," not in line
        for line in lines
    )
    has_aliases = any(_aliases(line) is not None for line in lines) or has_inline_league_aliases
    has_league_row = any(
        re.fullmatch(r"(?:\d+|[A-Za-z][A-Za-z0-9_-]{0,9})\s+[^,|]+", line)
        for line in lines
    ) or has_inline_league_aliases
    has_match_shape = any(
        re.search(r"\s+(?:v|vs)\.?\s+|\b\d{1,2}\s*[-:]\s*\d{1,2}\b", line, re.I)
        for line in lines
    )
    if has_aliases and has_league_row and not has_match_shape:
        return "leagues"
    return None


def _looks_like_player_row(line: str) -> bool:
    parts = [part.strip() for part in line.split(",")]
    if len(parts) < 3 or not parts[0]:
        return False
    metadata = ", ".join(parts[1:])
    has_birth_date = _BIRTH.search(metadata) is not None
    has_metric_height = _HEIGHT.search(metadata) is not None
    has_centimeter_height = any(_HEIGHT_CM.fullmatch(part) for part in parts[1:])
    has_position = any(part.upper() in _POSITIONS for part in parts[1:])
    # Historical players use ``Name|Alias, 3 Sep 1979, 186`` while current
    # files normally include position, metric height and ``b.``.  Requiring a
    # date plus either height representation keeps fixture rows out.
    return has_birth_date and (has_metric_height or has_centimeter_height) and (
        has_position or "|" in parts[0] or len(parts) >= 3
    )


@dataclass(slots=True)
class OpenFootballLeague:
    code: str
    name: str
    country: str | None = None
    aliases: list[str] = field(default_factory=list)
    division: int | None = None
    competition_type: Literal["league", "cup"] | None = None
    source_file: str | None = None
    source_repository: str = "leagues"


@dataclass(slots=True)
class OpenFootballClub:
    name: str
    country: str | None = None
    aliases: list[str] = field(default_factory=list)
    founded_year: int | None = None
    city: str | None = None
    stadium: str | None = None
    source_file: str | None = None
    source_repository: str = "clubs"


@dataclass(slots=True)
class OpenFootballPlayer:
    name: str
    nationality: str | None = None
    aliases: list[str] = field(default_factory=list)
    position: str | None = None
    height_m: float | None = None
    birth_date: date | None = None
    birthplace: str | None = None
    source_file: str | None = None
    source_repository: str = "players"


@dataclass(frozen=True, slots=True)
class OpenFootballCatalog:
    kind: CatalogKind
    records: tuple[OpenFootballLeague | OpenFootballClub | OpenFootballPlayer, ...]
    warnings: tuple[str, ...] = ()


def _meaningful_line(raw: str) -> str:
    line = raw.strip().lstrip("\ufeff")
    if not line or line.startswith("#"):
        return ""
    return line.split("#", 1)[0].strip()


def _heading(line: str) -> str | None:
    match = _HEADING.fullmatch(line)
    if not match:
        return None
    value = match.group("label").strip(" =")
    return value or None


def _aliases(line: str) -> list[str] | None:
    if not line.lstrip().startswith("|"):
        return None
    return [value.strip() for value in line.split("|") if value.strip()]


def _competition_type(code: str, name: str) -> Literal["league", "cup"] | None:
    normalized = f"{code} {name}".casefold()
    cup_tokens = (
        " cup",
        "copa ",
        "pokal",
        "coupe",
        "taça",
        "taca",
        "champions league",
        "europa league",
        "libertadores",
        "sudamericana",
    )
    if any(token in f" {normalized}" for token in cup_tokens):
        return "cup"
    if code.isdigit() or any(token in normalized for token in ("league", "liga", "division")):
        return "league"
    return None


def parse_openfootball_leagues_data(
    text: str, *, source_file: str | None = None
) -> OpenFootballCatalog:
    records: list[OpenFootballLeague] = []
    warnings: list[str] = []
    country: str | None = None
    current: OpenFootballLeague | None = None
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = _meaningful_line(raw)
        if not line:
            continue
        if (heading := _heading(line)) is not None:
            country = heading
            current = None
            continue
        aliases = _aliases(line)
        if aliases is not None:
            if current is None:
                warnings.append(f"{source_file or '<memory>'}:{line_number}: aliases without league")
            else:
                current.aliases.extend(value for value in aliases if value not in current.aliases)
            continue
        inline = [value.strip() for value in line.split("|")]
        canonical = inline[0].strip(" ,;")
        inline_aliases = [value for value in inline[1:] if value]
        parts = canonical.split(maxsplit=1)
        if len(parts) == 2 and _looks_like_league_code(parts[0]):
            code, name = parts[0].strip(), parts[1].strip(" ,;")
        elif canonical:
            code, name = "", canonical
        else:
            warnings.append(f"{source_file or '<memory>'}:{line_number}: unsupported league row")
            current = None
            continue
        division = int(code) if code.isdigit() and int(code) > 0 else None
        current = OpenFootballLeague(
            code=code,
            name=name,
            country=country,
            aliases=list(dict.fromkeys(inline_aliases)),
            division=division,
            competition_type=_competition_type(code, name),
            source_file=source_file,
        )
        records.append(current)
    return OpenFootballCatalog("leagues", tuple(records), tuple(warnings))


def _looks_like_league_code(value: str) -> bool:
    """Recognize explicit OpenFootball codes without stealing name tokens.

    Official catalog codes are numeric or compact lowercase identifiers (for
    example ``1`` and ``cl``).  Title-cased ``World`` in ``World Cup`` is a
    name, not a code.
    """

    return bool(
        value.isdigit()
        or (
            value == value.casefold()
            and re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,9}", value)
        )
    )


def parse_openfootball_clubs_data(
    text: str, *, source_file: str | None = None
) -> OpenFootballCatalog:
    records: list[OpenFootballClub] = []
    warnings: list[str] = []
    country: str | None = None
    current: OpenFootballClub | None = None
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = _meaningful_line(raw)
        if not line:
            continue
        if (heading := _heading(line)) is not None:
            country = heading
            current = None
            continue
        aliases = _aliases(line)
        if aliases is not None:
            if current is None:
                warnings.append(f"{source_file or '<memory>'}:{line_number}: aliases without club")
            else:
                current.aliases.extend(value for value in aliases if value not in current.aliases)
            continue
        parts = [part.strip() for part in line.split(",")]
        name = parts[0].strip(" ,;") if parts else ""
        if not name or name.startswith(("@", ">")):
            warnings.append(f"{source_file or '<memory>'}:{line_number}: unsupported club row")
            current = None
            continue
        founded_year: int | None = None
        stadium: str | None = None
        city: str | None = None
        stadium_index: int | None = None
        for index, part in enumerate(parts[1:], start=1):
            if founded_year is None and _YEAR.fullmatch(part):
                founded_year = int(part)
            elif part.startswith("@") and part[1:].strip():
                stadium = part[1:].strip()
                stadium_index = index
        if stadium_index is not None:
            city = next(
                (part for part in parts[stadium_index + 1 :] if part and not _YEAR.fullmatch(part)),
                None,
            )
        current = OpenFootballClub(
            name=name,
            country=country,
            founded_year=founded_year,
            city=city,
            stadium=stadium,
            source_file=source_file,
        )
        records.append(current)
    return OpenFootballCatalog("clubs", tuple(records), tuple(warnings))


def _birth_date(text: str) -> date | None:
    match = _BIRTH.search(text)
    if not match:
        return None
    month = _MONTHS.get(match.group("month").casefold().rstrip("."))
    if month is None:
        return None
    try:
        return date(int(match.group("year")), month, int(match.group("day")))
    except ValueError:
        return None


def parse_openfootball_players_data(
    text: str, *, source_file: str | None = None
) -> OpenFootballCatalog:
    records: list[OpenFootballPlayer] = []
    warnings: list[str] = []
    nationality: str | None = None
    current: OpenFootballPlayer | None = None
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = _meaningful_line(raw)
        if not line:
            continue
        if (heading := _heading(line)) is not None:
            nationality = heading
            current = None
            continue
        aliases = _aliases(line)
        if aliases is not None:
            if current is None:
                warnings.append(f"{source_file or '<memory>'}:{line_number}: aliases without player")
            else:
                current.aliases.extend(value for value in aliases if value not in current.aliases)
            continue
        parts = [part.strip() for part in line.split(",")]
        name_values = [value.strip(" ,;") for value in parts[0].split("|")] if parts else []
        name_values = [value for value in name_values if value]
        name = name_values[0] if name_values else ""
        if not name:
            warnings.append(f"{source_file or '<memory>'}:{line_number}: unsupported player row")
            current = None
            continue
        metadata = ", ".join(parts[1:])
        position = next(
            (_POSITIONS[part.upper()] for part in parts[1:] if part.upper() in _POSITIONS),
            None,
        )
        height_match = _HEIGHT.search(metadata)
        height_m = None
        if height_match:
            height_m = float(height_match.group("height").replace(",", "."))
        else:
            height_cm_match = next(
                (_HEIGHT_CM.fullmatch(part) for part in parts[1:] if _HEIGHT_CM.fullmatch(part)),
                None,
            )
            if height_cm_match:
                height_m = int(height_cm_match.group("height")) / 100
        birthplace = None
        if "@" in metadata:
            birthplace = metadata.rsplit("@", 1)[1].strip(" ,;") or None
        current = OpenFootballPlayer(
            name=name,
            nationality=nationality,
            aliases=list(dict.fromkeys(name_values[1:])),
            position=position,
            height_m=height_m,
            birth_date=_birth_date(metadata),
            birthplace=birthplace,
            source_file=source_file,
        )
        records.append(current)
    return OpenFootballCatalog("players", tuple(records), tuple(warnings))


def parse_openfootball_catalog_data(
    text: str, kind: CatalogKind, *, source_file: str | None = None
) -> OpenFootballCatalog:
    if kind == "leagues":
        return parse_openfootball_leagues_data(text, source_file=source_file)
    if kind == "clubs":
        return parse_openfootball_clubs_data(text, source_file=source_file)
    return parse_openfootball_players_data(text, source_file=source_file)


def parse_openfootball_catalog(path: str | Path, kind: CatalogKind) -> OpenFootballCatalog:
    source = Path(path).expanduser().resolve(strict=True)
    payload = source.read_bytes()
    if len(payload) > MAX_CATALOG_BYTES:
        raise ValueError("OpenFootball catalog exceeds size limit")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = payload.decode("cp1252")
    return parse_openfootball_catalog_data(text, kind, source_file=source.name)


__all__ = [
    "CATALOG_KINDS",
    "CatalogKind",
    "OpenFootballCatalog",
    "OpenFootballClub",
    "OpenFootballLeague",
    "OpenFootballPlayer",
    "detect_openfootball_catalog_kind",
    "is_openfootball_catalog_auxiliary",
    "parse_openfootball_catalog",
    "parse_openfootball_catalog_data",
    "parse_openfootball_clubs_data",
    "parse_openfootball_leagues_data",
    "parse_openfootball_players_data",
    "sniff_openfootball_catalog_kind",
]
