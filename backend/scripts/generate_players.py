"""
Generate synthetic player and lineup data for Liga MX teams.
Creates 25 players per team with realistic Mexican names/positions,
then assigns lineups to the most recent matches.
"""

import sys
import random
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app import models

random.seed(42)
DB_PATH = Path(__file__).resolve().parent.parent / "matchvision.db"
DATA_SOURCE = "synthetic"

FIRST_NAMES_MALE = [
    "Juan", "Pedro", "Carlos", "Miguel", "Jose", "Luis", "Jesus", "Manuel",
    "Alejandro", "Antonio", "Francisco", "Jorge", "Rafael", "Eduardo", "Fernando",
    "Hector", "Sergio", "Pablo", "Diego", "David", "Adrian", "Oscar", "Victor",
    "Raul", "Alberto", "Marco", "Ivan", "Andres", "Daniel", "Ricardo",
    "Alan", "Cesar", "Rodrigo", "Gerardo", "Mauricio", "Enrique", "Arturo",
    "Guillermo", "Ruben", "Hugo", "Omar", "Julio", "Joel", "Erick",
    "Brandon", "Kevin", "Bryan", "Edgar", "Saul", "Uriel",
]

LAST_NAMES = [
    "Garcia", "Martinez", "Lopez", "Hernandez", "Gonzalez", "Perez", "Rodriguez",
    "Sanchez", "Ramirez", "Cruz", "Flores", "Morales", "Ortiz", "Jimenez",
    "Torres", "Diaz", "Reyes", "Vazquez", "Gutierrez", "Mendoza", "Aguilar",
    "Rojas", "Guerrero", "Medina", "Moreno", "Castillo", "Romero", "Alvarez",
    "Chavez", "Rivera", "Ramos", "Herrera", "Molina", "Munoz", "Ortega",
    "Castro", "Delgado", "Pena", "Contreras", "Nava", "Ayala", "Salazar",
    "Soto", "Campos", "Lara", "Marquez", "Pacheco", "Trujillo", "Estrada",
    "Valencia",
]

POSITIONS = ["GK", "DEF", "DEF", "DEF", "DEF", "MID", "MID", "MID", "MID", "FWD", "FWD"]
# GK, 4 DEF, 4 MID, 2 FWD per team (25 players = 3 GK, 8 DEF, 8 MID, 6 FWD)

def generate_player_name() -> str:
    return f"{random.choice(FIRST_NAMES_MALE)} {random.choice(LAST_NAMES)}"

def generate_birth_date(age_years: int = 25) -> str:
    year = 2026 - age_years - random.randint(0, 5)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}"

def generate_squad() -> list[dict]:
    squad = []
    # 3 goalkeepers
    for _ in range(3):
        name = generate_player_name()
        squad.append({
            "name": name,
            "position": "GK",
            "birth_date": generate_birth_date(random.randint(25, 35)),
            "nationality": "Mexico",
        })
    # 8 defenders
    for _ in range(8):
        name = generate_player_name()
        squad.append({
            "name": name,
            "position": "DEF",
            "birth_date": generate_birth_date(random.randint(22, 32)),
            "nationality": "Mexico" if random.random() < 0.85 else random.choice(["Argentina", "Colombia", "Uruguay", "Chile", "USA"]),
        })
    # 8 midfielders
    for _ in range(8):
        squad.append({
            "name": generate_player_name(),
            "position": "MID",
            "birth_date": generate_birth_date(random.randint(21, 31)),
            "nationality": "Mexico" if random.random() < 0.8 else random.choice(["Argentina", "Colombia", "Brazil", "Chile", "Ecuador"]),
        })
    # 6 forwards
    for _ in range(6):
        squad.append({
            "name": generate_player_name(),
            "position": "FWD",
            "birth_date": generate_birth_date(random.randint(20, 33)),
            "nationality": "Mexico" if random.random() < 0.75 else random.choice(["Argentina", "Colombia", "Brazil", "Uruguay", "Paraguay"]),
        })
    return squad

def main():
    engine = create_engine(f"sqlite:///{DB_PATH}")
    models.Base.metadata.create_all(engine)

    with Session(engine) as db:
        # Get Liga MX competition
        from app.services.entity_resolution import normalize_entity_name
        comp = db.scalar(
            select(models.Competition).where(
                models.Competition.name == "Liga MX",
                models.Competition.data_source == "football-data",
            )
        )
        if not comp:
            print("ERROR: Liga MX competition not found. Run import_mexico.py first.")
            return

        teams = db.scalars(
            select(models.Team).where(models.Team.data_source == "football-data")
            .order_by(models.Team.name)
        ).all()
        print(f"Found {len(teams)} teams")

        existing_count = db.scalar(select(func.count(models.Player.id)))
        if existing_count and existing_count > 0:
            print(f"Players already exist ({existing_count}), skipping.")
            return

        total_players = 0
        total_lineups = 0

        # Get finished matches ordered by date descending for lineup generation
        recent_matches = db.scalars(
            select(models.Match)
            .where(
                models.Match.competition_id == comp.id,
                models.Match.status == "finished",
                models.Match.home_score.is_not(None),
                models.Match.away_score.is_not(None),
            )
            .order_by(models.Match.match_date.desc())
            .limit(100)
        ).all()
        match_ids = {m.id for m in recent_matches}
        print(f"Recent matches for lineups: {len(match_ids)}")

        for team in teams:
            print(f"  {team.name}: ", end="", flush=True)
            squad = generate_squad()
            created_players = []
            shirt_nums = random.sample(range(1, 31), len(squad))

            for i, pdata in enumerate(squad):
                player = models.Player(
                    external_id=f"syn-{team.id}-{i}",
                    current_team_id=team.id,
                    name=pdata["name"],
                    birth_date=datetime.strptime(pdata["birth_date"], "%Y-%m-%d").date(),
                    nationality=pdata["nationality"],
                    primary_position=pdata["position"],
                    active=True,
                    data_source=DATA_SOURCE,
                    source_updated_at=datetime.now(),
                    is_mock_data=True,
                )
                db.add(player)
                db.flush()

                norm = f"{team.id}-{i}-{pdata['name']}"
                alias = models.PlayerAlias(
                    player_id=player.id,
                    provider=f"{DATA_SOURCE}-{team.id}",
                    alias=pdata["name"],
                    normalized_alias=normalize_entity_name(norm),
                )
                db.add(alias)
                created_players.append(player)
                total_players += 1

            # Create lineups for recent matches involving this team
            team_match_ids = db.scalars(
                select(models.Match.id)
                .where(
                    models.Match.id.in_(match_ids),
                    (models.Match.home_team_id == team.id) | (models.Match.away_team_id == team.id),
                )
                .limit(10)
            ).all()

            for match_id in team_match_ids:
                match = db.get(models.Match, match_id)
                if not match:
                    continue
                team_id_in_match = team.id
                # Select 11 starters + 7 subs
                lineup_players = random.sample(created_players, min(18, len(created_players)))
                for lp in lineup_players:
                    is_started = lineup_players.index(lp) < 11
                    lineup = models.Lineup(
                        match_id=match.id,
                        team_id=team_id_in_match,
                        player_id=lp.id,
                        started=is_started,
                        confirmed=True,
                        position=lp.primary_position,
                        shirt_number=shirt_nums[created_players.index(lp)],
                        expected_minutes=90.0 if is_started else 30.0,
                        data_source=DATA_SOURCE,
                        source_updated_at=datetime.now(),
                        is_mock_data=True,
                    )
                    db.add(lineup)
                    total_lineups += 1

            print(f"{len(squad)} players, {len(team_match_ids)} matches with lineups")

        db.commit()
        print(f"\nDone: {total_players} players, {total_lineups} lineups created.")

if __name__ == "__main__":
    main()
