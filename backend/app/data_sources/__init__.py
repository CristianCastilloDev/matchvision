"""Compatibility namespace for strictly local historical data sources."""

from app.providers.football_data import FootballDataLocalProvider
from app.providers.statsbomb import StatsBombLocalProvider

__all__ = ["FootballDataLocalProvider", "StatsBombLocalProvider"]
