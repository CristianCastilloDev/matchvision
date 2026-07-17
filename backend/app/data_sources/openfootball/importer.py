"""Offline OpenFootball dataset detection and repository orchestration."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from app.services.entity_resolution import normalize_entity_name

from .catalog_parser import (
    detect_openfootball_catalog_kind,
    sniff_openfootball_catalog_kind,
)
from .football_txt_parser import parse_football_txt_data
from .json_parser import parse_openfootball_json_data
from .validators import (
    normalize_openfootball_season,
    normalize_openfootball_team,
    validate_openfootball_match,
)


ALLOWED_SUFFIXES = {".json", ".txt"}
IGNORED_PARTS = {".git", "node_modules", "test", "tests", "spec", "specs"}
IGNORED_FILENAMES = {
    "package.json",
    "package-lock.json",
    "composer.json",
    "bower.json",
    "tsconfig.json",
}
MAX_FILES = 5000
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TOTAL_BYTES = 250 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 100

KNOWN_REPOSITORIES = (
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

COUNTRY_HINTS = {
    "england": "England",
    "espana": "Spain",
    "spain": "Spain",
    "deutschland": "Germany",
    "germany": "Germany",
    "italy": "Italy",
    "mexico": "Mexico",
    "usa": "United States",
    "united-states": "United States",
    "japan": "Japan",
    "argentina": "Argentina",
    "brazil": "Brazil",
    "brasil": "Brazil",
}


class OpenFootballImportError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OpenFootballFileError:
    source_file: str
    code: str
    message: str
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DetectedOpenFootballDataset:
    dataset_type: str
    source_repository: str
    country: str | None
    root_name: str
    files: tuple[str, ...]
    total_bytes: int
    content_hash: str

    @property
    def files_scanned(self) -> int:
        return len(self.files)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["files_scanned"] = self.files_scanned
        payload.pop("files")
        payload["sample_files"] = list(self.files[:25])
        return payload


@dataclass(frozen=True, slots=True)
class OpenFootballRepositoryResult:
    detection: DetectedOpenFootballDataset
    datasets: tuple[Any, ...]
    matches: tuple[Any, ...]
    errors: tuple[OpenFootballFileError, ...]
    warnings: tuple[str, ...]
    metrics: dict[str, int]
    quality_by_competition: tuple[dict[str, Any], ...]

    def to_dict(self, *, preview_limit: int = 50) -> dict[str, Any]:
        return {
            "detection": {
                **self.detection.to_dict(),
                "competitions": sorted(
                    {str(_value(match, "competition")) for match in self.matches if _value(match, "competition")}
                ),
                "seasons": sorted(
                    {str(_value(match, "season")) for match in self.matches if _value(match, "season")}
                ),
            },
            "metrics": dict(self.metrics),
            "preview_matches": [serialize_openfootball_match(match) for match in self.matches[:preview_limit]],
            "quality_by_competition": list(self.quality_by_competition),
            "errors": [error.to_dict() for error in self.errors],
            "warnings": list(self.warnings),
        }


def _safe_member(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    member = PurePosixPath(normalized)
    if member.is_absolute() or ".." in member.parts or not member.parts:
        raise OpenFootballImportError(f"Unsafe ZIP member path: {name!r}")
    return member


def _repository_hint(values: Iterable[str]) -> str:
    normalized_values = [value.casefold().replace("_", "-") for value in values]
    for repository in KNOWN_REPOSITORIES:
        token = repository.casefold()
        if any(token in value for value in normalized_values):
            return repository
    return "openfootball-local"


def _country_hint(values: Iterable[str]) -> str | None:
    text = "/".join(values).casefold().replace("_", "-")
    for token, country in COUNTRY_HINTS.items():
        if re.search(rf"(?:^|[/.-]){re.escape(token)}(?:$|[/.-])", text):
            return country
    return None


def _directory_files(root: Path) -> list[Path]:
    if root.is_symlink():
        raise OpenFootballImportError("Dataset root cannot be a symbolic link")
    root_resolved = root.resolve(strict=True)
    files: list[Path] = []
    total = 0
    for path in sorted(root.rglob("*")):
        relative_unresolved = path.relative_to(root)
        if (
            any(part.casefold() in IGNORED_PARTS for part in relative_unresolved.parts)
            or relative_unresolved.name.casefold() in IGNORED_FILENAMES
        ):
            continue
        if path.is_symlink():
            raise OpenFootballImportError(f"Symbolic links are not supported: {path}")
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(root_resolved)
        except ValueError as exc:
            raise OpenFootballImportError(f"Dataset path escapes its root: {path}") from exc
        if not resolved.is_file() or resolved.suffix.casefold() not in ALLOWED_SUFFIXES:
            continue
        relative = resolved.relative_to(root_resolved)
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise OpenFootballImportError(f"File exceeds size limit: {relative}")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise OpenFootballImportError("Dataset exceeds aggregate size limit")
        files.append(resolved)
        if len(files) > MAX_FILES:
            raise OpenFootballImportError("Dataset contains too many files")
    return files


def _zip_infos(path: Path) -> list[zipfile.ZipInfo]:
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise OpenFootballImportError("Invalid OpenFootball ZIP") from exc
    infos: list[zipfile.ZipInfo] = []
    total = 0
    for info in archive.infolist():
        member = _safe_member(info.filename)
        if (
            info.is_dir()
            or member.suffix.casefold() not in ALLOWED_SUFFIXES
            or any(part.casefold() in IGNORED_PARTS for part in member.parts)
            or member.name.casefold() in IGNORED_FILENAMES
        ):
            continue
        file_type = (info.external_attr >> 16) & 0o170000
        if file_type == 0o120000 or info.flag_bits & 0x1:
            raise OpenFootballImportError(f"Unsupported encrypted/symlink member: {info.filename}")
        if info.file_size > MAX_FILE_BYTES:
            raise OpenFootballImportError(f"ZIP member exceeds size limit: {info.filename}")
        if info.file_size / max(info.compress_size, 1) > MAX_ZIP_COMPRESSION_RATIO:
            raise OpenFootballImportError(f"Unsafe ZIP compression ratio: {info.filename}")
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise OpenFootballImportError("ZIP expands beyond aggregate size limit")
        infos.append(info)
        if len(infos) > MAX_FILES:
            raise OpenFootballImportError("ZIP contains too many dataset files")
    archive.close()
    return infos


def detect_openfootball_dataset(path: str | Path) -> DetectedOpenFootballDataset:
    unresolved = Path(path).expanduser()
    if unresolved.is_symlink():
        raise OpenFootballImportError("Dataset root cannot be a symbolic link")
    source = unresolved.resolve(strict=True)
    digest = hashlib.sha256()
    if source.is_dir():
        files = _directory_files(source)
        if not files:
            raise OpenFootballImportError("No OpenFootball JSON/TXT files found")
        relative_names = [path.relative_to(source).as_posix() for path in files]
        total = 0
        for file in files:
            payload = file.read_bytes()
            total += len(payload)
            digest.update(file.relative_to(source).as_posix().encode())
            digest.update(payload)
        kind = "repository"
        hints = [*source.parts[-6:], *relative_names]
    elif source.is_file() and source.suffix.casefold() == ".zip":
        infos = _zip_infos(source)
        if not infos:
            raise OpenFootballImportError("ZIP contains no OpenFootball JSON/TXT files")
        relative_names = [PurePosixPath(info.filename).as_posix() for info in infos]
        payload = source.read_bytes()
        total = sum(info.file_size for info in infos)
        digest.update(payload)
        kind = "zip"
        hints = [*source.parts[-6:], source.stem, *relative_names]
    elif source.is_file() and source.suffix.casefold() in ALLOWED_SUFFIXES:
        payload = source.read_bytes()
        if len(payload) > MAX_FILE_BYTES:
            raise OpenFootballImportError("OpenFootball file exceeds size limit")
        relative_names = [source.name]
        total = len(payload)
        digest.update(payload)
        kind = "json" if source.suffix.casefold() == ".json" else "football_txt"
        hints = [*source.parts[-6:], source.name]
    else:
        raise OpenFootballImportError("Expected a folder, ZIP, JSON or Football.TXT file")
    return DetectedOpenFootballDataset(
        dataset_type=kind,
        source_repository=_repository_hint(hints),
        country=_country_hint(hints),
        root_name=source.name,
        files=tuple(relative_names),
        total_bytes=total,
        content_hash=digest.hexdigest(),
    )


def _value(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def serialize_openfootball_match(
    record: Any, *, include_raw: bool = False
) -> dict[str, Any]:
    if hasattr(record, "to_dict"):
        try:
            payload = record.to_dict(include_raw=include_raw)
        except TypeError:
            payload = record.to_dict()
    elif is_dataclass(record):
        payload = asdict(record)
    elif isinstance(record, dict):
        payload = dict(record)
    else:
        payload = dict(vars(record))
    if not include_raw:
        payload.pop("raw_payload", None)
    for key, value in list(payload.items()):
        if hasattr(value, "isoformat"):
            payload[key] = value.isoformat()
    return payload


def _metrics(matches: list[Any], errors: list[OpenFootballFileError], files: int) -> dict[str, int]:
    statuses = [str(_value(match, "status", "unknown")).casefold() for match in matches]
    teams = {
        normalize_openfootball_team(str(value))
        for match in matches
        for value in (_value(match, "home_team"), _value(match, "away_team"))
        if value
    }
    competitions = {str(_value(match, "competition")) for match in matches if _value(match, "competition")}
    return {
        "files_scanned": files,
        "matches_found": len(matches),
        "finished_matches": statuses.count("finished"),
        "scheduled_matches": statuses.count("scheduled"),
        "teams_found": len(teams),
        "competitions_found": len(competitions),
        "duplicates": 0,
        "conflicts": 0,
        "errors": len(errors),
    }


def _quality(matches: list[Any], repository: str) -> tuple[dict[str, Any], ...]:
    grouped: dict[str, list[Any]] = {}
    for match in matches:
        competition = str(_value(match, "competition") or "Unknown competition")
        grouped.setdefault(competition, []).append(match)
    output: list[dict[str, Any]] = []
    for competition, records in sorted(grouped.items()):
        dates = [str(_value(record, "date")) for record in records if _value(record, "date")]
        statuses = [str(_value(record, "status", "unknown")).casefold() for record in records]
        seasons = sorted({str(_value(record, "season")) for record in records if _value(record, "season")})
        fields = sorted(
            {
                field
                for record in records
                for field, value in serialize_openfootball_match(record).items()
                if value not in (None, "", [], {})
            }
        )
        output.append(
            {
                "competition": competition,
                "source_repository": repository,
                "first_match_date": min(dates) if dates else None,
                "last_match_date": max(dates) if dates else None,
                "total_matches": len(records),
                "finished_matches": statuses.count("finished"),
                "scheduled_matches": statuses.count("scheduled"),
                "seasons_available": seasons,
                "fields_available": fields,
                "last_imported_at": None,
            }
        )
    return tuple(output)


def _dataset_matches(dataset: Any) -> list[Any]:
    value = dataset.get("matches", []) if isinstance(dataset, dict) else getattr(dataset, "matches", [])
    return list(value or [])


def _parse_payload(
    name: str,
    payload: bytes,
    *,
    repository: str,
    competition: str | None,
    season: str | None,
) -> Any:
    suffix = PurePosixPath(name).suffix.casefold()
    if suffix == ".json":
        return parse_openfootball_json_data(
            payload,
            source_file=name,
            competition=competition,
            season=season,
            source_repository=repository,
        )
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = payload.decode("cp1252")
    return parse_football_txt_data(
        text,
        source_file=name,
        competition=competition,
        season=season,
        source_repository=repository,
    )


def import_openfootball_repository(
    path: str | Path,
    *,
    competition: str | None = None,
    season: str | None = None,
) -> OpenFootballRepositoryResult:
    unresolved = Path(path).expanduser()
    if unresolved.is_symlink():
        raise OpenFootballImportError("Dataset root cannot be a symbolic link")
    source = unresolved.resolve(strict=True)
    detection = detect_openfootball_dataset(source)
    datasets: list[Any] = []
    matches: list[Any] = []
    errors: list[OpenFootballFileError] = []
    warnings: list[str] = []
    catalog_fallback = detect_openfootball_catalog_kind(
        source.stem if source.suffix.casefold() == ".zip" else source.name
    )

    def consume_dataset(name: str, dataset: Any) -> None:
        datasets.append(dataset)
        warnings.extend(list(_value(dataset, "warnings", ()) or ()))
        for record in _dataset_matches(dataset):
            validation = validate_openfootball_match(record)
            line = _value(record, "source_line")
            if not validation.valid:
                errors.append(
                    OpenFootballFileError(
                        name,
                        "validation_error",
                        "; ".join(validation.errors),
                        line=int(line) if line is not None else None,
                    )
                )
                continue
            matches.append(record)
            warnings.extend(
                f"{name}{f':{line}' if line is not None else ''}: {message}"
                for message in validation.warnings
            )

    def consume(name: str, payload: bytes) -> None:
        if PurePosixPath(name).suffix.casefold() == ".txt":
            catalog_kind = detect_openfootball_catalog_kind(name, catalog_fallback)
            if catalog_kind is None:
                try:
                    catalog_text = payload.decode("utf-8-sig")
                except UnicodeDecodeError:
                    catalog_text = payload.decode("cp1252")
                catalog_kind = sniff_openfootball_catalog_kind(catalog_text)
            if catalog_kind is not None:
                return
        try:
            dataset = _parse_payload(
                name,
                payload,
                repository=detection.source_repository,
                competition=competition,
                season=season,
            )
            consume_dataset(name, dataset)
        except Exception as exc:
            errors.append(OpenFootballFileError(name, "parse_error", str(exc)))

    if source.is_dir():
        for file in _directory_files(source):
            consume(file.relative_to(source).as_posix(), file.read_bytes())
    elif source.suffix.casefold() == ".zip":
        with zipfile.ZipFile(source) as archive:
            for info in _zip_infos(source):
                consume(PurePosixPath(info.filename).as_posix(), archive.read(info))
    elif source.suffix.casefold() == ".json":
        dataset = parse_openfootball_json_data(
            source.read_bytes(),
            source_file=source.name,
            competition=competition,
            season=season,
            source_repository=detection.source_repository,
        )
        consume_dataset(source.name, dataset)
    else:
        payload = source.read_bytes()
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = payload.decode("cp1252")
        dataset = parse_football_txt_data(
            text,
            source_file=source.name,
            competition=competition,
            season=season,
            source_repository=detection.source_repository,
        )
        consume_dataset(source.name, dataset)

    if competition:
        token = normalize_entity_name(competition)
        matches = [
            match
            for match in matches
            if token in normalize_entity_name(str(_value(match, "competition") or ""))
            or token in normalize_entity_name(str(_value(match, "source_file") or ""))
        ]
    if season:
        token = normalize_entity_name(normalize_openfootball_season(season) or season)
        matches = [
            match
            for match in matches
            if token
            == normalize_entity_name(
                normalize_openfootball_season(str(_value(match, "season") or "")) or ""
            )
        ]
    return OpenFootballRepositoryResult(
        detection=detection,
        datasets=tuple(datasets),
        matches=tuple(matches),
        errors=tuple(errors),
        warnings=tuple(dict.fromkeys(warnings)),
        metrics=_metrics(matches, errors, detection.files_scanned),
        quality_by_competition=_quality(matches, detection.source_repository),
    )


def import_openfootball_season(
    path: str | Path, competition: str, season: str
) -> OpenFootballRepositoryResult:
    return import_openfootball_repository(path, competition=competition, season=season)


def validate_openfootball_path(path: str | Path) -> dict[str, Any]:
    result = import_openfootball_repository(path)
    return {
        "valid": not result.errors and bool(result.matches),
        **result.to_dict(preview_limit=0),
    }


__all__ = [
    "DetectedOpenFootballDataset",
    "OpenFootballFileError",
    "OpenFootballImportError",
    "OpenFootballRepositoryResult",
    "detect_openfootball_dataset",
    "import_openfootball_repository",
    "import_openfootball_season",
    "serialize_openfootball_match",
    "validate_openfootball_path",
]
