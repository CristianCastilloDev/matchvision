from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app.providers.football_data import FootballDataFormatError, FootballDataLocalProvider
from app.providers.statsbomb import StatsBombLocalProvider


def test_football_data_normalizes_local_csv_and_preserves_missing(tmp_path: Path) -> None:
    source = tmp_path / "league.csv"
    source.write_text(
        "Date,HomeTeam,AwayTeam,FTHG,FTAG,HS,AS\n"
        "01/01/2025,Águilas,Leones,2,1,12,8\n",
        encoding="utf-8",
    )
    dataset = FootballDataLocalProvider(cache_dir=tmp_path / "cache").import_file(
        source, competition="Liga", season="2025"
    )
    assert dataset.imported_rows == 1
    row = dataset.records[0]
    assert row["home_team_name"] == "Águilas"
    assert row["home_goals"] == 2
    assert row["home_yellow_cards"] is None
    assert row["data_source"] == "football_data_local_file"
    assert dataset.column_report.usable


def test_football_data_rejects_zip_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../escape.csv", "Date,HomeTeam,AwayTeam,FTHG,FTAG\n")
    with pytest.raises(FootballDataFormatError):
        FootballDataLocalProvider(cache_dir=tmp_path / "cache").import_file(
            archive, competition="Liga", season="2025"
        )


def test_statsbomb_reads_only_local_tree(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "matches" / "11").mkdir(parents=True)
    (data / "events").mkdir()
    (data / "lineups").mkdir()
    (data / "competitions.json").write_text(
        json.dumps(
            [
                {
                    "competition_id": 11,
                    "competition_name": "Liga local",
                    "season_id": 90,
                    "season_name": "2025",
                }
            ]
        ),
        encoding="utf-8",
    )
    (data / "matches" / "11" / "90.json").write_text(
        json.dumps(
            [
                {
                    "match_id": 1,
                    "match_date": "2025-01-01",
                    "kick_off": "12:00:00",
                    "competition": {"competition_id": 11, "name": "Liga local"},
                    "season": {"season_id": 90, "name": "2025"},
                    "home_team": {"home_team_id": 1, "name": "A"},
                    "away_team": {"away_team_id": 2, "name": "B"},
                    "home_score": 1,
                    "away_score": 0,
                }
            ]
        ),
        encoding="utf-8",
    )
    (data / "events" / "1.json").write_text("[]", encoding="utf-8")
    (data / "lineups" / "1.json").write_text("[]", encoding="utf-8")
    provider = StatsBombLocalProvider(tmp_path, cache_dir=tmp_path / "cache")
    assert provider.get_matches(11, 90, normalize=True)[0]["home_team_name"] == "A"
    assert provider.import_competition(11, 90).complete
