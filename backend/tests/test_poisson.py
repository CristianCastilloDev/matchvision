from __future__ import annotations

import math

import pytest

from app.ml.poisson import PoissonModelError, PoissonPredictor, poisson_score_matrix


def test_score_matrix_is_9_by_9_and_normalized() -> None:
    matrix = poisson_score_matrix(1.72, 1.21)
    assert len(matrix) == 9
    assert all(len(row) == 9 for row in matrix)
    assert math.isclose(sum(map(sum, matrix)), 1.0, abs_tol=1e-12)
    assert all(0 <= probability <= 1 for row in matrix for probability in row)


def test_all_derived_probabilities_are_coherent() -> None:
    prediction = PoissonPredictor().predict(1.72, 1.21)
    assert math.isclose(sum(prediction.match_result.values()), 1.0, abs_tol=1e-12)
    assert math.isclose(sum(prediction.first_goal.values()), 1.0, abs_tol=1e-12)
    assert math.isclose(
        prediction.goals["over_2_5"] + prediction.goals["under_2_5"],
        1.0,
        abs_tol=1e-12,
    )
    assert prediction.likely_scores == tuple(
        sorted(
            prediction.likely_scores,
            key=lambda item: (-item.probability, item.home_goals, item.away_goals),
        )
    )


def test_invalid_rates_are_rejected() -> None:
    with pytest.raises(PoissonModelError):
        PoissonPredictor().predict(-0.1, 1.0)
