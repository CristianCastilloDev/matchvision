from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import models, schemas
from app.db import get_db
from app.services.predictions import (
    evaluate_prediction,
    generate_prediction,
    response_from_prediction,
)


router = APIRouter(prefix="/predictions", tags=["predictions"])


@router.post("/match", response_model=schemas.PredictionResponse)
def predict_match(payload: schemas.PredictionRequest, db: Session = Depends(get_db)):
    try:
        response = generate_prediction(db, payload)
        match = db.get(models.Match, payload.match_id)
        if match and match.status == "finished" and match.home_score is not None and match.away_score is not None:
            prediction = db.get(models.Prediction, response.prediction_id)
            if prediction:
                try:
                    outcomes = evaluate_prediction(
                        db, prediction,
                        home_score=match.home_score,
                        away_score=match.away_score,
                    )
                    response.outcomes = [
                        schemas.PredictionOutcomeOut(
                            event_type=o.event_type,
                            predicted_probability=o.predicted_probability,
                            predicted_value=o.predicted_value,
                            actual_value=o.actual_value,
                            status=o.status,
                            evaluated_at=o.evaluated_at,
                        )
                        for o in outcomes
                    ]
                except ValueError:
                    pass
        return response
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"No se pudo generar la predicción: {exc}") from exc


@router.get("/history", response_model=list[schemas.PredictionSummary])
def prediction_history(
    match_id: int | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    stmt = (
        select(models.Prediction)
        .options(selectinload(models.Prediction.outcomes))
        .order_by(models.Prediction.generated_at.desc())
    )
    if match_id is not None:
        stmt = stmt.where(models.Prediction.match_id == match_id)
    predictions = list(db.scalars(stmt.offset(offset).limit(limit)).all())
    response: list[schemas.PredictionSummary] = []
    for item in predictions:
        evaluation_status = item.status
        if item.outcomes:
            evaluation_status = (
                "incorrect"
                if any(outcome.status == "incorrect" for outcome in item.outcomes)
                else "correct"
            )
        response.append(
            schemas.PredictionSummary(
                id=item.id,
                match_id=item.match_id,
                model_name=item.model_name,
                model_version=item.model_version,
                confidence=item.confidence,
                quality_score=item.quality_score,
                generated_at=item.generated_at,
                status=evaluation_status,
                is_mock_data=item.is_mock_data,
            )
        )
    return response


@router.get("/match/{match_id}", response_model=list[schemas.PredictionResponse])
def predictions_for_match(match_id: int, db: Session = Depends(get_db)):
    predictions = list(
        db.scalars(
            select(models.Prediction)
            .where(models.Prediction.match_id == match_id)
            .order_by(models.Prediction.generated_at.desc())
        ).all()
    )
    return [response_from_prediction(prediction) for prediction in predictions]


@router.get("/{prediction_id}", response_model=schemas.PredictionResponse)
def get_prediction(prediction_id: str, db: Session = Depends(get_db)):
    prediction = db.get(models.Prediction, prediction_id)
    if prediction is None:
        raise HTTPException(status_code=404, detail="Predicción no encontrada")
    return response_from_prediction(prediction)


@router.post("/{prediction_id}/evaluate")
def evaluate_saved_prediction(
    prediction_id: str,
    payload: schemas.PredictionEvaluationRequest,
    db: Session = Depends(get_db),
):
    prediction = db.get(models.Prediction, prediction_id)
    if prediction is None:
        raise HTTPException(status_code=404, detail="Predicción no encontrada")
    match = db.get(models.Match, prediction.match_id)
    if match is None or match.status != "finished":
        raise HTTPException(
            status_code=409,
            detail="Registra primero el resultado real en /matches/{match_id}/result",
        )
    if match.home_score != payload.home_score or match.away_score != payload.away_score:
        raise HTTPException(
            status_code=409,
            detail="El resultado enviado no coincide con el resultado real registrado",
        )
    try:
        outcomes = evaluate_prediction(
            db,
            prediction,
            home_score=payload.home_score,
            away_score=payload.away_score,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "prediction_id": prediction.id,
        "prediction_immutable": True,
        "outcomes": [
            {
                "event_type": outcome.event_type,
                "predicted_probability": outcome.predicted_probability,
                "predicted_value": outcome.predicted_value,
                "actual_value": outcome.actual_value,
                "status": outcome.status,
                "evaluated_at": outcome.evaluated_at,
            }
            for outcome in outcomes
        ],
    }
