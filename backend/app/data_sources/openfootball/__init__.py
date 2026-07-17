"""Strictly local OpenFootball parsers and import primitives."""

from .catalog_parser import (
    OpenFootballCatalog,
    OpenFootballClub,
    OpenFootballLeague,
    OpenFootballPlayer,
    detect_openfootball_catalog_kind,
    parse_openfootball_catalog,
    parse_openfootball_catalog_data,
    parse_openfootball_clubs_data,
    parse_openfootball_leagues_data,
    parse_openfootball_players_data,
    sniff_openfootball_catalog_kind,
)
from .football_txt_parser import parse_football_txt, parse_football_txt_data
from .json_parser import parse_openfootball_json, parse_openfootball_json_data
from .schemas import (
    MatchStatus,
    OpenFootballDataset,
    OpenFootballError,
    OpenFootballMatch,
    OpenFootballParseError,
    ValidationResult,
)
from .validators import (
    ensure_valid_openfootball_match,
    normalize_openfootball_season,
    normalize_openfootball_team,
    validate_openfootball_match,
)


__all__ = [
    "MatchStatus",
    "OpenFootballCatalog",
    "OpenFootballClub",
    "OpenFootballDataset",
    "OpenFootballError",
    "OpenFootballLeague",
    "OpenFootballMatch",
    "OpenFootballParseError",
    "OpenFootballPlayer",
    "ValidationResult",
    "ensure_valid_openfootball_match",
    "detect_openfootball_catalog_kind",
    "normalize_openfootball_season",
    "normalize_openfootball_team",
    "parse_football_txt",
    "parse_football_txt_data",
    "parse_openfootball_catalog",
    "parse_openfootball_catalog_data",
    "parse_openfootball_clubs_data",
    "parse_openfootball_json",
    "parse_openfootball_json_data",
    "parse_openfootball_leagues_data",
    "parse_openfootball_players_data",
    "sniff_openfootball_catalog_kind",
    "validate_openfootball_match",
]
