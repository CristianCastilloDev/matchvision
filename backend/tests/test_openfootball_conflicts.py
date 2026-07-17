from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import models
from app.db import Base, SessionLocal
from app.services.openfootball_conflicts import (
    OpenFootballConflictError,
    OpenFootballConflictStateError,
    list_pending_openfootball_conflicts,
    resolve_openfootball_entity_conflict,
    resolve_openfootball_match_conflict,
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


def test_entity_conflict_requires_a_recorded_real_candidate_and_updates_mapping(
    db: Session,
) -> None:
    first = models.Team(
        external_id="conflict-team-1",
        name="United North",
        data_source="manual",
        is_mock_data=False,
    )
    second = models.Team(
        external_id="conflict-team-2",
        name="United South",
        data_source="manual",
        is_mock_data=False,
    )
    db.add_all([first, second])
    db.flush()
    mapping = models.OpenFootballEntityMapping(
        entity_type="team",
        original_name="United",
        normalized_name="united",
        internal_entity_id=None,
        source_repository="england",
        confidence=0.92,
        manually_verified=False,
        resolution_status="ambiguous",
    )
    conflict = models.EntityResolutionConflict(
        entity_type="team",
        provider="openfootball",
        source_repository="england",
        source_name="United",
        normalized_name="united",
        candidate_ids=[first.id, second.id],
        best_score=0.92,
        status="pending",
    )
    db.add_all([mapping, conflict])
    db.commit()

    pending = list_pending_openfootball_conflicts(db)
    assert pending["entity_conflicts"][0]["candidate_ids"] == [first.id, second.id]
    assert pending["entity_conflicts"][0]["source_repositories"] == ["england"]

    with pytest.raises(OpenFootballConflictError, match="candidate_ids"):
        resolve_openfootball_entity_conflict(db, conflict.id, candidate_id=999_999)
    assert mapping.internal_entity_id is None
    assert conflict.status == "pending"

    result = resolve_openfootball_entity_conflict(
        db, conflict.id, candidate_id=second.id, notes="Revisión local"
    )
    assert result["status"] == "resolved"
    assert result["selected_candidate_id"] == second.id
    assert mapping.internal_entity_id == second.id
    assert mapping.manually_verified is True
    assert mapping.resolution_status == "manually_resolved"
    assert mapping.confidence == 1.0
    assert conflict.status == "resolved"
    assert "candidate_id=" in (mapping.resolution_notes or "")
    assert list_pending_openfootball_conflicts(db)["entity_conflicts"] == []


def _match_with_conflict(db: Session) -> tuple[models.Match, models.MatchSourceRecord]:
    competition = models.Competition(
        external_id="conflict-cup",
        name="Conflict Cup",
        data_source="manual",
        is_mock_data=False,
    )
    home = models.Team(
        external_id="conflict-home",
        name="Conflict Home",
        data_source="manual",
        is_mock_data=False,
    )
    away = models.Team(
        external_id="conflict-away",
        name="Conflict Away",
        data_source="manual",
        is_mock_data=False,
    )
    db.add_all([competition, home, away])
    db.flush()
    season = models.Season(
        external_id="conflict-season",
        competition_id=competition.id,
        name="2026",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        data_source="manual",
        is_mock_data=False,
    )
    db.add(season)
    db.flush()
    match = models.Match(
        external_id="conflict-match",
        competition_id=competition.id,
        season_id=season.id,
        home_team_id=home.id,
        away_team_id=away.id,
        match_date=datetime(2026, 7, 1, tzinfo=UTC),
        status="finished",
        home_score=1,
        away_score=0,
        result_details={"penalty_home_goals": 3, "penalty_away_goals": 2},
        data_source="manual",
        is_mock_data=False,
    )
    db.add(match)
    db.flush()
    record = models.MatchSourceRecord(
        match_id=match.id,
        source_name="openfootball",
        source_repository="world",
        source_record_id="conflicting-record",
        source_file="2026/cup.txt",
        raw_payload={},
        normalized_payload={},
        field_provenance={},
        content_hash="a" * 64,
        conflict_status="conflict",
        conflict_details={
            "fields": [
                {"field": "home_score", "existing": 1, "incoming": 4},
                {"field": "away_score", "existing": 0, "incoming": 2},
                {
                    "field": "result_details.penalty_home_goals",
                    "existing": 3,
                    "incoming": 5,
                },
            ]
        },
    )
    db.add(record)
    db.commit()
    return match, record


def test_match_conflict_requires_every_choice_and_keeps_an_audit_trail(db: Session) -> None:
    match, record = _match_with_conflict(db)
    pending = list_pending_openfootball_conflicts(db)
    assert pending["match_conflicts"][0]["id"] == record.id
    assert {item["field"] for item in pending["match_conflicts"][0]["fields"]} == {
        "home_score",
        "away_score",
        "result_details.penalty_home_goals",
    }

    with pytest.raises(OpenFootballConflictError, match="faltan decisiones"):
        resolve_openfootball_match_conflict(
            db,
            record.id,
            decisions={"home_score": "existing", "away_score": "incoming"},
        )
    assert record.conflict_status == "conflict"
    assert (match.home_score, match.away_score) == (1, 0)

    with pytest.raises(OpenFootballConflictError, match="mismo origen"):
        resolve_openfootball_match_conflict(
            db,
            record.id,
            decisions={
                "home_score": "existing",
                "away_score": "incoming",
                "result_details.penalty_home_goals": "incoming",
            },
        )
    assert (match.home_score, match.away_score) == (1, 0)

    result = resolve_openfootball_match_conflict(
        db,
        record.id,
        decisions={
            "home_score": "incoming",
            "away_score": "incoming",
            "result_details.penalty_home_goals": "incoming",
        },
        notes="Acta revisada en local",
    )
    assert result["conflict_status"] == "resolved"
    assert (match.home_score, match.away_score) == (4, 2)
    assert match.result_details == {"penalty_home_goals": 5, "penalty_away_goals": 2}
    assert record.conflict_status == "resolved"
    assert record.conflict_details["fields"][0]["incoming"] == 4
    audit = record.conflict_details["resolution"]
    assert audit["decisions"]["home_score"] == "incoming"
    assert audit["values_before_resolution"]["away_score"] == 0
    assert audit["notes"] == "Acta revisada en local"
    assert list_pending_openfootball_conflicts(db)["match_conflicts"] == []

    with pytest.raises(OpenFootballConflictStateError, match="ya no está pendiente"):
        resolve_openfootball_match_conflict(
            db,
            record.id,
            decisions={
                "home_score": "incoming",
                "away_score": "incoming",
                "result_details.penalty_home_goals": "incoming",
            },
        )


def test_match_conflict_rejects_structural_or_arbitrary_fields(db: Session) -> None:
    _match, record = _match_with_conflict(db)
    record.conflict_details = {
        "fields": [{"field": "home_team_id", "existing": 1, "incoming": 2}]
    }
    db.commit()

    with pytest.raises(OpenFootballConflictStateError, match="no permitido"):
        resolve_openfootball_match_conflict(
            db, record.id, decisions={"home_team_id": "incoming"}
        )
    assert record.conflict_status == "conflict"


def test_openfootball_conflict_api_lists_and_resolves_both_kinds(
    client: TestClient,
) -> None:
    with SessionLocal() as session:
        first = models.Team(
            external_id="api-conflict-team-1",
            name="API United North",
            data_source="manual",
            is_mock_data=False,
        )
        second = models.Team(
            external_id="api-conflict-team-2",
            name="API United South",
            data_source="manual",
            is_mock_data=False,
        )
        session.add_all([first, second])
        session.flush()
        mapping = models.OpenFootballEntityMapping(
            entity_type="team",
            original_name="API United",
            normalized_name="api united",
            internal_entity_id=None,
            source_repository="england",
            confidence=0.9,
            manually_verified=False,
            resolution_status="ambiguous",
        )
        entity_conflict = models.EntityResolutionConflict(
            entity_type="team",
            provider="openfootball",
            source_repository="england",
            source_name="API United",
            normalized_name="api united",
            candidate_ids=[first.id, second.id],
            best_score=0.9,
            status="pending",
        )
        session.add_all([mapping, entity_conflict])
        session.flush()
        _match, match_conflict = _match_with_conflict(session)
        entity_conflict_id = entity_conflict.id
        selected_id = second.id
        match_conflict_id = match_conflict.id

    listed = client.get("/api/v1/openfootball/conflicts")
    assert listed.status_code == 200, listed.text
    assert any(item["id"] == entity_conflict_id for item in listed.json()["entity_conflicts"])
    assert any(item["id"] == match_conflict_id for item in listed.json()["match_conflicts"])

    invalid_entity = client.post(
        f"/api/v1/openfootball/conflicts/entities/{entity_conflict_id}/resolve",
        json={"candidate_id": 999_999},
    )
    assert invalid_entity.status_code == 422
    resolved_entity = client.post(
        f"/api/v1/openfootball/conflicts/entities/{entity_conflict_id}/resolve",
        json={"candidate_id": selected_id, "notes": "Revisión API offline"},
    )
    assert resolved_entity.status_code == 200, resolved_entity.text
    assert resolved_entity.json()["manually_verified"] is True

    incomplete_match = client.post(
        f"/api/v1/openfootball/conflicts/matches/{match_conflict_id}/resolve",
        json={"decisions": {"home_score": "existing"}},
    )
    assert incomplete_match.status_code == 422
    resolved_match = client.post(
        f"/api/v1/openfootball/conflicts/matches/{match_conflict_id}/resolve",
        json={
            "decisions": {
                "home_score": "incoming",
                "away_score": "incoming",
                "result_details.penalty_home_goals": "incoming",
            }
        },
    )
    assert resolved_match.status_code == 200, resolved_match.text
    assert resolved_match.json()["conflict_status"] == "resolved"

    repeated = client.post(
        f"/api/v1/openfootball/conflicts/matches/{match_conflict_id}/resolve",
        json={
            "decisions": {
                "home_score": "incoming",
                "away_score": "incoming",
                "result_details.penalty_home_goals": "incoming",
            }
        },
    )
    assert repeated.status_code == 409


def test_entity_resolution_is_scoped_to_the_conflict_repository(db: Session) -> None:
    first = models.Team(
        external_id="scoped-team-1",
        name="Scoped United North",
        data_source="manual",
        is_mock_data=False,
    )
    second = models.Team(
        external_id="scoped-team-2",
        name="Scoped United South",
        data_source="manual",
        is_mock_data=False,
    )
    db.add_all([first, second])
    db.flush()
    england_mapping = models.OpenFootballEntityMapping(
        entity_type="team",
        original_name="Scoped United",
        normalized_name="scoped united",
        source_repository="england",
        resolution_status="ambiguous",
    )
    world_mapping = models.OpenFootballEntityMapping(
        entity_type="team",
        original_name="Scoped United",
        normalized_name="scoped united",
        source_repository="world",
        resolution_status="ambiguous",
    )
    conflict = models.EntityResolutionConflict(
        entity_type="team",
        provider="openfootball",
        source_repository="england",
        source_name="Scoped United",
        normalized_name="scoped united",
        candidate_ids=[first.id, second.id],
        best_score=0.9,
        status="pending",
    )
    db.add_all([england_mapping, world_mapping, conflict])
    db.commit()

    listed = list_pending_openfootball_conflicts(db)["entity_conflicts"][0]
    assert listed["scope_status"] == "exact"
    assert listed["source_repositories"] == ["england"]
    resolve_openfootball_entity_conflict(db, conflict.id, candidate_id=second.id)
    assert england_mapping.internal_entity_id == second.id
    assert england_mapping.manually_verified is True
    assert world_mapping.internal_entity_id is None
    assert world_mapping.resolution_status == "ambiguous"


def test_legacy_entity_conflict_refuses_multiple_repository_mappings(db: Session) -> None:
    first = models.Team(
        external_id="legacy-scope-1",
        name="Legacy North",
        data_source="manual",
        is_mock_data=False,
    )
    second = models.Team(
        external_id="legacy-scope-2",
        name="Legacy South",
        data_source="manual",
        is_mock_data=False,
    )
    db.add_all([first, second])
    db.flush()
    mappings = [
        models.OpenFootballEntityMapping(
            entity_type="team",
            original_name="Legacy United",
            normalized_name="legacy united",
            source_repository=repository,
            resolution_status="ambiguous",
        )
        for repository in ("england", "world")
    ]
    conflict = models.EntityResolutionConflict(
        entity_type="team",
        provider="openfootball",
        source_repository=None,
        source_name="Legacy United",
        normalized_name="legacy united",
        candidate_ids=[first.id, second.id],
        best_score=0.9,
        status="pending",
    )
    db.add_all([*mappings, conflict])
    db.commit()

    listed = list_pending_openfootball_conflicts(db)["entity_conflicts"][0]
    assert listed["scope_status"] == "legacy_ambiguous"
    assert listed["mapping_ids"] == []
    with pytest.raises(OpenFootballConflictStateError, match="varios repositorios"):
        resolve_openfootball_entity_conflict(db, conflict.id, candidate_id=first.id)
    assert all(mapping.internal_entity_id is None for mapping in mappings)


def test_status_conflict_is_listable_and_final_invariants_are_enforced(db: Session) -> None:
    match, record = _match_with_conflict(db)
    record.conflict_details = {
        "fields": [{"field": "status", "existing": "finished", "incoming": "cancelled"}]
    }
    db.commit()

    listed = list_pending_openfootball_conflicts(db)["match_conflicts"][0]
    assert listed["fields"][0]["field"] == "status"
    with pytest.raises(OpenFootballConflictError, match="no puede conservar marcador"):
        resolve_openfootball_match_conflict(
            db, record.id, decisions={"status": "incoming"}
        )
    assert match.status == "finished"
    result = resolve_openfootball_match_conflict(
        db, record.id, decisions={"status": "existing"}
    )
    assert result["conflict_status"] == "resolved"


def test_halftime_cannot_exceed_final_score_after_resolution(db: Session) -> None:
    match, record = _match_with_conflict(db)
    record.conflict_details = {
        "fields": [
            {"field": "halftime_home_score", "existing": None, "incoming": 2},
            {"field": "halftime_away_score", "existing": None, "incoming": 1},
        ]
    }
    db.commit()

    with pytest.raises(OpenFootballConflictError, match="medio tiempo"):
        resolve_openfootball_match_conflict(
            db,
            record.id,
            decisions={
                "halftime_home_score": "incoming",
                "halftime_away_score": "incoming",
            },
        )
    assert match.halftime_home_score is None
    assert record.conflict_status == "conflict"
