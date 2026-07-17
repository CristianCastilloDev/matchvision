"""Small, dependency-free local cache used by data providers.

The cache deliberately accepts *keys*, not arbitrary relative paths.  Every key
component is validated before touching the filesystem, which makes the class safe
to use from CLI/API adapters without accidentally allowing path traversal.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class CacheError(RuntimeError):
    """Base error for provider cache failures."""


class UnsafeCacheKeyError(CacheError, ValueError):
    """Raised when a key could escape or otherwise abuse the cache root."""


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """Metadata for one locally cached artifact."""

    path: Path
    size_bytes: int
    modified_at: datetime

    def is_fresh(self, max_age: timedelta | None) -> bool:
        if max_age is None:
            return True
        now = datetime.now(timezone.utc)
        return now - self.modified_at <= max_age


def default_raw_cache_dir() -> Path:
    """Return the configured raw-data directory without creating it."""

    configured = os.getenv("DATA_CACHE_DIR", "data/cache")
    return Path(configured).expanduser() / "raw"


class LocalFileCache:
    """Atomic byte/JSON cache constrained to a single root directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()

    @staticmethod
    def _parts(key: str) -> tuple[str, ...]:
        if not isinstance(key, str) or not key.strip():
            raise UnsafeCacheKeyError("Cache key must be a non-empty string")
        normalized = key.replace("\\", "/").strip("/")
        parts = tuple(normalized.split("/"))
        if not parts or any(
            part in {"", ".", ".."} or _SAFE_COMPONENT.fullmatch(part) is None
            for part in parts
        ):
            raise UnsafeCacheKeyError(f"Unsafe cache key: {key!r}")
        return parts

    @staticmethod
    def _extension(extension: str) -> str:
        value = extension.lstrip(".")
        if not value or not value.isalnum() or len(value) > 12:
            raise UnsafeCacheKeyError(f"Unsafe cache extension: {extension!r}")
        return value.lower()

    def path_for(self, key: str, *, extension: str) -> Path:
        parts = self._parts(key)
        ext = self._extension(extension)
        return self.root.joinpath(*parts[:-1], f"{parts[-1]}.{ext}")

    def entry(self, key: str, *, extension: str) -> CacheEntry | None:
        path = self.path_for(key, extension=extension)
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        return CacheEntry(
            path=path,
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        )

    def get_bytes(
        self,
        key: str,
        *,
        extension: str,
        max_age: timedelta | None = None,
        max_bytes: int | None = None,
    ) -> bytes | None:
        entry = self.entry(key, extension=extension)
        if entry is None or not entry.is_fresh(max_age):
            return None
        if max_bytes is not None and entry.size_bytes > max_bytes:
            raise CacheError(
                f"Cached artifact {key!r} exceeds the {max_bytes}-byte limit"
            )
        return entry.path.read_bytes()

    def put_bytes(self, key: str, payload: bytes, *, extension: str) -> CacheEntry:
        if not isinstance(payload, bytes):
            raise TypeError("Cache payload must be bytes")
        path = self.path_for(key, extension=extension)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        entry = self.entry(key, extension=extension)
        if entry is None:  # pragma: no cover - defensive filesystem guard
            raise CacheError(f"Could not persist cache artifact {key!r}")
        return entry

    def get_json(
        self,
        key: str,
        *,
        max_age: timedelta | None = None,
        max_bytes: int | None = None,
    ) -> Any | None:
        payload = self.get_bytes(
            key, extension="json", max_age=max_age, max_bytes=max_bytes
        )
        if payload is None:
            return None
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CacheError(f"Invalid JSON cached for {key!r}") from exc

    def put_json(self, key: str, payload: Any) -> CacheEntry:
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CacheError(f"Payload for {key!r} is not JSON serializable") from exc
        return self.put_bytes(key, encoded, extension="json")


__all__ = [
    "CacheEntry",
    "CacheError",
    "LocalFileCache",
    "UnsafeCacheKeyError",
    "default_raw_cache_dir",
]
