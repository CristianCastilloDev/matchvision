from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app import models
from app.db import SessionLocal
from app.services.predictions import (
    InsufficientPredictionDataError,
    evaluate_prediction,
    generate_prediction,
)
from app.schemas import PredictionRequest


BATCH_SIZE = 50


def precompute(db: Session) -> dict[str, int]:
    total = 0
    generated = 0
    evaluated = 0
    skipped = 0
    errors = 0
    first_skipped_id = None

    while True:
        matches = list(
            db.scalars(
                select(models.Match)
                .where(
                    models.Match.status == "finished",
                    models.Match.home_score.is_not(None),
                    models.Match.away_score.is_not(None),
                    models.Match.home_team_id.is_not(None),
                    models.Match.away_team_id.is_not(None),
                )
                .order_by(models.Match.match_date.desc())
                .limit(BATCH_SIZE)
                .offset(total)
            ).all()
        )
        if not matches:
            break
        total += len(matches)

        existing_prediction_match_ids = {
            row[0]
            for row in db.execute(
                select(models.Prediction.match_id).where(
                    models.Prediction.match_id.in_([m.id for m in matches])
                )
            ).all()
        }

        for match in matches:
            if match.id in existing_prediction_match_ids:
                skipped += 1
                if first_skipped_id is None:
                    first_skipped_id = match.id
                continue

            try:
                response = generate_prediction(
                    db, PredictionRequest(match_id=match.id)
                )
                generated += 1
                print(
                    f"  [{match.id}] {match.home_team.name} vs {match.away_team.name} "
                    f"({match.match_date.strftime('%Y-%m-%d')}) "
                    f"→ home={response.match_result.home_win:.1%} "
                    f"draw={response.match_result.draw:.1%} "
                    f"away={response.match_result.away_win:.1%}"
                )

                prediction = db.get(models.Prediction, response.prediction_id)
                if prediction:
                    try:
                        outcomes = evaluate_prediction(
                            db, prediction,
                            home_score=match.home_score,
                            away_score=match.away_score,
                        )
                        evaluated += 1
                        status = "correct" if all(
                            o.status == "correct" for o in outcomes
                        ) else "incorrect"
                        print(f"    → {status}")
                    except ValueError as e:
                        print(f"    → evaluation skipped: {e}")

            except InsufficientPredictionDataError as e:
                skipped += 1
                print(
                    f"  [{match.id}] SKIP {match.home_team.name} vs {match.away_team.name}: {e}"
                )
            except Exception as e:
                errors += 1
                print(
                    f"  [{match.id}] ERROR {match.home_team.name} vs {match.away_team.name}: {e}"
                )

    return {
        "total": total,
        "generated": generated,
        "evaluated": evaluated,
        "skipped": skipped,
        "errors": errors,
    }


if __name__ == "__main__":
    db = SessionLocal()
    try:
        print(f"[{datetime.now(UTC).isoformat()}] Comenzando precómputo de predicciones...")
        stats = precompute(db)
        print(f"\nResumen:")
        print(f"  Partidos procesados: {stats['total']}")
        print(f"  Predicciones generadas: {stats['generated']}")
        print(f"  Evaluaciones realizadas: {stats['evaluated']}")
        print(f"  Omitidos (ya existentes / sin historial): {stats['skipped']}")
        print(f"  Errores: {stats['errors']}")
        print(f"[{datetime.now(UTC).isoformat()}] Completado.")
    finally:
        db.close()
