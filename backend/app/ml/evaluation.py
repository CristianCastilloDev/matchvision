"""Chronological splitting and dependency-free probability/count metrics."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any


class EvaluationError(ValueError):
    """Evaluation inputs are inconsistent or temporally unsafe."""


@dataclass(frozen=True, slots=True)
class TemporalSplit:
    train: tuple[dict[str, Any], ...]
    validation: tuple[dict[str, Any], ...]
    test: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    fold: int
    train: tuple[dict[str, Any], ...]
    test: tuple[dict[str, Any], ...]


def _datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise EvaluationError(f"Invalid chronological date: {value!r}") from exc
    else:
        raise EvaluationError(f"Missing chronological date: {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _records(rows: Any) -> list[dict[str, Any]]:
    if hasattr(rows, "to_dict") and not isinstance(rows, Mapping):
        try:
            return [dict(row) for row in rows.to_dict(orient="records")]
        except TypeError:
            pass
    return [dict(row) for row in rows]


def _sorted_groups(
    records: Iterable[Mapping[str, Any]], date_key: str
) -> list[tuple[datetime, list[dict[str, Any]]]]:
    ordered = sorted(
        (dict(row) for row in records), key=lambda row: _datetime(row.get(date_key))
    )
    groups: list[tuple[datetime, list[dict[str, Any]]]] = []
    for row in ordered:
        timestamp = _datetime(row.get(date_key))
        if not groups or groups[-1][0] != timestamp:
            groups.append((timestamp, []))
        groups[-1][1].append(row)
    return groups


def chronological_split(
    records: Any,
    *,
    date_key: str = "match_date",
    train_fraction: float = 0.70,
    validation_fraction: float = 0.15,
) -> TemporalSplit:
    """Split by whole timestamps, never placing same-time games across sets."""

    if not 0 < train_fraction < 1:
        raise EvaluationError("train_fraction must be between 0 and 1")
    if not 0 < validation_fraction < 1:
        raise EvaluationError("validation_fraction must be between 0 and 1")
    if train_fraction + validation_fraction >= 1:
        raise EvaluationError("train + validation fractions must be below 1")
    groups = _sorted_groups(_records(records), date_key)
    if len(groups) < 3:
        raise EvaluationError("At least three distinct timestamps are required")
    train_end = max(1, min(len(groups) - 2, int(len(groups) * train_fraction)))
    validation_count = max(1, int(len(groups) * validation_fraction))
    validation_end = min(len(groups) - 1, train_end + validation_count)
    train = tuple(row for _, group in groups[:train_end] for row in group)
    validation = tuple(
        row for _, group in groups[train_end:validation_end] for row in group
    )
    test = tuple(row for _, group in groups[validation_end:] for row in group)
    split = TemporalSplit(train, validation, test)
    assert_temporal_order(split, date_key=date_key)
    return split


def assert_temporal_order(split: TemporalSplit, *, date_key: str = "match_date") -> None:
    for name, rows in (
        ("train", split.train),
        ("validation", split.validation),
        ("test", split.test),
    ):
        if not rows:
            raise EvaluationError(f"{name} split is empty")
    train_max = max(_datetime(row.get(date_key)) for row in split.train)
    validation_min = min(_datetime(row.get(date_key)) for row in split.validation)
    validation_max = max(_datetime(row.get(date_key)) for row in split.validation)
    test_min = min(_datetime(row.get(date_key)) for row in split.test)
    if train_max >= validation_min or validation_max >= test_min:
        raise EvaluationError("Chronological split overlaps at a boundary")


def walk_forward_splits(
    records: Any,
    *,
    date_key: str = "match_date",
    initial_train_timestamps: int,
    test_timestamps: int = 1,
    step_timestamps: int = 1,
) -> tuple[WalkForwardFold, ...]:
    if initial_train_timestamps < 1 or test_timestamps < 1 or step_timestamps < 1:
        raise EvaluationError("walk-forward sizes must be positive")
    groups = _sorted_groups(_records(records), date_key)
    folds: list[WalkForwardFold] = []
    train_end = initial_train_timestamps
    fold = 0
    while train_end + test_timestamps <= len(groups):
        train = tuple(row for _, group in groups[:train_end] for row in group)
        test = tuple(
            row
            for _, group in groups[train_end : train_end + test_timestamps]
            for row in group
        )
        folds.append(WalkForwardFold(fold=fold, train=train, test=test))
        fold += 1
        train_end += step_timestamps
    if not folds:
        raise EvaluationError("History is too short for the requested walk-forward split")
    return tuple(folds)


def _numeric(values: Iterable[Any], field: str, *, nonnegative: bool = False) -> list[float]:
    output: list[float] = []
    for value in values:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise EvaluationError(f"{field} contains a non-numeric value") from exc
        if not math.isfinite(parsed) or (nonnegative and parsed < 0):
            raise EvaluationError(f"{field} contains an invalid value")
        output.append(parsed)
    if not output:
        raise EvaluationError(f"{field} is empty")
    return output


def _same_length(*arrays: Sequence[Any]) -> None:
    lengths = {len(array) for array in arrays}
    if len(lengths) != 1:
        raise EvaluationError("Metric inputs have different lengths")


def _poisson_deviance(actual: float, predicted: float) -> float:
    predicted = max(predicted, 1e-12)
    if actual == 0:
        return 2.0 * predicted
    return 2.0 * (actual * math.log(actual / predicted) - (actual - predicted))


def evaluate_counts(actual: Iterable[Any], predicted: Iterable[Any]) -> dict[str, float]:
    observed = _numeric(actual, "actual", nonnegative=True)
    estimates = _numeric(predicted, "predicted", nonnegative=True)
    _same_length(observed, estimates)
    errors = [
        estimate - value for value, estimate in zip(observed, estimates, strict=True)
    ]
    return {
        "mae": sum(abs(error) for error in errors) / len(errors),
        "rmse": math.sqrt(sum(error * error for error in errors) / len(errors)),
        "mean_poisson_deviance": sum(
            _poisson_deviance(value, estimate)
            for value, estimate in zip(observed, estimates, strict=True)
        )
        / len(observed),
        "mean_error": sum(errors) / len(errors),
        "n": float(len(observed)),
    }


def evaluate_goal_counts(
    actual_home: Iterable[Any],
    actual_away: Iterable[Any],
    predicted_home: Iterable[Any],
    predicted_away: Iterable[Any],
) -> dict[str, Any]:
    ah = list(actual_home)
    aa = list(actual_away)
    ph = list(predicted_home)
    pa = list(predicted_away)
    _same_length(ah, aa, ph, pa)
    return {
        "home": evaluate_counts(ah, ph),
        "away": evaluate_counts(aa, pa),
        "combined": evaluate_counts(ah + aa, ph + pa),
    }


def _outcome(home: float, away: float) -> int:
    return 0 if home > away else 1 if home == away else 2


def _probability_triplet(value: Any) -> tuple[float, float, float]:
    if hasattr(value, "match_result"):
        value = value.match_result
    if not isinstance(value, Mapping):
        raise EvaluationError("Each result prediction must be a mapping")
    try:
        probabilities = (
            float(value["home_win"]),
            float(value["draw"]),
            float(value["away_win"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EvaluationError("Result probabilities are incomplete") from exc
    if any(not math.isfinite(item) or item < 0 or item > 1 for item in probabilities):
        raise EvaluationError("Result probability is outside [0, 1]")
    if not math.isclose(sum(probabilities), 1.0, abs_tol=1e-8):
        raise EvaluationError("Result probabilities do not sum to one")
    return probabilities


def evaluate_match_results(
    actual_home: Iterable[Any],
    actual_away: Iterable[Any],
    predictions: Iterable[Any],
) -> dict[str, float]:
    home = _numeric(actual_home, "actual_home", nonnegative=True)
    away = _numeric(actual_away, "actual_away", nonnegative=True)
    probabilities = [_probability_triplet(value) for value in predictions]
    _same_length(home, away, probabilities)
    correct = 0
    log_loss = 0.0
    brier = 0.0
    ranked = 0.0
    for hg, ag, predicted in zip(home, away, probabilities, strict=True):
        actual = _outcome(hg, ag)
        correct += int(max(range(3), key=lambda index: predicted[index]) == actual)
        log_loss -= math.log(max(predicted[actual], 1e-15))
        brier += sum(
            (probability - float(index == actual)) ** 2
            for index, probability in enumerate(predicted)
        )
        actual_cumulative = (float(actual == 0), float(actual <= 1))
        predicted_cumulative = (predicted[0], predicted[0] + predicted[1])
        ranked += sum(
            (forecast - observed) ** 2
            for forecast, observed in zip(
                predicted_cumulative, actual_cumulative, strict=True
            )
        ) / 2.0
    size = len(home)
    return {
        "accuracy": correct / size,
        "log_loss": log_loss / size,
        "brier_score": brier / size,
        "ranked_probability_score": ranked / size,
        "n": float(size),
    }


def expected_calibration_error(
    actual: Iterable[Any], predicted: Iterable[Any], *, bins: int = 10
) -> float:
    if bins < 2 or bins > 100:
        raise EvaluationError("bins must be between 2 and 100")
    labels = _numeric(actual, "actual")
    probabilities = _numeric(predicted, "predicted")
    _same_length(labels, probabilities)
    if any(label not in (0.0, 1.0) for label in labels):
        raise EvaluationError("Calibration labels must be binary")
    if any(probability < 0 or probability > 1 for probability in probabilities):
        raise EvaluationError("Calibration probability is outside [0, 1]")
    total = len(labels)
    error = 0.0
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        members = [
            position
            for position, probability in enumerate(probabilities)
            if lower <= probability < upper or (index == bins - 1 and probability == 1)
        ]
        if not members:
            continue
        confidence = sum(probabilities[position] for position in members) / len(members)
        frequency = sum(labels[position] for position in members) / len(members)
        error += len(members) / total * abs(confidence - frequency)
    return error


__all__ = [
    "EvaluationError",
    "TemporalSplit",
    "WalkForwardFold",
    "assert_temporal_order",
    "chronological_split",
    "evaluate_counts",
    "evaluate_goal_counts",
    "evaluate_match_results",
    "expected_calibration_error",
    "walk_forward_splits",
]
