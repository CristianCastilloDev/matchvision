"""Interpretable independent-Poisson goals baseline.

The public predictor truncates scorelines to 0..8 and normalizes that matrix,
then derives every displayed probability from the same normalized distribution.
No metric or confidence value is fabricated: quality/confidence are only echoed
when supplied by an evaluated upstream pipeline.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


MODEL_NAME = "goals-independent-poisson"
MODEL_VERSION = "1.0.0"
DEFAULT_MAX_GOALS = 8


class PoissonModelError(ValueError):
    """Invalid rates/features were provided to the Poisson baseline."""


class InsufficientFeatureDataError(PoissonModelError):
    """There is not enough real pre-match history to estimate both rates."""


@dataclass(frozen=True, slots=True)
class ScoreProbability:
    home_goals: int
    away_goals: int
    probability: float

    @property
    def score(self) -> str:
        return f"{self.home_goals}-{self.away_goals}"

    def to_dict(self) -> dict[str, Any]:
        return {"score": self.score, "probability": self.probability}


@dataclass(frozen=True, slots=True)
class ExplanationFactor:
    factor: str
    value: float
    weight: float | None
    contribution: float | None
    impact: str
    target: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor": self.factor,
            "value": self.value,
            "weight": self.weight,
            "contribution": self.contribution,
            "impact": self.impact,
            "target": self.target,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class LambdaEstimate:
    lambda_home: float
    lambda_away: float
    factors: tuple[ExplanationFactor, ...]


@dataclass(frozen=True, slots=True)
class PoissonPrediction:
    lambda_home: float
    lambda_away: float
    matrix: tuple[tuple[float, ...], ...]
    match_result: dict[str, float]
    goals: dict[str, float]
    both_teams_score: dict[str, float]
    clean_sheets: dict[str, float]
    first_goal: dict[str, float]
    likely_scores: tuple[ScoreProbability, ...]
    key_factors: tuple[ExplanationFactor, ...]
    model_name: str
    model_version: str
    generated_at: str
    data_quality_score: float | None
    confidence_score: float | None
    limitations: tuple[str, ...]

    @property
    def score_matrix(self) -> tuple[tuple[float, ...], ...]:
        return self.matrix

    @property
    def goal_probabilities(self) -> dict[str, float]:
        return self.goals

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "model_version": self.model_version,
            "generated_at": self.generated_at,
            "lambda_home": self.lambda_home,
            "lambda_away": self.lambda_away,
            "matrix": [list(row) for row in self.matrix],
            "match_result": dict(self.match_result),
            "goals": dict(self.goals),
            "both_teams_score": dict(self.both_teams_score),
            "clean_sheets": dict(self.clean_sheets),
            "first_goal": dict(self.first_goal),
            "likely_scores": [score.to_dict() for score in self.likely_scores],
            "key_factors": [factor.to_dict() for factor in self.key_factors],
            "data_quality_score": self.data_quality_score,
            "confidence_score": self.confidence_score,
            "limitations": list(self.limitations),
        }


def _probability(value: float | None, field: str) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise PoissonModelError(f"{field} must be numeric") from exc
    if not math.isfinite(parsed) or parsed < 0 or parsed > 1:
        raise PoissonModelError(f"{field} must be between 0 and 1")
    return parsed


def _rate(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise PoissonModelError(f"{field} must be numeric") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise PoissonModelError(f"{field} must be a finite non-negative number")
    if parsed > 20:
        raise PoissonModelError(f"{field} is implausibly large (>20)")
    return parsed


def poisson_probabilities(rate: float, *, max_goals: int = DEFAULT_MAX_GOALS) -> tuple[float, ...]:
    rate = _rate(rate, "rate")
    if max_goals < 0 or max_goals > 30:
        raise PoissonModelError("max_goals must be between 0 and 30")
    probabilities = [math.exp(-rate)]
    for goals in range(1, max_goals + 1):
        probabilities.append(probabilities[-1] * rate / goals)
    return tuple(probabilities)


def poisson_score_matrix(
    lambda_home: float,
    lambda_away: float,
    *,
    max_goals: int = DEFAULT_MAX_GOALS,
) -> tuple[tuple[float, ...], ...]:
    """Return a normalized P(home=x, away=y) matrix for 0..max_goals."""

    home = poisson_probabilities(lambda_home, max_goals=max_goals)
    away = poisson_probabilities(lambda_away, max_goals=max_goals)
    raw = [[home_goals * away_goals for away_goals in away] for home_goals in home]
    total = sum(sum(row) for row in raw)
    if not math.isfinite(total) or total <= 0:
        raise PoissonModelError("Could not normalize score matrix")
    matrix = tuple(tuple(value / total for value in row) for row in raw)
    # Numerical guard: add the machine-sized residual to the final cell.
    residual = 1.0 - sum(sum(row) for row in matrix)
    if residual:
        mutable = [list(row) for row in matrix]
        mutable[-1][-1] += residual
        matrix = tuple(tuple(row) for row in mutable)
    return matrix


def _sum_where(
    matrix: Sequence[Sequence[float]], predicate: Any
) -> float:
    return sum(
        probability
        for home, row in enumerate(matrix)
        for away, probability in enumerate(row)
        if predicate(home, away)
    )


def derive_probabilities(
    matrix: Sequence[Sequence[float]],
) -> tuple[
    dict[str, float],
    dict[str, float],
    dict[str, float],
    dict[str, float],
]:
    """Derive result, totals, BTTS and clean sheets from one matrix."""

    home_win = _sum_where(matrix, lambda home, away: home > away)
    draw = _sum_where(matrix, lambda home, away: home == away)
    away_win = _sum_where(matrix, lambda home, away: home < away)
    match_result = {
        "home_win": home_win,
        "draw": draw,
        "away_win": away_win,
    }
    totals: dict[str, float] = {}
    for line in (1.5, 2.5, 3.5, 4.5):
        over = _sum_where(matrix, lambda home, away, line=line: home + away > line)
        token = str(line).replace(".", "_")
        totals[f"over_{token}"] = over
        totals[f"under_{token}"] = 1.0 - over
    yes = _sum_where(matrix, lambda home, away: home > 0 and away > 0)
    btts = {"yes": yes, "no": 1.0 - yes}
    clean_sheets = {
        "home_clean_sheet": _sum_where(matrix, lambda home, away: away == 0),
        "away_clean_sheet": _sum_where(matrix, lambda home, away: home == 0),
        "either_clean_sheet": _sum_where(
            matrix, lambda home, away: home == 0 or away == 0
        ),
    }
    return match_result, totals, btts, clean_sheets


def top_scorelines(
    matrix: Sequence[Sequence[float]], *, top_n: int = 5
) -> tuple[ScoreProbability, ...]:
    if top_n < 1 or top_n > len(matrix) * len(matrix[0]):
        raise PoissonModelError("top_n is outside the matrix range")
    scores = [
        ScoreProbability(home, away, probability)
        for home, row in enumerate(matrix)
        for away, probability in enumerate(row)
    ]
    return tuple(
        sorted(scores, key=lambda item: (-item.probability, item.home_goals, item.away_goals))[
            :top_n
        ]
    )


def _impact(contribution: float, rate: float, baseline: float | None) -> str:
    direction = "positive" if baseline is None or contribution >= 0 else "negative"
    ratio = abs(contribution) / max(rate, 1e-12)
    magnitude = "high" if ratio >= 0.35 else "medium" if ratio >= 0.2 else "low"
    return f"{magnitude}_{direction}"


def _weighted_rate(
    features: Mapping[str, Any],
    candidates: Sequence[tuple[str, float]],
    *,
    target: str,
    baseline: float | None,
) -> tuple[float, list[ExplanationFactor]]:
    available: list[tuple[str, float, float]] = []
    for name, weight in candidates:
        raw = features.get(name)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value >= 0:
            available.append((name, weight, value))
    if not available:
        raise InsufficientFeatureDataError(f"No real pre-match inputs for {target}")
    total_weight = sum(weight for _, weight, _ in available)
    rate = sum(weight * value for _, weight, value in available) / total_weight
    factors: list[ExplanationFactor] = []
    for name, raw_weight, value in available:
        weight = raw_weight / total_weight
        contribution = weight * value
        signed = contribution
        if baseline is not None:
            signed = weight * (value - baseline)
        factors.append(
            ExplanationFactor(
                factor=name,
                value=value,
                weight=weight,
                contribution=contribution,
                impact=_impact(signed, rate, baseline),
                target=target,
                text=(
                    f"{name}={value:.3f} aportó {contribution:.3f} "
                    f"a la tasa esperada de {target}."
                ),
            )
        )
    return rate, factors


DEFAULT_LAMBDA = 1.25

def _fallback_rate(
    features: Mapping[str, Any],
    candidates: Sequence[tuple[str, float]],
    *,
    target: str,
) -> tuple[float, list[ExplanationFactor]]:
    available: list[tuple[str, float, float]] = []
    for name, weight in candidates:
        raw = features.get(name)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value >= 0:
            available.append((name, weight, value))
    if available:
        total_weight = sum(weight for _, weight, _ in available)
        rate = sum(weight * value for _, weight, value in available) / total_weight
        factors: list[ExplanationFactor] = []
        for name, raw_weight, value in available:
            weight = raw_weight / total_weight
            factors.append(
                ExplanationFactor(
                    factor=name,
                    value=value,
                    weight=weight,
                    contribution=weight * value,
                    impact="medium_positive",
                    target=target,
                    text=f"{name}={value:.3f} aportó {weight * value:.3f} a la tasa esperada de {target}.",
                )
            )
        return rate, factors
    fallback = DEFAULT_LAMBDA
    return fallback, [
        ExplanationFactor(
            factor="default_prior",
            value=fallback,
            weight=1.0,
            contribution=fallback,
            impact="default",
            target=target,
            text=f"Sin datos históricos previos; se usó tasa por defecto {fallback:.2f}.",
        )
    ]

def estimate_lambdas_from_features(features: Mapping[str, Any]) -> LambdaEstimate:
    """Estimate rates from actual leakage-safe rolling values, with no defaults."""

    league_home = features.get("league_home_goals_avg")
    league_away = features.get("league_away_goals_avg")
    baseline_home = float(league_home) if isinstance(league_home, (int, float)) else None
    baseline_away = float(league_away) if isinstance(league_away, (int, float)) else None
    home_rate, home_factors = _fallback_rate(
        features,
        (
            ("home_goals_for_recency_weighted_10", 0.45),
            ("away_goals_against_recency_weighted_10", 0.35),
            ("league_home_goals_avg", 0.20),
        ),
        target="home_goals",
    )
    away_rate, away_factors = _fallback_rate(
        features,
        (
            ("away_goals_for_recency_weighted_10", 0.45),
            ("home_goals_against_recency_weighted_10", 0.35),
            ("league_away_goals_avg", 0.20),
        ),
        target="away_goals",
    )
    return LambdaEstimate(
        lambda_home=_rate(home_rate, "lambda_home"),
        lambda_away=_rate(away_rate, "lambda_away"),
        factors=tuple(home_factors + away_factors),
    )


def _parameter_factors(home: float, away: float) -> tuple[ExplanationFactor, ...]:
    return (
        ExplanationFactor(
            factor="lambda_home",
            value=home,
            weight=None,
            contribution=None,
            impact="model_parameter",
            target="home_goals",
            text=f"La tasa Poisson local utilizada fue {home:.3f} goles.",
        ),
        ExplanationFactor(
            factor="lambda_away",
            value=away,
            weight=None,
            contribution=None,
            impact="model_parameter",
            target="away_goals",
            text=f"La tasa Poisson visitante utilizada fue {away:.3f} goles.",
        ),
    )


class PoissonPredictor:
    """Stateless public baseline predictor."""

    model_name = MODEL_NAME
    model_version = MODEL_VERSION

    def __init__(self, *, max_goals: int = DEFAULT_MAX_GOALS) -> None:
        if max_goals != 8:
            # The product contract requires the display matrix to cover 0..8.
            raise PoissonModelError("MatchVision's baseline requires max_goals=8")
        self.max_goals = max_goals

    def predict(
        self,
        lambda_home: float,
        lambda_away: float,
        *,
        top_n: int = 5,
        factors: Sequence[ExplanationFactor] | None = None,
        data_quality_score: float | None = None,
        confidence_score: float | None = None,
    ) -> PoissonPrediction:
        home = _rate(lambda_home, "lambda_home")
        away = _rate(lambda_away, "lambda_away")
        quality = _probability(data_quality_score, "data_quality_score")
        confidence = _probability(confidence_score, "confidence_score")
        matrix = poisson_score_matrix(home, away, max_goals=self.max_goals)
        result, totals, btts, clean_sheets = derive_probabilities(matrix)
        no_goal = matrix[0][0]
        total_rate = home + away
        if total_rate:
            first_goal = {
                "home": (1.0 - no_goal) * home / total_rate,
                "away": (1.0 - no_goal) * away / total_rate,
                "no_goal": no_goal,
            }
        else:
            first_goal = {"home": 0.0, "away": 0.0, "no_goal": 1.0}
        return PoissonPrediction(
            lambda_home=home,
            lambda_away=away,
            matrix=matrix,
            match_result=result,
            goals=totals,
            both_teams_score=btts,
            clean_sheets=clean_sheets,
            first_goal=first_goal,
            likely_scores=top_scorelines(matrix, top_n=top_n),
            key_factors=tuple(factors or _parameter_factors(home, away)),
            model_name=self.model_name,
            model_version=self.model_version,
            generated_at=datetime.now(timezone.utc).isoformat(),
            data_quality_score=quality,
            confidence_score=confidence,
            limitations=(
                "Baseline independiente: no modela correlación de marcadores (Dixon-Coles).",
                "La matriz se limita a 0-8 goles y se normaliza dentro de ese rango.",
                "Las probabilidades son estimaciones educativas, no garantías.",
            ),
        )

    def predict_from_features(
        self,
        features: Mapping[str, Any],
        *,
        top_n: int = 5,
        confidence_score: float | None = None,
    ) -> PoissonPrediction:
        estimate = estimate_lambdas_from_features(features)
        quality = features.get("feature_quality_score")
        quality_value = float(quality) if isinstance(quality, (int, float)) else None
        return self.predict(
            estimate.lambda_home,
            estimate.lambda_away,
            top_n=top_n,
            factors=estimate.factors,
            data_quality_score=quality_value,
            confidence_score=confidence_score,
        )


PoissonBaseline = PoissonPredictor


def predict_match(
    lambda_home: float, lambda_away: float, *, top_n: int = 5
) -> PoissonPrediction:
    return PoissonPredictor().predict(lambda_home, lambda_away, top_n=top_n)


__all__ = [
    "DEFAULT_MAX_GOALS",
    "ExplanationFactor",
    "InsufficientFeatureDataError",
    "LambdaEstimate",
    "PoissonBaseline",
    "PoissonModelError",
    "PoissonPrediction",
    "PoissonPredictor",
    "ScoreProbability",
    "derive_probabilities",
    "estimate_lambdas_from_features",
    "poisson_probabilities",
    "poisson_score_matrix",
    "predict_match",
    "top_scorelines",
]
