from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from app.ml.features import FeatureBuilder, TemporalLeakageError, assert_no_temporal_leakage


def _history() -> list[dict]:
    return [
        {
            "id": "m1",
            "match_date": "2025-01-01T12:00:00+00:00",
            "competition_id": "c1",
            "home_team_id": "a",
            "away_team_id": "b",
            "home_goals": 1,
            "away_goals": 0,
            "status": "FINISHED",
            "data_source": "local_test",
            "is_mock_data": False,
        },
        {
            "id": "m2",
            "match_date": "2025-01-08T12:00:00+00:00",
            "competition_id": "c1",
            "home_team_id": "b",
            "away_team_id": "a",
            "home_goals": 2,
            "away_goals": 2,
            "status": "FINISHED",
            "data_source": "local_test",
            "is_mock_data": False,
        },
        {
            "id": "target",
            "match_date": "2025-01-15T12:00:00+00:00",
            "competition_id": "c1",
            "home_team_id": "a",
            "away_team_id": "b",
            "status": "SCHEDULED",
            "data_source": "manual",
            "is_mock_data": False,
        },
    ]


def test_future_result_cannot_change_target_features() -> None:
    baseline = FeatureBuilder().build(_history())[-1]
    changed = deepcopy(_history())
    changed.append(
        {
            "id": "future",
            "match_date": "2025-02-01T12:00:00+00:00",
            "competition_id": "c1",
            "home_team_id": "a",
            "away_team_id": "b",
            "home_goals": 99,
            "away_goals": 99,
            "status": "FINISHED",
        }
    )
    candidate = next(row for row in FeatureBuilder().build(changed) if row["match_id"] == "target")
    assert candidate == baseline
    assert candidate["history_contains_mock_data"] is False
    assert candidate["historical_sources"] == ["local_test"]


def test_same_timestamp_does_not_leak() -> None:
    rows = _history()[:2]
    rows[1]["match_date"] = rows[0]["match_date"]
    features = FeatureBuilder().build(rows)
    assert features[0]["home_history_matches"] == 0
    assert features[1]["home_history_matches"] == 0


def test_temporal_auditor_rejects_equal_cutoff() -> None:
    with pytest.raises(TemporalLeakageError):
        assert_no_temporal_leakage(
            [
                {
                    "match_date": datetime(2025, 1, 1, tzinfo=UTC).isoformat(),
                    "_feature_cutoff_at": datetime(2025, 1, 1, tzinfo=UTC).isoformat(),
                    "home_history_max_date": datetime(2025, 1, 1, tzinfo=UTC).isoformat(),
                }
            ]
        )
