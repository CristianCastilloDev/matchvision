from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app import models
from app.data_sources.openfootball.catalog_parser import (
    parse_openfootball_clubs_data,
    parse_openfootball_leagues_data,
    parse_openfootball_players_data,
    sniff_openfootball_catalog_kind,
)
from app.db import Base
from app.services.openfootball_imports import (
    confirm_openfootball_import,
    preview_openfootball_path,
    reprocess_openfootball_import,
)


def test_league_catalog_preserves_code_country_division_type_and_aliases() -> None:
    catalog = parse_openfootball_leagues_data(
        """
= International =
cl UEFA Champions League
World Cup | FIFA World Cup
= England =
1 English Premier League
 | ENG PL | England Premier League | Premier League
""",
        source_file="leagues.txt",
    )
    champions, world_cup, premier = catalog.records
    assert champions.code == "cl"
    assert champions.competition_type == "cup"
    assert world_cup.code == ""
    assert world_cup.name == "World Cup"
    assert world_cup.aliases == ["FIFA World Cup"]
    assert world_cup.competition_type == "cup"
    assert premier.country == "England"
    assert premier.division == 1
    assert premier.competition_type == "league"
    assert premier.aliases == ["ENG PL", "England Premier League", "Premier League"]


def test_club_catalog_preserves_identity_stadium_city_and_aliases() -> None:
    catalog = parse_openfootball_clubs_data(
        """
= England
Arsenal FC, 1886, @ Emirates Stadium, London
 | Arsenal | FC Arsenal
""",
        source_file="clubs.txt",
    )
    club = catalog.records[0]
    assert club.name == "Arsenal FC"
    assert club.country == "England"
    assert club.founded_year == 1886
    assert club.stadium == "Emirates Stadium"
    assert club.city == "London"
    assert club.aliases == ["Arsenal", "FC Arsenal"]


def test_player_catalog_contains_identity_only() -> None:
    catalog = parse_openfootball_players_data(
        """
= France
Kylian Mbappé, F, 1.78 m, b. 20 Dec 1998 @ Paris
 | Kylian Mbappe
""",
        source_file="players.txt",
    )
    player = catalog.records[0]
    assert player.name == "Kylian Mbappé"
    assert player.nationality == "France"
    assert player.position == "forward"
    assert player.height_m == 1.78
    assert player.birth_date is not None and player.birth_date.isoformat() == "1998-12-20"
    assert player.birthplace == "Paris"
    assert player.aliases == ["Kylian Mbappe"]
    assert not hasattr(player, "goals")
    assert not hasattr(player, "xg")
    assert not hasattr(player, "minutes")


def test_historical_player_catalog_supports_inline_alias_bare_birth_and_cm_height() -> None:
    catalog = parse_openfootball_players_data(
        """
= Brazil
José Silva|Zé Silva, 3 Sep 1979, 186
""",
        source_file="players.txt",
    )
    player = catalog.records[0]
    assert player.name == "José Silva"
    assert player.aliases == ["Zé Silva"]
    assert player.birth_date is not None and player.birth_date.isoformat() == "1979-09-03"
    assert player.height_m == 1.86
    assert player.position is None


def test_standalone_catalog_sniffing_does_not_claim_match_fixtures() -> None:
    assert (
        sniff_openfootball_catalog_kind(
            "= England =\n1 English Premier League\n | ENG PL | Premier League\n"
        )
        == "leagues"
    )
    assert (
        sniff_openfootball_catalog_kind(
            "= International =\nWorld Cup | FIFA World Cup\n"
        )
        == "leagues"
    )
    assert (
        sniff_openfootball_catalog_kind(
            "= Premier League 2025/26\nAug 15\n20:00 Liverpool v Bournemouth 4-2 (1-0)\n"
        )
        is None
    )


def test_catalog_folder_persists_identity_fields_and_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "openfootball"
    (root / "leagues").mkdir(parents=True)
    (root / "clubs").mkdir()
    (root / "players").mkdir()
    (root / "leagues" / "leagues.txt").write_text(
        "= England =\n1 English Premier League\n | ENG PL | Premier League\n",
        encoding="utf-8",
    )
    (root / "clubs" / "clubs.txt").write_text(
        "= England\nArsenal FC, 1886, @ Emirates Stadium, London\n | Arsenal\n",
        encoding="utf-8",
    )
    (root / "players" / "players.txt").write_text(
        "= France\nKylian Mbappé, F, 1.78 m, b. 20 Dec 1998 @ Paris\n",
        encoding="utf-8",
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        preview = preview_openfootball_path(db, root)
        assert preview.import_metrics["catalog_records_found"] == 3
        assert preview.import_metrics["matches_found"] == 0
        first = confirm_openfootball_import(db, preview)
        assert first.status == "completed"
        assert first.valid_records == 3
        assert first.import_metrics["catalog_records_imported"] == 3

        league = db.scalar(
            select(models.Competition).where(
                models.Competition.name == "English Premier League"
            )
        )
        club = db.scalar(select(models.Team).where(models.Team.name == "Arsenal FC"))
        player = db.scalar(
            select(models.Player).where(models.Player.name == "Kylian Mbappé")
        )
        assert league is not None
        assert (league.catalog_code, league.competition_level, league.competition_type) == (
            "1",
            1,
            "league",
        )
        assert club is not None
        assert (club.city, club.stadium, club.founded_year) == (
            "London",
            "Emirates Stadium",
            1886,
        )
        assert player is not None
        assert (player.height_m, player.birthplace) == (1.78, "Paris")
        assert player.penalty_taker is None
        assert player.free_kick_taker is None
        assert player.expected_minutes is None

        second = confirm_openfootball_import(
            db, preview_openfootball_path(db, root)
        )
        assert second.import_metrics["duplicates"] == 3
        assert db.scalar(select(func.count()).select_from(models.Competition)) == 1
        assert db.scalar(select(func.count()).select_from(models.Team)) == 1
        assert db.scalar(select(func.count()).select_from(models.Player)) == 1


@pytest.mark.parametrize("as_zip", [False, True], ids=["local-clone", "zip"])
def test_catalog_auxiliary_files_never_create_canonical_entities(
    tmp_path: Path, as_zip: bool
) -> None:
    root = tmp_path / "openfootball"
    (root / "clubs").mkdir(parents=True)
    (root / "leagues").mkdir()
    files = {
        "clubs/clubs.txt": "= England\nArsenal FC, 1886, @ Emirates Stadium, London\n",
        "clubs/england.props.txt": "Fake Props FC, 1900, @ Props Ground, Nowhere\n",
        "clubs/england.history.txt": "Fake History FC, 1901, @ History Ground, Nowhere\n",
        "clubs/england.stadiums.txt": "Fake Stadium FC, 1902, @ Stadium Ground, Nowhere\n",
        "leagues/leagues.txt": "= England =\n1 English Premier League\n",
        "leagues/seasons.txt": "1 False Seasonal League\n",
    }
    for relative, content in files.items():
        target = root / relative
        target.write_text(content, encoding="utf-8")

    source: Path = root
    if as_zip:
        source = tmp_path / "openfootball-catalogs.zip"
        with zipfile.ZipFile(source, "w") as archive:
            for relative in files:
                archive.write(root / relative, relative)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        preview = preview_openfootball_path(db, source)
        assert preview.import_metrics["catalog_records_found"] == 2
        warnings = preview.preview_payload["warnings"]
        for auxiliary in (
            "england.props.txt",
            "england.history.txt",
            "england.stadiums.txt",
            "seasons.txt",
        ):
            assert any(auxiliary in warning and "ignored auxiliary" in warning for warning in warnings)

        confirmed = confirm_openfootball_import(db, preview)
        assert confirmed.import_metrics["catalog_records_imported"] == 2
        assert db.scalar(select(func.count()).select_from(models.Team)) == 1
        assert db.scalar(select(func.count()).select_from(models.Competition)) == 1
        assert db.scalar(select(func.count()).select_from(models.Player)) == 0
        assert db.scalar(select(models.Team.name)) == "Arsenal FC"
        assert db.scalar(select(models.Competition.name)) == "English Premier League"


def test_player_homonyms_use_birth_date_and_reimport_preserves_manual_resolution(
    tmp_path: Path,
) -> None:
    root = tmp_path / "players"
    root.mkdir()
    (root / "players.txt").write_text(
        """= Brazil
José Silva|Zé Silva, 3 Sep 1979, 186
José Silva|Zézinho, 4 Oct 1985, 180
""",
        encoding="utf-8",
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        run = confirm_openfootball_import(db, preview_openfootball_path(db, root))
        players = list(db.scalars(select(models.Player).order_by(models.Player.birth_date)))
        assert [(player.name, player.birth_date.isoformat()) for player in players] == [
            ("José Silva", "1979-09-03"),
            ("José Silva", "1985-10-04"),
        ]

        mapping = db.scalar(
            select(models.OpenFootballEntityMapping).where(
                models.OpenFootballEntityMapping.entity_type == "player",
                models.OpenFootballEntityMapping.source_repository == "players",
                models.OpenFootballEntityMapping.original_name == "José Silva",
            )
        )
        assert mapping is not None
        mapping.internal_entity_id = players[1].id
        mapping.confidence = 1.0
        mapping.manually_verified = True
        mapping.resolution_status = "manually_resolved"
        mapping.resolution_notes = "DOB verified by a local reviewer"
        for conflict in db.scalars(
            select(models.EntityResolutionConflict).where(
                models.EntityResolutionConflict.entity_type == "player",
                models.EntityResolutionConflict.normalized_name == mapping.normalized_name,
            )
        ):
            conflict.status = "resolved"
        db.commit()

        repeated = reprocess_openfootball_import(db, run)
        db.refresh(mapping)
        assert repeated.import_metrics["duplicates"] >= 2
        assert db.scalar(select(func.count()).select_from(models.Player)) == 2
        assert mapping.internal_entity_id == players[1].id
        assert mapping.manually_verified is True
        assert mapping.resolution_status == "manually_resolved"
        assert mapping.resolution_notes == "DOB verified by a local reviewer"
        assert (
            db.scalar(
                select(func.count())
                .select_from(models.EntityResolutionConflict)
                .where(models.EntityResolutionConflict.status == "pending")
            )
            == 0
        )
