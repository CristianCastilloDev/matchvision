"""Offline data-provider adapters exposed by MatchVision AI."""

from .cache import CacheEntry, CacheError, LocalFileCache, UnsafeCacheKeyError
from .football_data import (
    ColumnReport,
    FootballDataCoUkProvider,
    FootballDataDataset,
    FootballDataFormatError,
    FootballDataLocalProvider,
    FootballDataProvider,
    detect_columns,
    normalize_records as normalize_football_data_records,
)
from .statsbomb import (
    ImportManifest,
    LocalDataUnavailableError,
    StatsBombLocalProvider,
    StatsBombOpenDataProvider,
    StatsBombProvider,
    StatsBombProviderError,
    normalize_competition as normalize_statsbomb_competition,
    normalize_event as normalize_statsbomb_event,
    normalize_match as normalize_statsbomb_match,
)


__all__ = [
    "CacheEntry",
    "CacheError",
    "ColumnReport",
    "FootballDataCoUkProvider",
    "FootballDataDataset",
    "FootballDataFormatError",
    "FootballDataLocalProvider",
    "FootballDataProvider",
    "ImportManifest",
    "LocalDataUnavailableError",
    "LocalFileCache",
    "StatsBombLocalProvider",
    "StatsBombOpenDataProvider",
    "StatsBombProvider",
    "StatsBombProviderError",
    "UnsafeCacheKeyError",
    "detect_columns",
    "normalize_football_data_records",
    "normalize_statsbomb_competition",
    "normalize_statsbomb_event",
    "normalize_statsbomb_match",
]
