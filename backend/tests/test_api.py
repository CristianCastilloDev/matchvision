from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient


def test_offline_prediction_result_and_immutable_snapshot(client: TestClient) -> None:
    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["mode"] == "offline"

    upcoming = client.get("/api/v1/matches/upcoming")
    assert upcoming.status_code == 200
    demo = next(match for match in upcoming.json() if match["is_mock_data"])

    predicted = client.post(
        "/api/v1/predictions/match",
        json={"match_id": demo["id"], "use_confirmed_lineups": False},
    )
    assert predicted.status_code == 200, predicted.text
    snapshot = predicted.json()
    assert snapshot["analysis"]["confidence_score"] == 0
    assert snapshot["analysis"]["confidence_method"] == "unavailable"
    assert snapshot["analysis"]["history_contains_mock_data"] is True
    assert snapshot["analysis"]["matches_used"] > 0
    assert snapshot["match"]["is_mock_data"] is True
    assert abs(sum(snapshot["match_result"].values()) - 1.0) < 1e-9
    required = {
        "match",
        "analysis",
        "match_result",
        "goals",
        "likely_scores",
        "cards",
        "likely_scorers",
        "card_risks",
        "other_events",
        "key_factors",
        "warnings",
        "disclaimer",
    }
    assert required <= snapshot.keys()

    structural_patch = client.patch(
        f"/api/v1/matches/{demo['id']}",
        json={"match_date": (datetime.now(UTC) + timedelta(days=10)).isoformat()},
    )
    assert structural_patch.status_code == 409

    result = client.post(
        f"/api/v1/matches/{demo['id']}/result",
        json={"home_score": 2, "away_score": 1, "halftime_home_score": 1, "halftime_away_score": 0},
    )
    assert result.status_code == 200, result.text
    retry = client.post(
        f"/api/v1/matches/{demo['id']}/result",
        json={"home_score": 2, "away_score": 1, "halftime_home_score": 1, "halftime_away_score": 0},
    )
    assert retry.status_code == 200
    saved = client.get(f"/api/v1/predictions/{snapshot['prediction_id']}")
    assert saved.status_code == 200
    assert saved.json() == snapshot
    history = client.get("/api/v1/predictions/history").json()
    item = next(row for row in history if row["id"] == snapshot["prediction_id"])
    assert item["status"] in {"correct", "incorrect"}
    assert client.post("/api/v1/predictions/match", json={"match_id": demo["id"]}).status_code == 422


def test_manual_names_create_entities_but_no_fake_prior(client: TestClient) -> None:
    response = client.post(
        "/api/v1/matches/manual",
        json={
            "competition": "Competición manual aislada",
            "season": "2026",
            "home_team": "Equipo Manual Norte",
            "away_team": "Equipo Manual Sur",
            "kickoff": (datetime.now(UTC) + timedelta(days=4)).isoformat(),
            "stadium": "Estadio local",
            "weather": {"temperature_c": 25},
            "importance": "amistoso educativo",
            "lineups": [
                {
                    "player_name": "Delantero Norte",
                    "team": "home",
                    "started": True,
                    "expected_minutes": 80,
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    match = response.json()
    assert match["data_source"] == "manual"
    assert match["weather"]["temperature_c"] == 25
    prediction = client.post("/api/v1/predictions/match", json={"match_id": match["id"]})
    assert prediction.status_code == 422
    assert "historial" in prediction.json()["detail"].lower()


def test_real_manual_fixture_never_reuses_demo_entities(client: TestClient) -> None:
    response = client.post(
        "/api/v1/matches/manual",
        json={
            "competition": "Liga Demostración",
            "season": "2026 Demo",
            "home_team": "Atlético Aurora",
            "away_team": "Deportivo Pacífico",
            "kickoff": (datetime.now(UTC) + timedelta(days=5)).isoformat(),
            "is_mock_data": False,
        },
    )
    assert response.status_code == 201, response.text
    match = response.json()
    assert match["is_mock_data"] is False
    assert match["data_source"] == "manual"

    prediction = client.post("/api/v1/predictions/match", json={"match_id": match["id"]})
    assert prediction.status_code == 422
    assert "historial" in prediction.json()["detail"].lower()


def test_template_endpoint_uses_exact_contract(client: TestClient) -> None:
    response = client.get("/api/v1/imports/templates/player_matches")
    assert response.status_code == 200
    assert response.text == (
        "match_id,player_name,team,started,minutes_played,goals,shots,shots_on_target,"
        "xg,yellow_cards,red_cards\n"
    )
