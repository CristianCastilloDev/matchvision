"""
Import upcoming Liga MX fixtures from ESPN scraper output.
Maps ESPN team names to our DB team names.

Usage: python scripts/scrape_fixtures.py | python scripts/import_upcoming.py
   Or: python scripts/import_upcoming.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import models
from app.db import SessionLocal
from sqlalchemy import select

ESPN_TO_DB = {
    "América": "Club America",
    "Atlante": "Atlante",
    "Atlas": "Atlas",
    "Atlético de San Luis": "Atl. San Luis",
    "Cruz Azul": "Cruz Azul",
    "FC Juarez": "Juarez",
    "Guadalajara": "Guadalajara Chivas",
    "León": "Club Leon",
    "Monterrey": "Monterrey",
    "Necaxa": "Necaxa",
    "Pachuca": "Pachuca",
    "Puebla": "Puebla",
    "Pumas UNAM": "UNAM Pumas",
    "Querétaro": "Queretaro",
    "Santos": "Santos Laguna",
    "Tigres UANL": "Tigres UANL",
    "Tijuana": "Club Tijuana",
    "Toluca": "Toluca",
    "Mazatlán": "Mazatlan FC",
}

def load_fixtures(path="data/upcoming_fixtures.json"):
    p = Path(__file__).resolve().parent.parent / path
    if not p.exists():
        print(f"File not found: {p}")
        return []
    with open(p) as f:
        return json.load(f)

def resolve_team(db, name):
    db_name = ESPN_TO_DB.get(name, name)
    team = db.scalar(select(models.Team).where(models.Team.name.ilike(db_name)))
    if team:
        return team
    # Try alias
    team = db.scalar(
        select(models.Team).join(models.TeamAlias).where(models.TeamAlias.alias.ilike(db_name))
    )
    if team:
        return team
    print(f"  ⚠️  Team not found: '{name}' (mapped to '{db_name}')")
    return None

def resolve_competition(db):
    comp = db.scalar(select(models.Competition).where(models.Competition.name.ilike("Liga MX")))
    if comp:
        return comp
    comp = db.scalar(select(models.Competition).where(models.Competition.id == 3))
    return comp

def resolve_season(db, comp, date):
    yr = date.year
    name = f"{yr}/{yr+1}"
    season = db.scalar(
        select(models.Season).where(
            models.Season.competition_id == comp.id,
            models.Season.name.ilike(f"%{yr}%")
        )
    )
    if season:
        return season
    season = models.Season(competition_id=comp.id, name=name, is_mock_data=False)
    db.add(season)
    db.flush()
    return season

def import_fixtures(fixtures):
    db = SessionLocal()
    comp = resolve_competition(db)
    if not comp:
        print("❌ Competition 'Liga MX' not found")
        return

    imported = 0
    skipped = 0
    errors = 0

    for f in fixtures:
        home_team = resolve_team(db, f["home"])
        away_team = resolve_team(db, f["away"])
        if not home_team or not away_team:
            errors += 1
            continue

        try:
            match_date = datetime.fromisoformat(f["date"].replace("Z", "+00:00"))
        except (ValueError, TypeError):
            errors += 1
            continue

        season = resolve_season(db, comp, match_date)

        # Check if match already exists
        existing = db.scalar(
            select(models.Match).where(
                models.Match.home_team_id == home_team.id,
                models.Match.away_team_id == away_team.id,
                models.Match.match_date == match_date,
            )
        )
        if existing:
            skipped += 1
            continue

        status = f.get("status", "scheduled")
        if status == "finished":
            status = "finished"
            home_score = f.get("home_score")
            away_score = f.get("away_score")
        else:
            status = "scheduled"
            home_score = None
            away_score = None

        match = models.Match(
            competition_id=comp.id,
            season_id=season.id,
            home_team_id=home_team.id,
            away_team_id=away_team.id,
            match_date=match_date,
            status=status,
            home_score=home_score,
            away_score=away_score,
            data_source="espn-api",
            source_updated_at=datetime.utcnow(),
            is_mock_data=False,
        )
        db.add(match)
        imported += 1

    db.commit()
    db.close()
    print(f"\n✅ Importados: {imported}  Omitidos: {skipped}  Errores: {errors}")

if __name__ == "__main__":
    fixtures = load_fixtures()
    if not fixtures:
        # Try reading from stdin
        try:
            fixtures = json.loads(sys.stdin.read())
        except (json.JSONDecodeError, EOFError):
            print("No fixtures found. Run scripts/scrape_fixtures.py first")
            sys.exit(1)
    print(f"Cargados {len(fixtures)} fixtures desde ESPN")
    import_fixtures(fixtures)
