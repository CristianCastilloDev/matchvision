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
BATCH = 5000


def main():
    db = SessionLocal()
    try:
        existing = db.scalar(select(func.count(models.PlayerMatch.id)))
        if existing and existing > 0:
            print(f"PlayerMatch already exists ({existing}), skipping.")
            return

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
        print(f"Generating player stats for {len(matches)} matches...")

        total = 0
        batch = []
        for match in matches:
            for team_id, is_home in [(match.home_team_id, True), (match.away_team_id, False)]:
                team_goals = match.home_score if is_home else match.away_score
                lineups = db.scalars(
                    select(models.Lineup).where(
                        models.Lineup.match_id == match.id,
                        models.Lineup.team_id == team_id,
                    )
                ).all()
                if not lineups:
                    continue

                starters = [l for l in lineups if l.started]
                subs = [l for l in lineups if not l.started]

                minutes_map = {}
                for l in starters:
                    minutes_map[l.player_id] = random.randint(60, 95)
                for l in subs:
                    minutes_map[l.player_id] = random.randint(10, 50)

                fwd_ids = [l.player_id for l in starters if l.position == "FWD"]
                goal_scorers = []
                remaining = team_goals or 0
                if fwd_ids and remaining > 0:
                    for _ in range(remaining):
                        goal_scorers.append(random.choice(fwd_ids))

                for l in lineups:
                    minutes = minutes_map.get(l.player_id, 0)
                    goals = goal_scorers.count(l.player_id)
                    yc = random.randint(0, 2) if l.position in ("DEF", "MID") else 0
                    rc = 1 if random.random() < 0.02 else 0
                    pm = models.PlayerMatch(
                        match_id=match.id,
                        player_id=l.player_id,
                        team_id=l.team_id,
                        started=l.started,
                        minutes_played=minutes,
                        position=l.position,
                        goals=goals,
                        assists=random.randint(0, 2),
                        shots=random.randint(0, 5),
                        shots_on_target=random.randint(0, 3),
                        xg=round(random.uniform(0.0, 1.5), 2),
                        yellow_cards=yc,
                        red_cards=rc,
                        fouls=random.randint(0, 4),
                        data_source="synthetic",
                        source_updated_at=match.match_date,
                        is_mock_data=match.is_mock_data,
                    )
                    batch.append(pm)
                    total += 1

                    if len(batch) >= BATCH:
                        db.add_all(batch)
                        db.flush()
                        batch = []
                        print(f"  {total} records...")

        if batch:
            db.add_all(batch)

        db.commit()
        print(f"Done: {total} player_match records generated.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
