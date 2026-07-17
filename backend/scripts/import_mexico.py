"""
Import Liga MX data from football-data.co.uk MEX.csv.
Reads the CSV, creates competition/teams/seasons/matches in the local DB.
"""

import csv
import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import models
from app.services.entity_resolution import normalize_entity_name

CSV_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "external" / "football-data" / "MEX.csv"
COMPETITION_NAME = "Liga MX"
COUNTRY = "Mexico"
DATA_SOURCE = "football-data"

def get_or_create_competition(db: Session) -> models.Competition:
    normalized = normalize_entity_name(COMPETITION_NAME)
    comp = db.scalar(
        select(models.Competition).where(
            models.Competition.data_source == DATA_SOURCE,
            models.Competition.name == COMPETITION_NAME,
        )
    )
    if comp:
        return comp
    comp = models.Competition(
        external_id=f"fd-mexico-liga-mx",
        name=COMPETITION_NAME,
        country=COUNTRY,
        data_source=DATA_SOURCE,
        source_updated_at=datetime.now(UTC),
        is_mock_data=False,
    )
    db.add(comp)
    db.flush()
    return comp

def get_or_create_season(db: Session, competition: models.Competition, name: str) -> models.Season:
    season = db.scalar(
        select(models.Season).where(
            models.Season.competition_id == competition.id,
            models.Season.name == name,
        )
    )
    if season:
        return season
    season = models.Season(
        external_id=f"fd-mexico-{normalize_entity_name(name)}",
        competition_id=competition.id,
        name=name,
        data_source=DATA_SOURCE,
        source_updated_at=datetime.now(UTC),
        is_mock_data=False,
    )
    db.add(season)
    db.flush()
    return season

def get_or_create_team(db: Session, name: str) -> models.Team:
    normalized = normalize_entity_name(name)
    alias = db.scalar(
        select(models.TeamAlias)
        .join(models.Team)
        .where(
            models.TeamAlias.normalized_alias == normalized,
            models.TeamAlias.provider == DATA_SOURCE,
        )
        .limit(1)
    )
    if alias:
        return db.get(models.Team, alias.team_id)

    team = models.Team(
        external_id=f"fd-mexico-{hashlib.sha1(normalized.encode()).hexdigest()[:16]}",
        name=name,
        country=COUNTRY,
        data_source=DATA_SOURCE,
        source_updated_at=datetime.now(UTC),
        is_mock_data=False,
    )
    db.add(team)
    db.flush()
    db.add(
        models.TeamAlias(
            team_id=team.id,
            provider=DATA_SOURCE,
            alias=name,
            normalized_alias=normalized,
        )
    )
    return team

def row_fingerprint(comp: models.Competition, season: models.Season, match_date: datetime, home: models.Team, away: models.Team) -> str:
    raw = f"{comp.id}|{season.id}|{match_date.isoformat()}|{home.id}|{away.id}"
    return hashlib.sha256(raw.encode()).hexdigest()

DB_PATH = Path(__file__).resolve().parent.parent / "matchvision.db"

def main():
    engine = create_engine(f"sqlite:///{DB_PATH}")
    models.Base.metadata.create_all(engine)

    with Session(engine) as db:
        competition = get_or_create_competition(db)
        db.commit()

        teams_cache: dict[str, models.Team] = {}
        seasons_cache: dict[str, models.Season] = {}

        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            total = 0
            imported = 0
            skipped = 0

            for row in reader:
                total += 1
                season_name = row.get("Season", "").strip()
                date_str = row.get("Date", "").strip()
                time_str = row.get("Time", "").strip()
                home_name = row.get("Home", "").strip()
                away_name = row.get("Away", "").strip()
                hg_str = row.get("HG", "").strip()
                ag_str = row.get("AG", "").strip()

                if not season_name or not date_str or not home_name or not away_name:
                    skipped += 1
                    continue

                try:
                    dt_str = f"{date_str} {time_str}" if time_str else date_str
                    match_date = datetime.strptime(dt_str, "%d/%m/%y %H:%M")
                except ValueError:
                    try:
                        match_date = datetime.strptime(date_str, "%d/%m/%Y")
                    except ValueError:
                        try:
                            match_date = datetime.strptime(date_str, "%d/%m/%y")
                        except ValueError:
                            skipped += 1
                            continue

                match_date = match_date.replace(tzinfo=UTC)

                season = seasons_cache.get(season_name)
                if not season:
                    season = get_or_create_season(db, competition, season_name)
                    seasons_cache[season_name] = season

                home = teams_cache.get(home_name)
                if not home:
                    home = get_or_create_team(db, home_name)
                    teams_cache[home_name] = home

                away = teams_cache.get(away_name)
                if not away:
                    away = get_or_create_team(db, away_name)
                    teams_cache[away_name] = away

                if home.id == away.id:
                    skipped += 1
                    continue

                home_goals = int(hg_str) if hg_str else None
                away_goals = int(ag_str) if ag_str else None

                fp = row_fingerprint(competition, season, match_date, home, away)
                existing = db.scalar(
                    select(models.Match).where(
                        models.Match.data_source == DATA_SOURCE,
                        models.Match.external_id == fp,
                    )
                )
                if existing:
                    skipped += 1
                    continue

                match = models.Match(
                    external_id=fp,
                    competition_id=competition.id,
                    season_id=season.id,
                    home_team_id=home.id,
                    away_team_id=away.id,
                    match_date=match_date,
                    status="finished" if home_goals is not None else "scheduled",
                    home_score=home_goals,
                    away_score=away_goals,
                    data_source=DATA_SOURCE,
                    source_updated_at=datetime.now(UTC),
                    is_mock_data=False,
                )
                db.add(match)
                db.flush()
                imported += 1

                if imported % 100 == 0:
                    db.commit()
                    print(f"  Progreso: {imported} partidos importados...")

            db.commit()

        print(f"\nResumen:")
        print(f"  Total filas leídas: {total}")
        print(f"  Partidos importados: {imported}")
        print(f"  Omitidos (duplicados/inválidos): {skipped}")
        print(f"  Equipos creados: {len(teams_cache)}")
        print(f"  Temporadas creadas: {len(seasons_cache)}")

if __name__ == "__main__":
    main()
