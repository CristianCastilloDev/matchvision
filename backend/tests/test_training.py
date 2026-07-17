from __future__ import annotations

from datetime import date, timedelta

from app.ml.features import FeatureBuilder
from app.ml.training import infer_feature_names, train_evaluate_poisson


def _openfootball_only_history() -> list[dict]:
    start = date(2025, 1, 1)
    teams = ("A", "B", "C", "D")
    rows: list[dict] = []
    for index in range(16):
        home = teams[index % len(teams)]
        away = teams[(index + 1) % len(teams)]
        rows.append(
            {
                "id": f"m-{index}",
                "match_date": (start + timedelta(days=index * 7)).isoformat(),
                "competition_id": "openfootball-league",
                "home_team_id": home,
                "away_team_id": away,
                "home_goals": index % 4,
                "away_goals": (index + 2) % 3,
                "status": "FINISHED",
                "data_source": "openfootball",
                "is_mock_data": False,
            }
        )
    rows.append(
        {
            "id": "future",
            "match_date": (start + timedelta(days=16 * 7)).isoformat(),
            "competition_id": "openfootball-league",
            "home_team_id": "A",
            "away_team_id": "B",
            "status": "SCHEDULED",
            "data_source": "openfootball",
            "is_mock_data": False,
        }
    )
    return rows


def test_openfootball_results_train_poisson_without_optional_stats() -> None:
    features = FeatureBuilder().build(_openfootball_only_history())
    training = [
        row
        for row in features
        if row.get("target_home_goals") is not None
        and row.get("target_away_goals") is not None
    ]
    assert len(features) == 17
    assert len(training) == 16
    names = infer_feature_names(training)
    assert "home_shots_avg_3" not in names
    assert "home_elo" in names
    report = train_evaluate_poisson(training, model_version="openfootball-test")
    assert report.validation_rows > 0
    assert report.model.metadata.data_source == "normalized_local_files"
