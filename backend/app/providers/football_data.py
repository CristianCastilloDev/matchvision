"""Local Football-Data CSV/ZIP importer and schema normalizer.

There is intentionally no downloader in this module.  A user supplies a local
CSV (or a ZIP containing CSV files) and the importer preserves the original bytes
before transforming any rows.
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path, PurePosixPath
from typing import Any
from zipfile import BadZipFile, ZipFile, ZipInfo

from .cache import LocalFileCache, default_raw_cache_dir


MAX_CSV_BYTES = 25 * 1024 * 1024
MAX_ZIP_BYTES = 75 * 1024 * 1024
MAX_ZIP_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_ZIP_MEMBERS = 20
MAX_COMPRESSION_RATIO = 100


COLUMN_MAP: dict[str, str] = {
    "Div": "provider_competition_code",
    "Date": "match_date",
    "Time": "match_time",
    "HomeTeam": "home_team_name",
    "AwayTeam": "away_team_name",
    "FTHG": "home_goals",
    "FTAG": "away_goals",
    "FTR": "fulltime_result",
    "HTHG": "halftime_home_goals",
    "HTAG": "halftime_away_goals",
    "HTR": "halftime_result",
    "HS": "home_shots",
    "AS": "away_shots",
    "HST": "home_shots_on_target",
    "AST": "away_shots_on_target",
    "HC": "home_corners",
    "AC": "away_corners",
    "HF": "home_fouls",
    "AF": "away_fouls",
    "HY": "home_yellow_cards",
    "AY": "away_yellow_cards",
    "HR": "home_red_cards",
    "AR": "away_red_cards",
    "Referee": "referee_name",
}

REQUIRED_COLUMNS = frozenset({"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"})
ODDS_COLUMNS = frozenset(
    {
        "B365H",
        "B365D",
        "B365A",
        "PSH",
        "PSD",
        "PSA",
        "AvgH",
        "AvgD",
        "AvgA",
        "MaxH",
        "MaxD",
        "MaxA",
    }
)
INTEGER_FIELDS = frozenset(
    {
        "home_goals",
        "away_goals",
        "halftime_home_goals",
        "halftime_away_goals",
        "home_shots",
        "away_shots",
        "home_shots_on_target",
        "away_shots_on_target",
        "home_corners",
        "away_corners",
        "home_fouls",
        "away_fouls",
        "home_yellow_cards",
        "away_yellow_cards",
        "home_red_cards",
        "away_red_cards",
    }
)


class FootballDataError(RuntimeError):
    """Base error for local Football-Data imports."""


class FootballDataFormatError(FootballDataError, ValueError):
    """The supplied CSV/ZIP does not have a safe, usable format."""


@dataclass(frozen=True, slots=True)
class ColumnReport:
    raw_columns: tuple[str, ...]
    available_variables: tuple[str, ...]
    missing_variables: tuple[str, ...]
    missing_required_columns: tuple[str, ...]
    odds_columns: tuple[str, ...]

    @property
    def usable(self) -> bool:
        return not self.missing_required_columns

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_columns": list(self.raw_columns),
            "available_variables": list(self.available_variables),
            "missing_variables": list(self.missing_variables),
            "missing_required_columns": list(self.missing_required_columns),
            "odds_columns": list(self.odds_columns),
            "usable": self.usable,
        }


@dataclass(frozen=True, slots=True)
class FootballDataDataset:
    records: tuple[dict[str, Any], ...]
    column_report: ColumnReport
    competition: str
    season: str
    source_hash: str
    source_files: tuple[str, ...]
    rejected_rows: int
    warnings: tuple[str, ...]

    @property
    def imported_rows(self) -> int:
        return len(self.records)

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": list(self.records),
            "column_report": self.column_report.to_dict(),
            "competition": self.competition,
            "season": self.season,
            "source_hash": self.source_hash,
            "source_files": list(self.source_files),
            "imported_rows": self.imported_rows,
            "rejected_rows": self.rejected_rows,
            "warnings": list(self.warnings),
            "data_source": "football_data_local_file",
        }


def detect_columns(columns: Iterable[str]) -> ColumnReport:
    cleaned = tuple(dict.fromkeys(str(column).strip() for column in columns if column))
    raw = set(cleaned)
    available = tuple(sorted(COLUMN_MAP[column] for column in raw if column in COLUMN_MAP))
    missing = tuple(sorted(set(COLUMN_MAP.values()) - set(available)))
    missing_required = tuple(sorted(REQUIRED_COLUMNS - raw))
    odds = tuple(sorted(raw & ODDS_COLUMNS))
    return ColumnReport(cleaned, available, missing, missing_required, odds)


def _decode_csv(payload: bytes) -> str:
    if len(payload) > MAX_CSV_BYTES:
        raise FootballDataFormatError("CSV exceeds the local size limit")
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise FootballDataFormatError("CSV encoding is not supported")


def _parse_csv(payload: bytes) -> tuple[list[dict[str, str]], ColumnReport]:
    text = _decode_csv(payload)
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise FootballDataFormatError("CSV has no header")
    normalized_headers = [str(value).strip() for value in reader.fieldnames]
    report = detect_columns(normalized_headers)
    rows: list[dict[str, str]] = []
    for raw in reader:
        row = {
            str(key).strip(): str(value).strip()
            for key, value in raw.items()
            if key is not None and value not in (None, "")
        }
        if row:
            rows.append(row)
    return rows, report


def _parse_number(value: Any, *, integer: bool) -> int | float | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return None
    if number < 0:
        return None
    if integer:
        return int(number) if number.is_integer() else None
    return number


def _parse_date(value: str, time_value: str | None) -> str | None:
    value = value.strip()
    formats = ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d-%m-%y")
    parsed_date = None
    for fmt in formats:
        try:
            parsed_date = datetime.strptime(value, fmt)
            break
        except ValueError:
            continue
    if parsed_date is None:
        return None
    if time_value:
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                parsed_time = datetime.strptime(time_value.strip(), fmt).time()
                parsed_date = datetime.combine(parsed_date.date(), parsed_time)
                break
            except ValueError:
                continue
    return parsed_date.replace(tzinfo=timezone.utc).isoformat()


def _external_id(
    competition: str, season: str, match_date: str, home: str, away: str
) -> str:
    identity = "|".join((competition, season, match_date, home, away)).casefold()
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def normalize_row(
    row: Mapping[str, Any],
    *,
    competition: str,
    season: str,
    source_updated_at: str,
    source_file: str,
    row_number: int,
) -> dict[str, Any] | None:
    """Normalize one row; return ``None`` when its identity is incomplete."""

    home = str(row.get("HomeTeam") or "").strip()
    away = str(row.get("AwayTeam") or "").strip()
    match_date = _parse_date(
        str(row.get("Date") or ""), str(row.get("Time") or "") or None
    )
    if not home or not away or match_date is None:
        return None

    normalized: dict[str, Any] = {
        "external_id": _external_id(competition, season, match_date, home, away),
        "competition_name": competition,
        "season_name": season,
        "provider_competition_code": row.get("Div"),
        "match_date": match_date,
        "home_team_name": home,
        "away_team_name": away,
        "data_source": "football_data_local_file",
        "source_updated_at": source_updated_at,
        "is_mock_data": False,
        "source_file": source_file,
        "source_row_number": row_number,
    }
    invalid_numeric_fields: list[str] = []
    for source_name, target_name in COLUMN_MAP.items():
        if target_name in {
            "match_date",
            "match_time",
            "home_team_name",
            "away_team_name",
            "provider_competition_code",
        }:
            continue
        value = row.get(source_name)
        if target_name in INTEGER_FIELDS:
            parsed_number = _parse_number(value, integer=True)
            normalized[target_name] = parsed_number
            if value not in (None, "") and parsed_number is None:
                invalid_numeric_fields.append(source_name)
        elif value not in (None, ""):
            normalized[target_name] = str(value).strip()

    odds: dict[str, float] = {}
    for column in ODDS_COLUMNS:
        value = _parse_number(row.get(column), integer=False)
        if isinstance(value, float | int):
            odds[column] = float(value)
    normalized["historical_odds"] = odds or None

    home_goals = normalized.get("home_goals")
    away_goals = normalized.get("away_goals")
    if not isinstance(home_goals, int) or not isinstance(away_goals, int):
        return None
    normalized["status"] = "FINISHED"
    normalized["provider_invalid_fields"] = invalid_numeric_fields
    for side in ("home", "away"):
        components = (
            normalized.get(f"{side}_yellow_cards"),
            normalized.get(f"{side}_red_cards"),
        )
        normalized[f"{side}_cards"] = (
            sum(value for value in components if isinstance(value, int))
            if any(isinstance(value, int) for value in components)
            else None
        )
    return normalized


def normalize_records(
    rows: Iterable[Mapping[str, Any]],
    *,
    competition: str,
    season: str,
    source_updated_at: str,
    source_file: str,
) -> tuple[list[dict[str, Any]], int]:
    output: list[dict[str, Any]] = []
    rejected = 0
    for number, row in enumerate(rows, start=2):
        item = normalize_row(
            row,
            competition=competition,
            season=season,
            source_updated_at=source_updated_at,
            source_file=source_file,
            row_number=number,
        )
        if item is None:
            rejected += 1
        else:
            output.append(item)
    return output, rejected


def _safe_token(value: str, label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.")
    if not cleaned:
        raise ValueError(f"{label} must contain letters or digits")
    return cleaned[:80]


def _safe_zip_member(info: ZipInfo) -> bool:
    path = PurePosixPath(info.filename.replace("\\", "/"))
    if info.is_dir() or path.is_absolute() or ".." in path.parts:
        return False
    # Unix symlink bits, when present in a ZIP created on Unix.
    file_type = (info.external_attr >> 16) & 0o170000
    if file_type == 0o120000 or info.flag_bits & 0x1:
        return False
    return path.suffix.casefold() == ".csv"


class FootballDataLocalProvider:
    """Import Football-Data files supplied explicitly by the user."""

    def __init__(self, *, cache_dir: str | Path | None = None) -> None:
        self.cache = LocalFileCache(
            cache_dir or default_raw_cache_dir() / "football_data"
        )

    @staticmethod
    def _csv_payloads(path: Path) -> list[tuple[str, bytes]]:
        suffix = path.suffix.casefold()
        if suffix == ".csv":
            if path.stat().st_size > MAX_CSV_BYTES:
                raise FootballDataFormatError("CSV exceeds the local size limit")
            return [(path.name, path.read_bytes())]
        if suffix != ".zip":
            raise FootballDataFormatError("Only local .csv and .zip files are accepted")
        if path.stat().st_size > MAX_ZIP_BYTES:
            raise FootballDataFormatError("ZIP exceeds the local size limit")
        try:
            with ZipFile(path) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_ZIP_MEMBERS:
                    raise FootballDataFormatError("ZIP contains too many members")
                candidates = [info for info in infos if _safe_zip_member(info)]
                if not candidates:
                    raise FootballDataFormatError("ZIP contains no safe CSV file")
                if sum(info.file_size for info in candidates) > MAX_ZIP_UNCOMPRESSED_BYTES:
                    raise FootballDataFormatError("ZIP expands beyond the aggregate size limit")
                payloads: list[tuple[str, bytes]] = []
                for info in candidates:
                    if info.file_size > MAX_CSV_BYTES:
                        raise FootballDataFormatError(
                            f"ZIP member {info.filename!r} exceeds the size limit"
                        )
                    compressed = max(info.compress_size, 1)
                    if info.file_size / compressed > MAX_COMPRESSION_RATIO:
                        raise FootballDataFormatError(
                            f"ZIP member {info.filename!r} has an unsafe compression ratio"
                        )
                    payload = archive.read(info)
                    if len(payload) != info.file_size:
                        raise FootballDataFormatError(
                            f"ZIP member {info.filename!r} is truncated"
                        )
                    payloads.append((info.filename, payload))
                return payloads
        except BadZipFile as exc:
            raise FootballDataFormatError("Invalid ZIP file") from exc

    def import_file(
        self,
        file_path: str | Path,
        *,
        competition: str,
        season: str,
        strict: bool = False,
    ) -> FootballDataDataset:
        """Preserve and normalize a user-supplied local CSV/ZIP.

        This is the provider operation used by the CLI command
        ``import-football-data --file ... --competition ... --season ...``.
        """

        path = Path(file_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise FootballDataFormatError(f"Not a file: {path}")
        payloads = self._csv_payloads(path)
        competition = competition.strip()
        season = season.strip()
        if not competition or not season:
            raise ValueError("competition and season are required")
        competition_key = _safe_token(competition, "competition")
        season_key = _safe_token(season, "season")
        original = path.read_bytes()
        source_hash = hashlib.sha256(original).hexdigest()
        self.cache.put_bytes(
            f"imports/{source_hash}", original, extension=path.suffix.lstrip(".")
        )

        modified_at = datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat()
        all_records: list[dict[str, Any]] = []
        rejected = 0
        warnings: list[str] = []
        reports: list[ColumnReport] = []
        source_files: list[str] = []
        for source_name, payload in payloads:
            payload_hash = hashlib.sha256(payload).hexdigest()
            self.cache.put_bytes(
                f"datasets/{competition_key}/{season_key}/{payload_hash}",
                payload,
                extension="csv",
            )
            rows, report = _parse_csv(payload)
            reports.append(report)
            source_files.append(source_name)
            if strict and not report.usable:
                missing = ", ".join(report.missing_required_columns)
                raise FootballDataFormatError(
                    f"{source_name} is missing required columns: {missing}"
                )
            if not report.usable:
                warnings.append(
                    f"{source_name}: missing columns "
                    + ", ".join(report.missing_required_columns)
                )
            normalized, rejected_here = normalize_records(
                rows,
                competition=competition,
                season=season,
                source_updated_at=modified_at,
                source_file=source_name,
            )
            all_records.extend(normalized)
            rejected += rejected_here

        combined_report = detect_columns(
            column for report in reports for column in report.raw_columns
        )
        result = FootballDataDataset(
            records=tuple(all_records),
            column_report=combined_report,
            competition=competition,
            season=season,
            source_hash=source_hash,
            source_files=tuple(source_files),
            rejected_rows=rejected,
            warnings=tuple(warnings),
        )
        self.cache.put_json(
            f"manifests/{competition_key}/{season_key}/{source_hash}",
            {
                key: value
                for key, value in result.to_dict().items()
                if key != "records"
            },
        )
        return result

    # Explicit local-only aliases for adapters that prefer ingestion terminology.
    load_file = import_file
    import_csv = import_file


FootballDataProvider = FootballDataLocalProvider
FootballDataCoUkProvider = FootballDataLocalProvider


__all__ = [
    "COLUMN_MAP",
    "ColumnReport",
    "FootballDataCoUkProvider",
    "FootballDataDataset",
    "FootballDataError",
    "FootballDataFormatError",
    "FootballDataLocalProvider",
    "FootballDataProvider",
    "detect_columns",
    "normalize_records",
    "normalize_row",
]
