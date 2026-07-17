"""Optional scikit-learn training helpers for the Poisson baseline.

Inference from explicit lambdas never requires scikit-learn.  These helpers load
scikit-learn/joblib lazily so offline ingestion and CI smoke tests still work in a
minimal Python environment.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any
from uuid import uuid4

from .evaluation import evaluate_goal_counts
from .features import assert_no_temporal_leakage
from .poisson import ExplanationFactor, PoissonPrediction, PoissonPredictor


FORBIDDEN_FEATURE_NAMES = frozenset(
    {
        "home_goals",
        "away_goals",
        "home_score",
        "away_score",
        "fulltime_home_score",
        "fulltime_away_score",
        "target_home_goals",
        "target_away_goals",
        "fulltime_result",
        "actual_outcome",
        "result",
    }
)
NON_FEATURE_NAMES = frozenset(
    {
        "match_id",
        "match_date",
        "home_team_key",
        "away_team_key",
        "competition_key",
        "data_source",
        "home_history_max_date",
        "historical_sources",
        "history_contains_mock_data",
        "is_mock_data",
        "away_history_max_date",
        "_feature_cutoff_at",
    }
)


class TrainingError(ValueError):
    """Training data or artifact settings are invalid."""


class DependencyUnavailableError(TrainingError, RuntimeError):
    """Optional sklearn/joblib dependency is not installed."""


@dataclass(frozen=True, slots=True)
class TrainingMetadata:
    model_name: str
    model_version: str
    trained_at: str
    max_training_date: str
    feature_names: tuple[str, ...]
    training_rows: int
    dataset_hash: str
    hyperparameters: dict[str, Any]
    data_source: str


@dataclass(slots=True)
class TrainedPoissonGoalsModel:
    home_model: Any
    away_model: Any
    feature_names: tuple[str, ...]
    medians: dict[str, float]
    metadata: TrainingMetadata

    def _vector(self, features: Mapping[str, Any]) -> list[float]:
        values: list[float] = []
        for name in self.feature_names:
            raw = features.get(name)
            try:
                value = float(raw) if raw is not None else self.medians[name]
            except (TypeError, ValueError):
                value = self.medians[name]
            if not math.isfinite(value):
                value = self.medians[name]
            values.append(value)
        return values

    def predict_lambdas(self, features: Mapping[str, Any]) -> tuple[float, float]:
        vector = [self._vector(features)]
        home = float(self.home_model.predict(vector)[0])
        away = float(self.away_model.predict(vector)[0])
        if not math.isfinite(home) or not math.isfinite(away) or home < 0 or away < 0:
            raise TrainingError("Trained model returned an invalid Poisson rate")
        return home, away

    def explain(
        self, features: Mapping[str, Any], *, top_n: int = 6
    ) -> tuple[ExplanationFactor, ...]:
        vector = self._vector(features)
        factors: list[ExplanationFactor] = []
        for target, estimator in (
            ("home_goals", self.home_model),
            ("away_goals", self.away_model),
        ):
            coefficients = getattr(estimator, "coef_", None)
            if coefficients is None:
                continue
            contributions = [
                (name, value, float(coefficient), value * float(coefficient))
                for name, value, coefficient in zip(
                    self.feature_names, vector, coefficients, strict=True
                )
            ]
            for name, value, coefficient, contribution in sorted(
                contributions, key=lambda item: abs(item[3]), reverse=True
            )[:top_n]:
                magnitude = "high" if abs(contribution) >= 0.5 else "medium"
                if abs(contribution) < 0.2:
                    magnitude = "low"
                direction = "positive" if contribution >= 0 else "negative"
                factors.append(
                    ExplanationFactor(
                        factor=name,
                        value=value,
                        weight=coefficient,
                        contribution=contribution,
                        impact=f"{magnitude}_{direction}",
                        target=target,
                        text=(
                            f"{name}={value:.3f}, coeficiente={coefficient:.4f}, "
                            f"contribución lineal={contribution:.4f} para {target}."
                        ),
                    )
                )
        return tuple(factors)

    def predict(
        self,
        features: Mapping[str, Any],
        *,
        top_n: int = 5,
        data_quality_score: float | None = None,
        confidence_score: float | None = None,
    ) -> PoissonPrediction:
        home, away = self.predict_lambdas(features)
        factors = self.explain(features)
        prediction = PoissonPredictor().predict(
            home,
            away,
            top_n=top_n,
            factors=factors,
            data_quality_score=data_quality_score,
            confidence_score=confidence_score,
        )
        return replace(
            prediction,
            model_name=self.metadata.model_name,
            model_version=self.metadata.model_version,
        )


@dataclass(frozen=True, slots=True)
class PoissonTrainingReport:
    model: TrainedPoissonGoalsModel
    metrics: dict[str, Any]
    validation_rows: int


def _records(rows: Any) -> list[dict[str, Any]]:
    if hasattr(rows, "to_dict") and not isinstance(rows, Mapping):
        try:
            return [dict(row) for row in rows.to_dict(orient="records")]
        except TypeError:
            pass
    return [dict(row) for row in rows]


def _date(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise TrainingError(f"Invalid training date: {value!r}") from exc
    else:
        raise TrainingError(f"Missing training date: {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_feature_names(feature_names: Sequence[str]) -> tuple[str, ...]:
    names = tuple(str(name) for name in feature_names)
    if not names or len(names) != len(set(names)):
        raise TrainingError("feature_names must be unique and non-empty")
    forbidden = [
        name
        for name in names
        if name in FORBIDDEN_FEATURE_NAMES
        or name.startswith("target_")
        or name.startswith("actual_")
        or name.endswith("_history_max_date")
        or name.startswith("_")
    ]
    if forbidden:
        raise TrainingError(
            "Outcome/audit columns cannot be model features: " + ", ".join(forbidden)
        )
    return names


def infer_feature_names(rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    candidates: set[str] = set()
    for row in rows:
        for name, value in row.items():
            if name in NON_FEATURE_NAMES or name in FORBIDDEN_FEATURE_NAMES:
                continue
            if name.startswith("_") or name.endswith("_history_max_date"):
                continue
            if isinstance(value, bool):
                candidates.add(name)
            elif isinstance(value, int | float) and math.isfinite(float(value)):
                candidates.add(name)
    return validate_feature_names(sorted(candidates))


def _target(row: Mapping[str, Any], key: str) -> float:
    value = row.get(key)
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise TrainingError(f"Missing/non-numeric target {key}") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise TrainingError(f"Invalid target {key}")
    return parsed


def _matrix(
    rows: Sequence[Mapping[str, Any]], feature_names: Sequence[str]
) -> tuple[list[list[float]], dict[str, float]]:
    medians: dict[str, float] = {}
    for name in feature_names:
        values: list[float] = []
        for row in rows:
            try:
                value = float(row.get(name))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                values.append(value)
        if not values:
            raise TrainingError(f"Feature {name!r} has no observed training values")
        medians[name] = float(median(values))
    matrix: list[list[float]] = []
    for row in rows:
        vector: list[float] = []
        for name in feature_names:
            try:
                value = float(row.get(name))
            except (TypeError, ValueError):
                value = medians[name]
            if not math.isfinite(value):
                value = medians[name]
            vector.append(value)
        matrix.append(vector)
    return matrix, medians


def _dataset_hash(
    rows: Sequence[Mapping[str, Any]],
    feature_names: Sequence[str],
    home_target: str,
    away_target: str,
) -> str:
    selected = [
        {
            **{name: row.get(name) for name in feature_names},
            home_target: row.get(home_target),
            away_target: row.get(away_target),
            "match_date": row.get("match_date"),
        }
        for row in rows
    ]
    payload = json.dumps(selected, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_sklearn() -> Any:
    try:
        from sklearn.linear_model import PoissonRegressor  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise DependencyUnavailableError(
            "scikit-learn is required only for model training"
        ) from exc
    return PoissonRegressor


def train_poisson_regressors(
    records: Any,
    *,
    feature_names: Sequence[str] | None = None,
    home_target: str = "target_home_goals",
    away_target: str = "target_away_goals",
    date_key: str = "match_date",
    alpha: float = 0.1,
    max_iter: int = 1000,
    model_version: str = "1.0.0",
    data_source: str = "normalized_local_files",
) -> TrainedPoissonGoalsModel:
    """Fit two deterministic sklearn Poisson regressors in chronological order."""

    if alpha < 0 or max_iter < 1:
        raise TrainingError("alpha must be non-negative and max_iter positive")
    rows = _records(records)
    if len(rows) < 3:
        raise TrainingError("At least three completed matches are required")
    assert_no_temporal_leakage(rows)
    rows.sort(key=lambda row: _date(row.get(date_key)))
    if feature_names is not None:
        names = validate_feature_names(feature_names)
    else:
        names = validate_feature_names(
            tuple(
                name
                for name in infer_feature_names(rows)
                if name not in {home_target, away_target}
            )
        )
    X, medians = _matrix(rows, names)
    y_home = [_target(row, home_target) for row in rows]
    y_away = [_target(row, away_target) for row in rows]
    PoissonRegressor = _require_sklearn()
    parameters = {"alpha": float(alpha), "max_iter": int(max_iter), "fit_intercept": True}
    home_model = PoissonRegressor(**parameters).fit(X, y_home)
    away_model = PoissonRegressor(**parameters).fit(X, y_away)
    dates = [_date(row.get(date_key)) for row in rows]
    metadata = TrainingMetadata(
        model_name="goals-poisson-regression",
        model_version=model_version,
        trained_at=datetime.now(timezone.utc).isoformat(),
        max_training_date=max(dates).isoformat(),
        feature_names=names,
        training_rows=len(rows),
        dataset_hash=_dataset_hash(rows, names, home_target, away_target),
        hyperparameters=parameters,
        data_source=data_source,
    )
    return TrainedPoissonGoalsModel(home_model, away_model, names, medians, metadata)


def train_evaluate_poisson(
    records: Any,
    *,
    feature_names: Sequence[str] | None = None,
    validation_fraction: float = 0.20,
    **training_options: Any,
) -> PoissonTrainingReport:
    """Fit on the oldest rows and evaluate once on a newer untouched holdout."""

    if not 0 < validation_fraction < 0.5:
        raise TrainingError("validation_fraction must be between 0 and 0.5")
    rows = _records(records)
    rows.sort(key=lambda row: _date(row.get("match_date")))
    timestamps = sorted({_date(row.get("match_date")) for row in rows})
    if len(timestamps) < 2:
        raise TrainingError("At least two distinct timestamps are required")
    validation_timestamp_count = max(1, int(len(timestamps) * validation_fraction))
    cutoff = timestamps[-validation_timestamp_count]
    train = [row for row in rows if _date(row.get("match_date")) < cutoff]
    validation = [row for row in rows if _date(row.get("match_date")) >= cutoff]
    if len(train) < 3 or not validation:
        raise TrainingError("Not enough rows for a chronological validation holdout")
    model = train_poisson_regressors(
        train, feature_names=feature_names, **training_options
    )
    predicted = [model.predict_lambdas(row) for row in validation]
    home_target = str(training_options.get("home_target", "target_home_goals"))
    away_target = str(training_options.get("away_target", "target_away_goals"))
    metrics = evaluate_goal_counts(
        (row.get(home_target) for row in validation),
        (row.get(away_target) for row in validation),
        (item[0] for item in predicted),
        (item[1] for item in predicted),
    )
    return PoissonTrainingReport(model, metrics, len(validation))


def _require_joblib() -> Any:
    try:
        import joblib  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise DependencyUnavailableError("joblib is required to persist trained models") from exc
    return joblib


def save_model(model: TrainedPoissonGoalsModel, path: str | Path) -> Path:
    """Atomically persist a model artifact.  Never overwrite it silently."""

    destination = Path(path).expanduser()
    if destination.suffix != ".joblib":
        raise TrainingError("Model artifact must use the .joblib extension")
    if destination.exists():
        raise FileExistsError(f"Model artifact already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    joblib = _require_joblib()
    try:
        joblib.dump(model, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def load_model(path: str | Path) -> TrainedPoissonGoalsModel:
    """Load a trusted local artifact (joblib files must never be untrusted)."""

    source = Path(path).expanduser().resolve(strict=True)
    if source.suffix != ".joblib":
        raise TrainingError("Model artifact must use the .joblib extension")
    model = _require_joblib().load(source)
    if not isinstance(model, TrainedPoissonGoalsModel):
        raise TrainingError("Artifact is not a MatchVision Poisson goals model")
    return model


__all__ = [
    "DependencyUnavailableError",
    "FORBIDDEN_FEATURE_NAMES",
    "PoissonTrainingReport",
    "TrainedPoissonGoalsModel",
    "TrainingError",
    "TrainingMetadata",
    "infer_feature_names",
    "load_model",
    "save_model",
    "train_evaluate_poisson",
    "train_poisson_regressors",
    "validate_feature_names",
]
