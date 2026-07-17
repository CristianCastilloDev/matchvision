"""
Import real Liga MX match statistics from API-Football.
Replaces synthetic team_match_statistics with real data.
Respects the 100 requests/day free tier limit.
Saves progress in a checkpoint file to resume daily.
"""

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import models

API_KEYS = [
    "315b95d4d5d8daa7af3c387d02dd913d",
    "f66c7b4cc97b7f0b5e130f1875a3fe41",
    "2c03e5f94ce74e71415aea164e22f62c",
    "486a6761689014818d67d82199579110",
]
BASE_URL = "https://v3.football.api-sports.io"
LEAGUE_ID = 262
DB_PATH = Path(__file__).resolve().parent.parent / "matchvision.db"
CHECKPOINT_PATH = Path(__file__).resolve().parent / ".import_checkpoint.json"

STATS_MAP = {
    "Total Shots": "shots",
    "Shots on Goal": "shots_on_target",
    "Corner Kicks": "corners",
    "Fouls": "fouls",
    "Yellow Cards": "yellow_cards",
    "Red Cards": "red_cards",
    "Ball Possession": "possession",
    "Total passes": "passes",
    "expected_goals": "xg",
}

key_usage = {k: 0 for k in API_KEYS}


def api_get(endpoint: str, params: dict | None = None) -> tuple[dict, bool]:
    for key in API_KEYS:
        if key_usage[key] >= 100:
            continue
        headers = {"x-apisports-key": key}
        resp = requests.get(
            f"{BASE_URL}{endpoint}", headers=headers, params=params, timeout=30
        )
        if resp.status_code == 429:
            key_usage[key] = 100
            continue
        key_usage[key] = key_usage.get(key, 0) + 1
        remaining = 100 - key_usage[key]
        key_name = key[:10] + "..."
        print(f"  [{resp.status_code}] {endpoint} — key={key_name} ({remaining} left today)")
        return resp.json(), False
    print("  All keys exhausted (429)")
    return {}, True


API_TO_DB_TEAM = {
    "Guadalajara Chivas": "Guadalajara Chivas",
    "Tigres UANL": "Tigres UANL",
    "Club Tijuana": "Club Tijuana",
    "Toluca": "Toluca",
    "Monterrey": "Monterrey",
    "Atlas": "Atlas",
    "Santos Laguna": "Santos Laguna",
    "U.N.A.M. - Pumas": ["UNAM Pumas", "Pumas UNAM"],
    "Club America": "Club America",
    "Necaxa": "Necaxa",
    "Leon": "Club Leon",
    "Club Queretaro": "Queretaro",
    "Puebla": "Puebla",
    "CF Pachuca": "Pachuca",
    "Cruz Azul": "Cruz Azul",
    "FC Juarez": "Juarez",
    "Atletico San Luis": "Atl. San Luis",
    "Mazatlán": ["Mazatlan FC", "Mazatlán"],
    "Chiapas": "Chiapas",
    "Monarcas": "Monarcas",
    "Atlante": "Atlante",
    "Veracruz": "Veracruz",
    "Lobos BUAP": "Lobos BUAP",
    "Dorados de Sinaloa": "Dorados de Sinaloa",
    "Leones Negros": "Leones Negros",
}


def find_team_in_db(db: Session, api_name: str):
    lookup = API_TO_DB_TEAM.get(api_name, api_name)
    names = [lookup] if isinstance(lookup, str) else lookup
    for name in names:
        team = db.scalar(
            select(models.Team).where(models.Team.name.ilike(name)).limit(1)
        )
        if team:
            return team
        team = db.scalar(
            select(models.Team)
            .join(models.TeamAlias, models.TeamAlias.team_id == models.Team.id)
            .where(models.TeamAlias.alias.ilike(name))
            .limit(1)
        )
        if team:
            return team
    return None


def load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        return json.loads(CHECKPOINT_PATH.read_text())
    return {"next_season_idx": 0, "next_fixture_idx": 0, "done": False}


def save_checkpoint(state: dict):
    CHECKPOINT_PATH.write_text(json.dumps(state, indent=2))
    print(f"  Checkpoint saved: season_idx={state['next_season_idx']}, fixture_idx={state['next_fixture_idx']}")


def main():
    engine = create_engine(f"sqlite:///{DB_PATH}")
    models.Base.metadata.create_all(engine)

    state = load_checkpoint()
    if state.get("done"):
        print("All seasons completed! 🎉")
        return

    seasons = [2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025]
    start_season_idx = state.get("next_season_idx", 0)

    total_updated = 0
    total_matched = 0
    exhausted = False

    with Session(engine) as db:
        for si, season in enumerate(seasons):
            if si < start_season_idx:
                continue
            if exhausted:
                break

            print(f"\n=== Season {season} ===")
            data, exh = api_get("/fixtures", {"league": LEAGUE_ID, "season": season})
            if exh:
                exhausted = True
                break

            fixtures = data.get("response", [])
            fixtures = [f for f in fixtures if f.get("fixture", {}).get("status", {}).get("short") == "FT"]
            print(f"  {len(fixtures)} finished matches")

            start_fixture_idx = state.get("next_fixture_idx", 0) if si == start_season_idx else 0

            for fi, fixture in enumerate(fixtures):
                if fi < start_fixture_idx:
                    continue
                if exhausted:
                    break

                fixture_id = fixture["fixture"]["id"]
                home_api = fixture["teams"]["home"]["name"]
                away_api = fixture["teams"]["away"]["name"]


                match = None
                home_team = find_team_in_db(db, home_api)
                away_team = find_team_in_db(db, away_api)
                if home_team and away_team:
                    match_date = datetime.fromisoformat(
                        fixture["fixture"]["date"].replace("Z", "+00:00")
                    )
                    match = db.scalar(
                        select(models.Match).where(
                            models.Match.match_date >= match_date.replace(hour=0, minute=0, second=0),
                            models.Match.match_date <= match_date.replace(hour=23, minute=59, second=59),
                            models.Match.home_team_id == home_team.id,
                            models.Match.away_team_id == away_team.id,
                        ).limit(1)
                    )

                if not match:
                    print(f"  ⏭️  #{fixture_id}: {home_api} vs {away_api} — not in DB")
                    continue

                total_matched += 1

                stats_json, exh = api_get("/fixtures/statistics", {"fixture": fixture_id})
                if exh:
                    exhausted = True
                    save_checkpoint({
                        "next_season_idx": si,
                        "next_fixture_idx": fi,
                        "done": False,
                    })
                    break

                if not stats_json:
                    continue

                for team_stats in stats_json.get("response", []):
                    api_team_name = team_stats["team"]["name"]
                    team = find_team_in_db(db, api_team_name)
                    if not team:
                        continue

                    tid = team.id
                    if tid not in (match.home_team_id, match.away_team_id):
                        continue

                    stat_values = {}
                    for s in team_stats["statistics"]:
                        field = STATS_MAP.get(s["type"])
                        if not field or s["value"] is None:
                            continue
                        val = s["value"]
                        if field == "possession" and isinstance(val, str):
                            val = int(val.replace("%", ""))
                        elif field == "xg":
                            val = float(val)
                        else:
                            val = int(val)
                        stat_values[field] = val

                    if not stat_values:
                        continue

                    existing = db.scalar(
                        select(models.TeamMatchStatistics).where(
                            models.TeamMatchStatistics.match_id == match.id,
                            models.TeamMatchStatistics.team_id == tid,
                        ).limit(1)
                    )

                    if existing:
                        for field, val in stat_values.items():
                            setattr(existing, field, val)
                        existing.data_source = "api-football"
                        existing.source_updated_at = datetime.now(UTC)
                        existing.is_mock_data = False
                    else:
                        db.add(models.TeamMatchStatistics(
                            match_id=match.id,
                            team_id=tid,
                            data_source="api-football",
                            source_updated_at=datetime.now(UTC),
                            is_mock_data=False,
                            **stat_values,
                        ))
                    total_updated += 1

                db.commit()
                print(f"  ✅ #{fixture_id}: {home_api} vs {away_api}")

                time.sleep(0.5)

            if exhausted:
                break

            state["next_season_idx"] = si + 1
            state["next_fixture_idx"] = 0
            save_checkpoint(state)

        if not exhausted:
            state["done"] = True
            save_checkpoint(state)

    print(f"\n{'='*50}")
    print(f"Session complete!")
    print(f"  Matches matched: {total_matched}")
    print(f"  Team stats updated: {total_updated}")
    print(f"  Requests per key:")
    for k in API_KEYS:
        print(f"    {k[:10]}... : {key_usage[k]}/100")
    if exhausted:
        print(f"  Rate limit reached on all keys. Run again tomorrow to continue.")
    else:
        print(f"  All seasons imported! 🎉")


if __name__ == "__main__":
    main()
    if "--continuous" in sys.argv and not load_checkpoint().get("done"):
        print(f"\nRate limit reached. Waiting 24h for reset...")
        while True:
            time.sleep(3600 * 24)
            print(f"\n{'='*50}")
            print(f"New day — resuming...")
            key_usage.clear()
            for k in API_KEYS:
                key_usage[k] = 0
            main()
            if load_checkpoint().get("done"):
                print("All seasons complete! 🎉")
                break
            print(f"\nRate limit again. Waiting another 24h...")
