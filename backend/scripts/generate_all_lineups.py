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
BATCH = 2000


def main():
    db = SessionLocal()
    try:
        existing = db.scalar(select(func.count(models.Lineup.id)))
        if existing and existing > 0:
            print(f"Lineups already exist ({existing}), skipping.")
            return

        players = db.scalars(
            select(models.Player)
            .where(models.Player.data_source == "synthetic")
        ).all()
        print(f"Players: {len(players)}")

        team_players: dict[int, list[models.Player]] = {}
        for p in players:
            if p.current_team_id:
                team_players.setdefault(p.current_team_id, []).append(p)
        print(f"Teams with players: {len(team_players)}")

        matches = db.scalars(
            select(models.Match)
            .where(
                models.Match.competition_id == 3,
                models.Match.status == "finished",
                models.Match.home_score.is_not(None),
                models.Match.away_score.is_not(None),
                models.Match.home_team_id.is_not(None),
                models.Match.away_team_id.is_not(None),
            )
            .order_by(models.Match.match_date)
        ).all()
        print(f"Matches: {len(matches)}")

        total = 0
        batch = []
        for match in matches:
            for team_id in (match.home_team_id, match.away_team_id):
                squad = team_players.get(team_id)
                if not squad or len(squad) < 11:
                    continue

                selected = random.sample(squad, min(18, len(squad)))
                shirt_nums = random.sample(range(1, 31), len(selected))

                for idx, player in enumerate(selected):
                    is_started = idx < 11
                    lineup = models.Lineup(
                        match_id=match.id,
                        team_id=team_id,
                        player_id=player.id,
                        started=is_started,
                        confirmed=True,
                        position=player.primary_position,
                        shirt_number=shirt_nums[idx],
                        expected_minutes=90.0 if is_started else 30.0,
                        data_source="synthetic",
                        source_updated_at=match.match_date,
                        is_mock_data=match.is_mock_data,
                    )
                    batch.append(lineup)
                    total += 1

                    if len(batch) >= BATCH:
                        db.add_all(batch)
                        db.flush()
                        batch = []
                        print(f"  {total} lineups...")

        if batch:
            db.add_all(batch)

        db.commit()
        print(f"Done: {total} lineups generated.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
