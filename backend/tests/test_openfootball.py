from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models
from app.data_sources.openfootball.entity_resolver import resolve_openfootball_name
from app.data_sources.openfootball.importer import (
    OpenFootballImportError,
    detect_openfootball_dataset,
    import_openfootball_repository,
)
from app.data_sources.openfootball.json_parser import parse_openfootball_json_data
from app.data_sources.openfootball.football_txt_parser import parse_football_txt_data
from app.data_sources.openfootball.validators import validate_openfootball_match
from app.data_sources.openfootball.validators import normalize_openfootball_season
from app.db import SessionLocal
from app.services.openfootball_imports import (
    confirm_openfootball_import,
    preview_openfootball_path,
    reprocess_openfootball_import,
)


def test_json_keeps_90_extra_time_penalties_and_future_fixture_separate() -> None:
    dataset = parse_openfootball_json_data(
        {
            "name": "Copa Offline",
            "matches": [
                {
                    "date": "2026-06-01",
                    "team1": "Norte FC",
                    "team2": "Sur FC",
                    "score": {"ft": [1, 1], "ht": [0, 1], "et": [2, 1], "p": [4, 3]},
                },
                {"date": "2026-06-02", "team1": "Este", "team2": "Oeste"},
            ],
        },
        source_file="cup/2026.json",
        season="2026",
    )
    result, fixture = dataset.matches
    assert (result.fulltime_home_goals, result.extra_time_home_goals) == (1, 2)
    assert (result.penalty_home_goals, result.penalty_away_goals) == (4, 3)
    assert result.status.value == "finished"
    assert fixture.status.value == "scheduled"
    assert fixture.fulltime_home_goals is None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "Bayern v Chelsea 3-4 pen (1-1,1-1,0-0)",
            {"ft": (1, 1), "et": (1, 1), "ht": (0, 0), "p": (3, 4)},
        ),
        (
            "Bayern v Chelsea 3-4 pen 1-1 aet",
            {"ft": (None, None), "et": (1, 1), "ht": (None, None), "p": (3, 4)},
        ),
        (
            "France 2-1 aet/gg Germany",
            {"ft": (None, None), "et": (2, 1), "ht": (None, None), "p": (None, None)},
        ),
    ],
)
def test_football_txt_elimination_variants_keep_teams_clean(
    line: str, expected: dict[str, tuple[int | None, int | None]]
) -> None:
    match = parse_football_txt_data(
        f"= Cup 2026\n2026-01-01\n{line}", competition="Cup", season="2026"
    ).matches[0]
    assert match.away_team in {"Chelsea", "Germany"}
    assert (match.fulltime_home_goals, match.fulltime_away_goals) == expected["ft"]
    assert (match.extra_time_home_goals, match.extra_time_away_goals) == expected["et"]
    assert (match.halftime_home_goals, match.halftime_away_goals) == expected["ht"]
    assert (match.penalty_home_goals, match.penalty_away_goals) == expected["p"]


def test_football_txt_inherits_partial_dates_and_rolls_season_year() -> None:
    dataset = parse_football_txt_data(
        """
= Liga MX 2025/26
Dec 20
20:00 Club América v León 2-1 (1-0)
Jan 10
Pumas UNAM v Tigres UANL
""",
        source_file="world/north-america/mexico/2025-26/liga-mx.txt",
    )
    first, second = dataset.matches
    assert first.date == "2025-12-20"
    assert second.date == "2026-01-10"
    assert first.home_team == "Club América"
    assert (first.halftime_home_goals, first.halftime_away_goals) == (1, 0)
    assert second.status.value == "scheduled"
    assert second.fulltime_home_goals is None


def test_validation_rejects_partial_score_and_missing_date() -> None:
    parsed = parse_openfootball_json_data(
        [{"team1": "A", "team2": "B", "score": {"ft": [2, None]}}],
        source_file="broken.json",
        competition="Broken",
        season="2026",
    ).matches[0]
    validation = validate_openfootball_match(parsed)
    assert not validation.valid
    assert any("provided together" in error for error in validation.errors)
    assert "date is required" in validation.errors


def test_importer_filters_invalid_rows_from_preview(tmp_path: Path) -> None:
    source = tmp_path / "league.json"
    source.write_text(
        json.dumps(
            {
                "name": "Validation League",
                "matches": [
                    {"date": "2026-01-01", "team1": "A", "team2": "B", "score": {"ft": [1, 0]}},
                    {"date": "2026-01-02", "team1": "C", "team2": "D", "score": {"ft": [2, None]}},
                ],
            }
        ),
        encoding="utf-8",
    )
    result = import_openfootball_repository(source, season="2026")
    assert len(result.matches) == 1
    assert result.metrics["errors"] == 1
    assert result.errors[0].code == "validation_error"


def test_directory_and_zip_security(tmp_path: Path) -> None:
    dataset = tmp_path / "repo"
    dataset.mkdir()
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    symlink = dataset / "escape.json"
    try:
        symlink.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    with pytest.raises(OpenFootballImportError):
        detect_openfootball_dataset(dataset)

    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../escape.json", "{}")
    with pytest.raises(OpenFootballImportError):
        detect_openfootball_dataset(archive)


def test_repository_metadata_and_zip_fixtures_are_ignored(tmp_path: Path) -> None:
    repo = tmp_path / "football.json"
    (repo / "tests").mkdir(parents=True)
    (repo / "package.json").write_text('{"name":"football.json"}', encoding="utf-8")
    (repo / "tests" / "fixture.json").write_text("{}", encoding="utf-8")
    (repo / "2024-25.json").write_text(
        json.dumps(
            {
                "name": "Metadata Safe League 2024/25",
                "matches": [
                    {"date": "2024-08-01", "team1": "Meta A", "team2": "Meta B"}
                ],
            }
        ),
        encoding="utf-8",
    )
    folder_result = import_openfootball_repository(repo)
    assert folder_result.detection.files_scanned == 1
    assert not folder_result.errors
    assert len(folder_result.matches) == 1

    archive = tmp_path / "football.json.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("football.json-master/package.json", '{"name":"football.json"}')
        output.writestr("football.json-master/tests/broken.json", "{}")
        output.writestr(
            "football.json-master/2024-25.json",
            (repo / "2024-25.json").read_text(encoding="utf-8"),
        )
    zip_result = import_openfootball_repository(archive)
    assert zip_result.detection.files_scanned == 1
    assert not zip_result.errors
    assert len(zip_result.matches) == 1


def test_equivalent_season_labels_share_one_canonical_value() -> None:
    assert {
        normalize_openfootball_season(value)
        for value in ("2024/25", "2024-25", "2024-2025")
    } == {"2024-25"}


@pytest.mark.parametrize(
    ("parts", "competition", "repository", "country"),
    [
        (("world", "north-america", "mexico"), "Liga MX", "world", "Mexico"),
        (("england",), "Premier League", "england", "England"),
        (("espana",), "LaLiga", "espana", "Spain"),
    ],
)
def test_priority_competitions_are_detected_offline(
    tmp_path: Path,
    parts: tuple[str, ...],
    competition: str,
    repository: str,
    country: str,
) -> None:
    root = tmp_path / "openfootball"
    for part in parts:
        root /= part
    season = root / "2025-26"
    season.mkdir(parents=True)
    (season / "league.txt").write_text(
        f"= {competition} 2025/26\n2025-08-01\nHome Ñ v Away Ü\n",
        encoding="utf-8",
    )
    result = import_openfootball_repository(root)
    assert result.detection.source_repository == repository
    assert result.detection.country == country
    assert not result.errors
    assert result.matches[0].competition == competition
    assert result.matches[0].season == "2025-26"
    assert result.matches[0].status.value == "scheduled"


def test_exact_homonyms_are_ambiguous() -> None:
    resolution = resolve_openfootball_name("United", [(10, "United"), (20, "United")])
    assert resolution.status == "ambiguous"
    assert resolution.internal_entity_id is None
    assert set(resolution.candidate_ids) == {10, 20}


def test_persistence_is_idempotent_and_keeps_90_minute_score(tmp_path: Path) -> None:
    source = tmp_path / "timezone-cup.json"
    source.write_text(
        json.dumps(
            {
                "name": "Timezone Cup Isolated",
                "matches": [
                    {
                        "date": "2026-07-10",
                        "time": "13:00 UTC-6",
                        "team1": "Man United",
                        "team2": "Liverpool FC",
                        "score": {"ft": [1, 1], "et": [2, 1]},
                    },
                    {
                        "date": "2026-07-11",
                        "team1": "Liverpool",
                        "team2": "Manchester United FC",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    with SessionLocal() as db:
        run = preview_openfootball_path(db, source, season="2026")
        run = confirm_openfootball_import(db, run)
        assert run.status == "completed"
        match = db.scalar(
            select(models.Match)
            .join(models.Competition, models.Competition.id == models.Match.competition_id)
            .where(models.Competition.name == "Timezone Cup Isolated")
            .order_by(models.Match.match_date)
        )
        assert match is not None
        assert match.match_date.hour == 19
        assert (match.home_score, match.away_score) == (1, 1)
        assert match.result_details["extra_time_home_goals"] == 2
        assert match.result_details["timezone_known"] is True
        mapping_ids = set(
            db.scalars(
                select(models.OpenFootballEntityMapping.internal_entity_id).where(
                    models.OpenFootballEntityMapping.entity_type == "team",
                    models.OpenFootballEntityMapping.original_name.in_(
                        ["Man United", "Manchester United FC"]
                    ),
                )
            ).all()
        )
        assert len(mapping_ids) == 1
        source_records_before = len(
            db.scalars(
                select(models.MatchSourceRecord).where(
                    models.MatchSourceRecord.ingestion_run_id == run.id
                )
            ).all()
        )
        run = confirm_openfootball_import(db, run)
        source_records_after = len(
            db.scalars(
                select(models.MatchSourceRecord).where(
                    models.MatchSourceRecord.ingestion_run_id == run.id
                )
            ).all()
        )
        assert source_records_after == source_records_before == 2


def test_second_run_deduplicates_and_reprocess_preserves_score_conflict(tmp_path: Path) -> None:
    source = tmp_path / "reprocess.json"

    def write_score(home: int) -> None:
        source.write_text(
            json.dumps(
                {
                    "name": "Reprocess Conflict League Isolated",
                    "matches": [
                        {
                            "date": "2026-09-01",
                            "team1": "Conflict North",
                            "team2": "Conflict South",
                            "score": {"ft": [home, 0]},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    write_score(1)
    with SessionLocal() as db:
        first = confirm_openfootball_import(
            db, preview_openfootball_path(db, source, season="2026")
        )
        second = confirm_openfootball_import(
            db, preview_openfootball_path(db, source, season="2026")
        )
        assert first.status == second.status == "completed"
        assert second.import_metrics["duplicates"] == 1
        count_before = len(
            db.scalars(
                select(models.MatchSourceRecord)
                .join(models.Match, models.Match.id == models.MatchSourceRecord.match_id)
                .join(models.Competition, models.Competition.id == models.Match.competition_id)
                .where(models.Competition.name == "Reprocess Conflict League Isolated")
            ).all()
        )
        old_hash = first.file_hash
        write_score(2)
        first = reprocess_openfootball_import(db, first)
        match = db.scalar(
            select(models.Match)
            .join(models.Competition, models.Competition.id == models.Match.competition_id)
            .where(models.Competition.name == "Reprocess Conflict League Isolated")
        )
        assert match is not None and match.home_score == 1
        assert first.status == "completed_with_errors"
        assert first.import_metrics["conflicts"] == 1
        assert first.file_hash != old_hash
        assert first.preview_payload["preview_matches"][0]["fulltime_home_goals"] == 2
        count_after = len(
            db.scalars(
                select(models.MatchSourceRecord)
                .join(models.Match, models.Match.id == models.MatchSourceRecord.match_id)
                .join(models.Competition, models.Competition.id == models.Match.competition_id)
                .where(models.Competition.name == "Reprocess Conflict League Isolated")
            ).all()
        )
        assert count_after == count_before + 1
        first = reprocess_openfootball_import(db, first)
        assert first.import_metrics["conflicts"] == 1


def test_openfootball_web_flow_has_no_server_path_input(client: TestClient) -> None:
    payload = json.dumps(
        {
            "name": "Web Upload League Isolated",
            "matches": [
                {"date": "2026-08-01", "team1": "Web Alpha", "team2": "Web Beta", "score": {"ft": [2, 0]}}
            ],
        }
    ).encode()
    preview = client.post(
        "/api/v1/openfootball/preview",
        files=[("files", ("league.json", payload, "application/json"))],
        data={"season": "2026", "preview_limit": "10"},
    )
    assert preview.status_code == 201, preview.text
    body = preview.json()
    assert body["status"] == "previewed"
    assert body["metrics"]["matches_found"] == 1
    assert "raw_payload" not in body["preview_matches"][0]
    run_id = body["import_id"]
    confirmed = client.post(f"/api/v1/openfootball/imports/{run_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "completed"
    repeated = client.post(f"/api/v1/openfootball/imports/{run_id}/confirm")
    assert repeated.status_code == 200
    assert repeated.json()["import_id"] == run_id
