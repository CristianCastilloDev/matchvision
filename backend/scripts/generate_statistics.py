from __future__ import annotations

import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.db import SessionLocal

random.seed(42)
DATA_SOURCE = "synthetic"


def generate_statistics_for_match(
    match: models.Match, home_players: int, away_players: int
) -> list[models.TeamMatchStatistics]:
    home_poss = random.randint(38, 62)
    away_poss = 100 - home_poss
    return [
        models.TeamMatchStatistics(
            match_id=match.id,
            team_id=match.home_team_id,
            possession=float(home_poss),
            shots=random.randint(3, 18),
            shots_on_target=random.randint(1, 8),
            corners=random.randint(1, 10),
            fouls=random.randint(5, 18),
            yellow_cards=random.randint(0, 5),
            red_cards=random.randint(0, 1),
            xg=round(random.uniform(0.2, 3.0), 2),
            passes=random.randint(200, 550),
            source_updated_at=match.match_date,
            is_mock_data=match.is_mock_data,
        ),
        models.TeamMatchStatistics(
            match_id=match.id,
            team_id=match.away_team_id,
            possession=float(away_poss),
            shots=random.randint(3, 18),
            shots_on_target=random.randint(1, 8),
            corners=random.randint(1, 10),
            fouls=random.randint(5, 18),
            yellow_cards=random.randint(0, 5),
            red_cards=random.randint(0, 1),
            xg=round(random.uniform(0.2, 3.0), 2),
            passes=random.randint(200, 550),
            source_updated_at=match.match_date,
            is_mock_data=match.is_mock_data,
        ),
    ]


def main():
    db = SessionLocal()
    try:
        existing = db.scalar(select(func.count(models.TeamMatchStatistics.id)))
        if existing and existing > 0:
            print(f"TeamMatchStatistics already exist ({existing}), truncating...")
            db.execute(models.TeamMatchStatistics.__table__.delete())
            db.flush()

        matches = db.scalars(
            select(models.Match)
            .where(
                models.Match.competition_id == 3,
                models.Match.status == "finished",
                models.Match.home_score.is_not(None),
                models.Match.away_score.is_not(None),
            )
            .order_by(models.Match.match_date)
        ).all()
        print(f"Generating statistics for {len(matches)} matches...")

        total = 0
        batch = []
        for match in matches:
            home_cnt = db.scalar(
                select(func.count(models.Lineup.id)).where(
                    models.Lineup.match_id == match.id,
                    models.Lineup.team_id == match.home_team_id,
                )
            )
            away_cnt = db.scalar(
                select(func.count(models.Lineup.id)).where(
                    models.Lineup.match_id == match.id,
                    models.Lineup.team_id == match.away_team_id,
                )
            )
            stats = generate_statistics_for_match(match, home_cnt or 0, away_cnt or 0)
            batch.extend(stats)
            total += 2
            if total % 500 == 0:
                db.add_all(batch)
                db.flush()
                batch = []
                print(f"  {total} records...")

        if batch:
            db.add_all(batch)

        db.commit()
        print(f"Done: {total} statistics records generated.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
