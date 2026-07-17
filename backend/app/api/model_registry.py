from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.orm import Session, selectinload

from app import models, schemas
from app.db import get_db


router = APIRouter(prefix="/models", tags=["models"])


def _versions(db: Session, model_name: str) -> list[models.ModelVersion]:
    return list(
        db.scalars(
            select(models.ModelVersion)
            .options(selectinload(models.ModelVersion.metrics))
            .where(models.ModelVersion.name == model_name)
            .order_by(models.ModelVersion.trained_at.desc())
        ).all()
    )


@router.get("/stats")
def model_stats(db: Session = Depends(get_db)):
    # Get the latest prediction for each match
    latest_preds = {}
    all_preds = db.scalars(
        select(models.Prediction).order_by(models.Prediction.generated_at.desc())
    ).all()
    for p in all_preds:
        if p.match_id not in latest_preds:
            latest_preds[p.match_id] = p
    latest_pred_ids = [p.id for p in latest_preds.values()]

    # Match result accuracy
    result_outs = list(db.scalars(
        select(models.PredictionOutcome)
        .where(
            models.PredictionOutcome.prediction_id.in_(latest_pred_ids),
            models.PredictionOutcome.event_type == "match_result",
        )
    ).all())
    result_correct = sum(1 for o in result_outs if o.status == "correct")
    result_incorrect = sum(1 for o in result_outs if o.status == "incorrect")
    result_total = result_correct + result_incorrect

    # Parlay accuracy (non match_result outcomes)
    parlay_outs = list(db.scalars(
        select(models.PredictionOutcome)
        .where(
            models.PredictionOutcome.prediction_id.in_(latest_pred_ids),
            models.PredictionOutcome.event_type != "match_result",
            models.PredictionOutcome.event_type != "top_3_scorer_any",
        )
    ).all())
    parlay_correct = sum(1 for o in parlay_outs if o.status == "correct")
    parlay_incorrect = sum(1 for o in parlay_outs if o.status == "incorrect")

    # Scorer accuracy
    scorer_outs = list(db.scalars(
        select(models.PredictionOutcome)
        .where(
            models.PredictionOutcome.prediction_id.in_(latest_pred_ids),
            models.PredictionOutcome.event_type == "top_3_scorer_any",
        )
    ).all())
    scorer_correct = sum(1 for o in scorer_outs if o.status == "correct")
    scorer_incorrect = sum(1 for o in scorer_outs if o.status == "incorrect")

    # Per-category breakdown
    categories = {}
    for prefix, label in [("goals", "Goles"), ("cards", "Tarjetas"), ("corners", "Córners")]:
        outs = [o for o in parlay_outs if o.event_type.startswith(prefix)]
        c = sum(1 for o in outs if o.status == "correct")
        i = sum(1 for o in outs if o.status == "incorrect")
        if c + i > 0:
            categories[label] = {"correct": c, "incorrect": i, "total": c + i}

    # Recent graded predictions (latest 5)
    recent_preds = list(db.scalars(
        select(models.Prediction)
        .where(models.Prediction.id.in_(latest_pred_ids), models.Prediction.status.in_(["correct", "incorrect"]))
        .order_by(models.Prediction.generated_at.desc())
        .limit(10)
    ).all())
    recent = []
    for p in recent_preds:
        match = db.get(models.Match, p.match_id)
        if not match:
            continue
        outcome = next((o for o in result_outs if o.prediction_id == p.id), None)
        score = f"{match.home_score}-{match.away_score}" if match.home_score is not None and match.away_score is not None else None
        recent.append({
            "match_id": match.id,
            "home": match.home_team.name if match.home_team else "?",
            "away": match.away_team.name if match.away_team else "?",
            "score": score,
            "status": outcome.status if outcome else p.status,
            "date": p.generated_at.isoformat(),
        })

    # Calculate Brier score from match_result probabilities
    brier_scores = []
    for o in result_outs:
        if o.predicted_probability is not None:
            # For match_result, Brier = (predicted_prob - actual)^2
            # actual is 1.0 if correct, 0.0 if incorrect
            actual = 1.0 if o.status == "correct" else 0.0
            brier_scores.append((o.predicted_probability - actual) ** 2)
    brier = sum(brier_scores) / len(brier_scores) if brier_scores else None

    return {
        "result": {
            "correct": result_correct,
            "incorrect": result_incorrect,
            "total": result_total,
            "accuracy": round(result_correct / result_total * 100, 1) if result_total else 0,
        },
        "parlay": {
            "correct": parlay_correct,
            "incorrect": parlay_incorrect,
            "total": parlay_correct + parlay_incorrect,
            "categories": categories,
        },
        "scorer": {
            "correct": scorer_correct,
            "incorrect": scorer_incorrect,
            "total": scorer_correct + scorer_incorrect,
        },
        "brier": round(brier, 4) if brier is not None else None,
        "recent": recent,
    }


@router.get("", response_model=list[schemas.ModelVersionOut])
def list_models(db: Session = Depends(get_db)):
    return list(
        db.scalars(
            select(models.ModelVersion).order_by(
                models.ModelVersion.name, models.ModelVersion.trained_at.desc()
            )
        ).all()
    )


@router.get("/{model_name}/metrics", response_model=list[schemas.ModelMetricOut])
def model_metrics(model_name: str, db: Session = Depends(get_db)):
    versions = _versions(db, model_name)
    if not versions:
        raise HTTPException(status_code=404, detail="Modelo no encontrado")
    return versions[0].metrics


@router.get("/{model_name}/calibration")
def model_calibration(model_name: str, db: Session = Depends(get_db)):
    versions = _versions(db, model_name)
    if not versions:
        raise HTTPException(status_code=404, detail="Modelo no encontrado")
    metrics = versions[0].metrics
    calibration = [
        {
            "metric_name": metric.metric_name,
            "value": metric.value,
            "bins": metric.calibration_bins,
            "split": metric.split,
            "scope": metric.scope,
        }
        for metric in metrics
        if metric.calibration_bins
        or metric.metric_name.casefold() in {"brier_score", "log_loss", "expected_calibration_error"}
    ]
    return {
        "model_name": model_name,
        "model_version": versions[0].version,
        "available": bool(calibration),
        "calibration": calibration,
        "warning": None
        if calibration
        else "No hay evaluación de calibración registrada; no se inventan métricas.",
    }
