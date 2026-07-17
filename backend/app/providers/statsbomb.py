"""Strictly local StatsBomb Open Data reader.

This module contains no HTTP client and has no API-key concept.  It reads an
already downloaded ``statsbomb/open-data`` tree (or its ``data`` directory),
validates the JSON, and keeps a private raw copy so subsequent training runs are
independent of the original folder.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cache import CacheError, LocalFileCache, default_raw_cache_dir


MAX_JSON_BYTES = 100 * 1024 * 1024


class StatsBombProviderError(RuntimeError):
    """Base error raised by the local StatsBomb reader."""


class LocalDataUnavailableError(StatsBombProviderError, FileNotFoundError):
    """Neither the source folder nor the local raw cache has an artifact."""


class StatsBombResponseError(StatsBombProviderError):
    """A local StatsBomb JSON artifact is malformed or unexpectedly large."""


@dataclass(frozen=True, slots=True)
class ImportManifest:
    competition_id: int
    season_id: int
    matches: int
    lineups: int
    event_files: int
    errors: tuple[str, ...]
    lineups_requested: bool = True
    events_requested: bool = True

    @property
    def complete(self) -> bool:
        lineups_complete = not self.lineups_requested or self.lineups == self.matches
        events_complete = not self.events_requested or self.event_files == self.matches
        return not self.errors and lineups_complete and events_complete


DownloadManifest = ImportManifest  # compatibility name; no download is performed.


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_id(value: int | str, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f"{field} must be non-negative")
    return parsed


def _nested_name(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key in (
            "name",
            "competition_name",
            "season_name",
            "home_team_name",
            "away_team_name",
        ):
            if value.get(key) not in (None, ""):
                return str(value[key])
    if value in (None, ""):
        return None
    return str(value)


def _nested_id(value: Any, *keys: str) -> int | str | None:
    if isinstance(value, Mapping):
        for key in keys:
            if value.get(key) not in (None, ""):
                return value[key]
    return None


def _match_datetime(row: Mapping[str, Any]) -> str | None:
    raw_date = row.get("match_date")
    if not raw_date:
        return None
    raw_time = str(row.get("kick_off") or "00:00:00").strip()
    try:
        parsed = datetime.fromisoformat(
            f"{raw_date}T{raw_time}".replace("Z", "+00:00")
        )
    except ValueError:
        return str(raw_date)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def normalize_competition(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "external_id": row.get("competition_id"),
        "name": row.get("competition_name"),
        "country": row.get("country_name"),
        "season_external_id": row.get("season_id"),
        "season_name": row.get("season_name"),
        "competition_gender": row.get("competition_gender"),
        "competition_youth": bool(row.get("competition_youth", False)),
        "competition_international": bool(
            row.get("competition_international", False)
        ),
        "data_source": "statsbomb_open_data_local",
        "source_updated_at": row.get("match_updated") or row.get("match_available"),
        "is_mock_data": False,
    }


def normalize_match(row: Mapping[str, Any]) -> dict[str, Any]:
    competition = row.get("competition")
    season = row.get("season")
    home = row.get("home_team")
    away = row.get("away_team")
    stadium = row.get("stadium")
    referee = row.get("referee")
    return {
        "external_id": row.get("match_id"),
        "competition_external_id": _nested_id(
            competition, "competition_id", "id"
        ),
        "competition_name": _nested_name(competition),
        "season_external_id": _nested_id(season, "season_id", "id"),
        "season_name": _nested_name(season),
        "home_team_external_id": _nested_id(home, "home_team_id", "team_id", "id"),
        "home_team_name": _nested_name(home),
        "away_team_external_id": _nested_id(away, "away_team_id", "team_id", "id"),
        "away_team_name": _nested_name(away),
        "match_date": _match_datetime(row),
        "venue": _nested_name(stadium),
        "status": "FINISHED" if row.get("home_score") is not None else "SCHEDULED",
        "home_score": row.get("home_score"),
        "away_score": row.get("away_score"),
        "halftime_home_score": row.get("halftime_home_score"),
        "halftime_away_score": row.get("halftime_away_score"),
        "referee_external_id": _nested_id(referee, "id"),
        "referee_name": _nested_name(referee),
        "match_week": row.get("match_week"),
        "data_source": "statsbomb_open_data_local",
        "source_updated_at": row.get("last_updated_360")
        or row.get("last_updated"),
        "is_mock_data": False,
    }


def normalize_event(row: Mapping[str, Any]) -> dict[str, Any]:
    event_type = row.get("type")
    team = row.get("team")
    player = row.get("player")
    possession_team = row.get("possession_team")
    return {
        "external_id": row.get("id"),
        "match_external_id": row.get("match_id"),
        "index": row.get("index"),
        "period": row.get("period"),
        "minute": row.get("minute"),
        "second": row.get("second"),
        "timestamp": row.get("timestamp"),
        "event_type": _nested_name(event_type),
        "team_external_id": _nested_id(team, "id"),
        "team_name": _nested_name(team),
        "player_external_id": _nested_id(player, "id"),
        "player_name": _nested_name(player),
        "possession": row.get("possession"),
        "possession_team_external_id": _nested_id(possession_team, "id"),
        "location": row.get("location"),
        "data_source": "statsbomb_open_data_local",
        "source_updated_at": None,
        "is_mock_data": False,
        "provider_payload": dict(row),
    }


class StatsBombLocalProvider:
    """Read a local StatsBomb Open Data tree and populate an atomic raw cache."""

    def __init__(
        self,
        source_dir: str | Path | None = None,
        *,
        cache_dir: str | Path | None = None,
    ) -> None:
        self.cache = LocalFileCache(cache_dir or default_raw_cache_dir() / "statsbomb")
        self.source_dir = self._normalize_source_root(source_dir) if source_dir else None

    @staticmethod
    def _normalize_source_root(source_dir: str | Path) -> Path:
        supplied = Path(source_dir).expanduser().resolve(strict=True)
        if not supplied.is_dir():
            raise NotADirectoryError(f"StatsBomb source is not a directory: {supplied}")
        data_child = supplied / "data"
        if data_child.is_dir() and (data_child / "competitions.json").is_file():
            return data_child.resolve()
        return supplied

    def set_source_dir(self, source_dir: str | Path) -> None:
        self.source_dir = self._normalize_source_root(source_dir)

    def _safe_source_file(self, relative_path: str) -> Path | None:
        if self.source_dir is None:
            return None
        candidate = (self.source_dir / relative_path).resolve()
        try:
            candidate.relative_to(self.source_dir)
        except ValueError as exc:  # pragma: no cover - relative paths are internal
            raise StatsBombProviderError("StatsBomb path escaped the source root") from exc
        return candidate

    @staticmethod
    def _parse(payload: bytes, resource: str) -> list[dict[str, Any]]:
        if len(payload) > MAX_JSON_BYTES:
            raise StatsBombResponseError(f"{resource} exceeds the local size limit")
        try:
            parsed = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StatsBombResponseError(f"Invalid local JSON for {resource}") from exc
        if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
            raise StatsBombResponseError(f"{resource} must be a JSON array of objects")
        return parsed

    def _load(self, relative_path: str, cache_key: str, *, refresh: bool) -> list[dict[str, Any]]:
        if not refresh:
            try:
                cached = self.cache.get_bytes(
                    cache_key, extension="json", max_bytes=MAX_JSON_BYTES
                )
            except CacheError as exc:
                raise StatsBombResponseError(str(exc)) from exc
            if cached is not None:
                return self._parse(cached, relative_path)

        source = self._safe_source_file(relative_path)
        if source is None or not source.is_file():
            raise LocalDataUnavailableError(
                f"Missing local StatsBomb artifact {relative_path!r}; "
                "provide the statsbomb/open-data folder or import it first"
            )
        size = source.stat().st_size
        if size > MAX_JSON_BYTES:
            raise StatsBombResponseError(f"{relative_path} exceeds the local size limit")
        payload = source.read_bytes()
        rows = self._parse(payload, relative_path)
        self.cache.put_bytes(cache_key, payload, extension="json")
        return rows

    def get_competitions(
        self, *, normalize: bool = False, refresh: bool = False
    ) -> list[dict[str, Any]]:
        rows = self._load("competitions.json", "competitions", refresh=refresh)
        return [normalize_competition(row) for row in rows] if normalize else rows

    def get_matches(
        self,
        competition_id: int | str,
        season_id: int | str,
        *,
        normalize: bool = False,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        competition = _positive_id(competition_id, "competition_id")
        season = _positive_id(season_id, "season_id")
        rows = self._load(
            f"matches/{competition}/{season}.json",
            f"matches/{competition}/{season}",
            refresh=refresh,
        )
        return [normalize_match(row) for row in rows] if normalize else rows

    def get_lineups(
        self, match_id: int | str, *, refresh: bool = False
    ) -> list[dict[str, Any]]:
        match = _positive_id(match_id, "match_id")
        return self._load(
            f"lineups/{match}.json", f"lineups/{match}", refresh=refresh
        )

    def get_events(
        self,
        match_id: int | str,
        *,
        normalize: bool = False,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        match = _positive_id(match_id, "match_id")
        rows = self._load(
            f"events/{match}.json", f"events/{match}", refresh=refresh
        )
        if not normalize:
            return rows
        output: list[dict[str, Any]] = []
        for row in rows:
            item = normalize_event(row)
            item["match_external_id"] = match
            output.append(item)
        return output

    def import_competition(
        self,
        competition_id: int | str,
        season_id: int | str,
        *,
        include_lineups: bool = True,
        include_events: bool = True,
        fail_fast: bool = False,
    ) -> ImportManifest:
        """Copy one local competition into the private cache with a manifest."""

        competition = _positive_id(competition_id, "competition_id")
        season = _positive_id(season_id, "season_id")
        matches = self.get_matches(competition, season, refresh=True)
        lineup_count = 0
        event_count = 0
        errors: list[str] = []
        for row in matches:
            raw_match_id = row.get("match_id")
            try:
                match = _positive_id(raw_match_id, "match_id")
                if include_lineups:
                    self.get_lineups(match, refresh=True)
                    lineup_count += 1
                if include_events:
                    self.get_events(match, refresh=True)
                    event_count += 1
            except (StatsBombProviderError, ValueError) as exc:
                errors.append(f"match_id={raw_match_id!r}: {exc}")
                if fail_fast:
                    raise
        manifest = ImportManifest(
            competition_id=competition,
            season_id=season,
            matches=len(matches),
            lineups=lineup_count,
            event_files=event_count,
            errors=tuple(errors),
            lineups_requested=include_lineups,
            events_requested=include_events,
        )
        self.cache.put_json(
            f"manifests/{competition}/{season}",
            {
                "competition_id": competition,
                "season_id": season,
                "matches": manifest.matches,
                "lineups": manifest.lineups,
                "event_files": manifest.event_files,
                "errors": list(manifest.errors),
                "lineups_requested": manifest.lineups_requested,
                "events_requested": manifest.events_requested,
                "generated_at": _utcnow(),
                "source": "local_folder",
            },
        )
        return manifest


StatsBombOpenDataProvider = StatsBombLocalProvider
StatsBombProvider = StatsBombLocalProvider


__all__ = [
    "DownloadManifest",
    "ImportManifest",
    "LocalDataUnavailableError",
    "StatsBombLocalProvider",
    "StatsBombOpenDataProvider",
    "StatsBombProvider",
    "StatsBombProviderError",
    "StatsBombResponseError",
    "normalize_competition",
    "normalize_event",
    "normalize_match",
]
