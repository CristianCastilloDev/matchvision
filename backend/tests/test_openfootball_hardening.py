from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import models
from app.db import Base
from app.services.openfootball_conflicts import (
    OpenFootballConflictError,
    resolve_openfootball_match_conflict,
)
from app.services.openfootball_imports import (
    OpenFootballPersistenceError,
    confirm_openfootball_import,
    preview_openfootball_path,
)


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.mark.parametrize(
    ("contents", "season"),
    [
        ("= League Without Year\n2026-01-01\nNorth v South\n", None),
        ("2026-01-01\nNorth v South\n", "2026"),
    ],
)
def test_preview_rejects_missing_competition_or_year_bearing_season(
    db: Session, tmp_path: Path, contents: str, season: str | None
) -> None:
    source = tmp_path / "league.txt"
    source.write_text(contents, encoding="utf-8")

    with pytest.raises(OpenFootballPersistenceError, match="identidad incompleta"):
        preview_openfootball_path(db, source, season=season)


def test_preview_and_confirm_apply_the_same_identity_gate(
    db: Session, tmp_path: Path
) -> None:
    source = tmp_path / "league.txt"
    source.write_text(
        "= League Without Year\n2026-01-01\nNorth v South\n",
        encoding="utf-8",
    )
    with pytest.raises(OpenFootballPersistenceError) as preview_error:
        preview_openfootball_path(db, source)

    run = models.DataIngestionRun(
        source="openfootball",
        original_filename=source.name,
        stored_path=str(source),
        import_options={"offline": True},
        import_metrics={},
        status="previewed",
        pipeline_version="test",
        is_mock_data=False,
    )
    db.add(run)
    db.commit()
    with pytest.raises(OpenFootballPersistenceError) as confirm_error:
        confirm_openfootball_import(db, run)

    assert "falta season con año válido" in str(preview_error.value)
    assert "falta season con año válido" in str(confirm_error.value)
    assert run.status == "failed"


def test_preview_canonicalizes_season_and_confirm_reuses_legacy_equivalent(
    db: Session, tmp_path: Path
) -> None:
    source = tmp_path / "canonical.json"
    source.write_text(
        json.dumps(
            {
                "name": "Canonical League 2024/2025",
                "matches": [
                    {
                        "date": "2024-08-01",
                        "team1": "Canonical North",
                        "team2": "Canonical South",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    competition = models.Competition(
        external_id="canonical-league",
        name="Canonical League",
        data_source="manual",
        is_mock_data=False,
    )
    db.add(competition)
    db.flush()
    legacy = models.Season(
        external_id="canonical-legacy-season",
        competition_id=competition.id,
        name="2024/2025",
        data_source="manual",
        is_mock_data=False,
    )
    db.add(legacy)
    db.commit()

    run = preview_openfootball_path(db, source)
    assert run.preview_payload["preview_matches"][0]["season"] == "2024-25"
    assert run.preview_payload["detection"]["seasons"] == ["2024-25"]
    confirm_openfootball_import(db, run)

    seasons = list(
        db.scalars(
            select(models.Season).where(models.Season.competition_id == competition.id)
        ).all()
    )
    assert len(seasons) == 1
    assert seasons[0].id == legacy.id
    assert seasons[0].name == "2024-25"


def test_score_conflict_is_persisted_and_resolved_as_one_atomic_group(
    db: Session, tmp_path: Path
) -> None:
    source = tmp_path / "atomic.json"

    def write_score(home: int, away: int) -> None:
        source.write_text(
            json.dumps(
                {
                    "name": "Atomic League 2026",
                    "matches": [
                        {
                            "date": "2026-09-01",
                            "team1": "Atomic North",
                            "team2": "Atomic South",
                            "score": {"ft": [home, away]},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    write_score(1, 0)
    first = confirm_openfootball_import(db, preview_openfootball_path(db, source))
    write_score(2, 0)
    second = confirm_openfootball_import(db, preview_openfootball_path(db, source))
    assert first.status == "completed"
    assert second.status == "completed_with_errors"
    record = db.scalar(
        select(models.MatchSourceRecord).where(
            models.MatchSourceRecord.ingestion_run_id == second.id,
            models.MatchSourceRecord.conflict_status == "conflict",
        )
    )
    assert record is not None
    fields = {item["field"] for item in record.conflict_details["fields"]}
    assert {
        "home_score",
        "away_score",
        "result_details.fulltime_home_goals",
        "result_details.fulltime_away_goals",
    } <= fields
    match = db.get(models.Match, record.match_id)
    assert match is not None and (match.home_score, match.away_score) == (1, 0)

    hybrid = {field: "incoming" for field in fields}
    hybrid["away_score"] = "existing"
    with pytest.raises(OpenFootballConflictError, match="mismo origen"):
        resolve_openfootball_match_conflict(db, record.id, decisions=hybrid)
    assert (match.home_score, match.away_score) == (1, 0)

    resolve_openfootball_match_conflict(
        db, record.id, decisions={field: "existing" for field in fields}
    )
    assert (match.home_score, match.away_score) == (1, 0)
    assert match.result_details["fulltime_home_goals"] == 1
