"""XGBoost training helpers for the Poisson baseline."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .evaluation import evaluate_goal_counts
from .features import assert_no_temporal_leakage
from .poisson import ExplanationFactor, PoissonPrediction, PoissonPredictor
from .training import (
    DependencyUnavailableError,
    FORBIDDEN_FEATURE_NAMES,
    NON_FEATURE_NAMES,
    TrainingError,
    TrainingMetadata,
    _dataset_hash,
    _date,
    _records,
    _target,
    infer_feature_names,
    validate_feature_names,
)


@dataclass(slots=True)
class TrainedXGBoostGoalsModel:
    home_model: Any
    away_model: Any
    feature_names: tuple[str, ...]
    metadata: TrainingMetadata

    def _vector(self, features: Mapping[str, Any]) -> list[float]:
        values: list[float] = []
        for name in self.feature_names:
            raw = features.get(name)
            try:
                value = float(raw) if raw is not None else float("nan")
            except (TypeError, ValueError):
                value = float("nan")
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
            importances = getattr(estimator, "feature_importances_", None)
            if importances is None:
                continue
            
            contributions = [
                (name, value, float(importance), value * float(importance) if not math.isnan(value) else 0.0)
                for name, value, importance in zip(
                    self.feature_names, vector, importances, strict=True
                )
            ]
            for name, value, importance, contribution in sorted(
                contributions, key=lambda item: abs(item[2]), reverse=True
            )[:top_n]:
                magnitude = "high" if abs(importance) >= 0.1 else "medium"
                if abs(importance) < 0.02:
                    magnitude = "low"
                direction = "positive"
                factors.append(
                    ExplanationFactor(
                        factor=name,
                        value=value if not math.isnan(value) else 0.0,
                        weight=importance,
                        contribution=importance,
                        impact=f"{magnitude}_{direction}",
                        target=target,
                        text=(
                            f"{name}={value if not math.isnan(value) else 'N/A'}, "
                            f"importancia global={importance:.4f} para {target}."
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
            limitations=prediction.limitations + ("Usa XGBoost (no lineal).",),
        )


@dataclass(frozen=True, slots=True)
class XGBoostTrainingReport:
    model: TrainedXGBoostGoalsModel
    metrics: dict[str, Any]
    validation_rows: int


def _require_xgboost() -> Any:
    try:
        from xgboost import XGBRegressor  # type: ignore[import-not-found]
    except Exception as exc:  # XGBoost can raise when the platform OpenMP runtime is absent.
        raise DependencyUnavailableError(
            "XGBoost no está disponible. En macOS instala el runtime OpenMP con: brew install libomp"
        ) from exc
    return XGBRegressor


def _matrix_xgb(
    rows: Sequence[Mapping[str, Any]], feature_names: Sequence[str]
) -> list[list[float]]:
    matrix: list[list[float]] = []
    for row in rows:
        vector: list[float] = []
        for name in feature_names:
            try:
                value = float(row.get(name))
            except (TypeError, ValueError):
                value = float("nan")
            vector.append(value)
        matrix.append(vector)
    return matrix


def train_xgboost_regressors(
    records: Any,
    *,
    feature_names: Sequence[str] | None = None,
    home_target: str = "target_home_goals",
    away_target: str = "target_away_goals",
    date_key: str = "match_date",
    n_estimators: int = 200,
    max_depth: int = 4,
    learning_rate: float = 0.05,
    reg_alpha: float = 0.1,
    reg_lambda: float = 1.0,
    model_version: str = "1.0.0",
    data_source: str = "normalized_local_files",
) -> TrainedXGBoostGoalsModel:
    """Fit two XGBoost regressors in chronological order."""

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
    
    X = _matrix_xgb(rows, names)
    y_home = [_target(row, home_target) for row in rows]
    y_away = [_target(row, away_target) for row in rows]
    
    XGBRegressor = _require_xgboost()
    
    parameters = {
        "n_estimators": int(n_estimators),
        "max_depth": int(max_depth),
        "learning_rate": float(learning_rate),
        "reg_alpha": float(reg_alpha),
        "reg_lambda": float(reg_lambda),
        "objective": "count:poisson",
        "eval_metric": "poisson-nloglik",
        "random_state": 42,
    }
    
    home_model = XGBRegressor(**parameters).fit(X, y_home)
    away_model = XGBRegressor(**parameters).fit(X, y_away)
    
    dates = [_date(row.get(date_key)) for row in rows]
    metadata = TrainingMetadata(
        model_name="goals-xgboost",
        model_version=model_version,
        trained_at=datetime.now(timezone.utc).isoformat(),
        max_training_date=max(dates).isoformat(),
        feature_names=names,
        training_rows=len(rows),
        dataset_hash=_dataset_hash(rows, names, home_target, away_target),
        hyperparameters=parameters,
        data_source=data_source,
    )
    return TrainedXGBoostGoalsModel(home_model, away_model, names, metadata)


def train_evaluate_xgboost(
    records: Any,
    *,
    feature_names: Sequence[str] | None = None,
    validation_fraction: float = 0.20,
    **training_options: Any,
) -> XGBoostTrainingReport:
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
        
    model = train_xgboost_regressors(
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
    
    return XGBoostTrainingReport(model, metrics, len(validation))


def load_xgboost_model(path: str | Path) -> TrainedXGBoostGoalsModel:
    """Load a trusted, locally-produced XGBoost artifact."""

    source = Path(path).expanduser().resolve(strict=True)
    if source.suffix != ".joblib":
        raise TrainingError("Model artifact must use the .joblib extension")
    try:
        import joblib  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on deployment
        raise DependencyUnavailableError("joblib is required to load trained models") from exc
    model = joblib.load(source)
    if not isinstance(model, TrainedXGBoostGoalsModel):
        raise TrainingError("Artifact is not a MatchVision XGBoost goals model")
    return model

__all__ = [
    "TrainedXGBoostGoalsModel",
    "XGBoostTrainingReport",
    "load_xgboost_model",
    "train_evaluate_xgboost",
    "train_xgboost_regressors",
]
