"""
Sync live match statuses from ESPN API.
Run periodically (every 60s) to update match statuses and scores.

Usage: python scripts/live_sync.py
"""
import json
import time
import urllib.request
import ssl
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import models
from app.db import SessionLocal
from sqlalchemy import select

ssl_ctx = ssl.create_default_context()

MX_TEAM_MAP = {
    "Necaxa": "Necaxa",
    "Atlante": "Atlante",
    "Tijuana": "Club Tijuana",
    "Tigres UANL": "Tigres UANL",
    "Atlético de San Luis": "Atl. San Luis",
    "Cruz Azul": "Cruz Azul",
    "León": "Club Leon",
    "Atlas": "Atlas",
    "FC Juarez": "Juarez",
    "Puebla": "Puebla",
    "Pumas UNAM": "UNAM Pumas",
    "Pachuca": "Pachuca",
    "Monterrey": "Monterrey",
    "Santos": "Santos Laguna",
    "Guadalajara": "Guadalajara Chivas",
    "Toluca": "Toluca",
    "Querétaro": "Queretaro",
    "América": "Club America",
}

ESPN_STATUS_MAP = {
    "STATUS_SCHEDULED": "scheduled",
    "STATUS_IN_PROGRESS": "live",
    "STATUS_HALFTIME": "live",
    "STATUS_FINAL": "finished",
    "STATUS_END_OF_PERIOD": "live",
}


def fetch_scoreboard():
    url = "https://site.api.espn.com/apis/site/v2/sports/soccer/mex.1/scoreboard"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    })
    with urllib.request.urlopen(req, context=ssl_ctx, timeout=15) as resp:
        return json.loads(resp.read())


def sync():
    db = SessionLocal()
    data = fetch_scoreboard()
    updates = 0

    for e in data.get("events", []):
        espn_status = e.get("status", {}).get("type", {}).get("name", "")
        local_status = ESPN_STATUS_MAP.get(espn_status)
        home_team = None
        away_team = None
        home_score = None
        away_score = None

        competitors = e.get("competitions", [{}])[0].get("competitors", [])
        for c in competitors:
            is_home = c.get("homeAway") == "home"
            name = c.get("team", {}).get("displayName", "")
            score = c.get("score")
            if is_home:
                home_team = MX_TEAM_MAP.get(name)
                home_score = int(score) if score and score.isdigit() else None
            else:
                away_team = MX_TEAM_MAP.get(name)
                away_score = int(score) if score and score.isdigit() else None

        if not home_team or not away_team or not local_status:
            continue

        # Find match in DB by team names and approximate date (today ± 1 day)
        matches = list(db.scalars(
            select(models.Match).where(
                models.Match.home_team_id == db.scalar(
                    select(models.Team.id).where(models.Team.name.ilike(home_team))
                ),
                models.Match.away_team_id == db.scalar(
                    select(models.Team.id).where(models.Team.name.ilike(away_team))
                ),
                models.Match.match_date >= datetime.now(timezone.utc).replace(hour=0, minute=0) - __import__('datetime').timedelta(days=1),
                models.Match.match_date <= datetime.now(timezone.utc).replace(hour=23, minute=59) + __import__('datetime').timedelta(days=1),
            ).order_by(models.Match.match_date.desc()).limit(1)
        ).all())

        for match in matches:
            changed = False
            if match.status != local_status:
                match.status = local_status
                changed = True
            if home_score is not None and (match.home_score != home_score or match.away_score != away_score):
                match.home_score = home_score
                match.away_score = away_score
                changed = True
            if changed:
                updates += 1

    db.commit()
    db.close()
    return updates


if __name__ == "__main__":
    print(f"[{datetime.now().isoformat()}] Syncing live matches...")
    updated = sync()
    print(f"  Updated {updated} matches")

    if "--loop" in sys.argv:
        print("  Running in loop mode (30s interval)...")
        while True:
            time.sleep(30)
            try:
                u = sync()
                if u:
                    print(f"  [{datetime.now().isoformat()}] Updated {u} matches")
            except Exception as e:
                print(f"  [{datetime.now().isoformat()}] Error: {e}")
