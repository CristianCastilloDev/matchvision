"""
Auto-import match statistics from ESPN for finished Liga MX matches.
Runs every 5 minutes, imports stats for newly finished matches.

Usage: python scripts/auto_import_stats.py
"""
import json
import time
import ssl
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import models
from app.db import SessionLocal
from sqlalchemy import select

ssl_ctx = ssl.create_default_context()
MX_TEAM_MAP = {
    "Necaxa": "Necaxa", "Atlante": "Atlante", "Tijuana": "Club Tijuana",
    "Tigres UANL": "Tigres UANL", "Atlético de San Luis": "Atl. San Luis",
    "Cruz Azul": "Cruz Azul", "León": "Club Leon", "Atlas": "Atlas",
    "FC Juarez": "Juarez", "Puebla": "Puebla", "Pumas UNAM": "UNAM Pumas",
    "Pachuca": "Pachuca", "Monterrey": "Monterrey", "Santos": "Santos Laguna",
    "Guadalajara": "Guadalajara Chivas", "Toluca": "Toluca",
    "Querétaro": "Queretaro", "América": "Club America",
    "Mazatlán": "Mazatlan FC",
}


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    })
    with urllib.request.urlopen(req, context=ssl_ctx, timeout=15) as resp:
        return json.loads(resp.read())


def find_match(db, espn_home, espn_away) -> int | None:
    home_db = MX_TEAM_MAP.get(espn_home)
    away_db = MX_TEAM_MAP.get(espn_away)
    if not home_db or not away_db:
        return None
    hid = db.scalar(select(models.Team.id).where(models.Team.name.ilike(home_db)))
    aid = db.scalar(select(models.Team.id).where(models.Team.name.ilike(away_db)))
    if not hid or not aid:
        return None
    match = db.scalar(
        select(models.Match).where(
            models.Match.home_team_id == hid,
            models.Match.away_team_id == aid,
            models.Match.match_date >= datetime.now(timezone.utc).replace(hour=0, minute=0) - __import__('datetime').timedelta(days=2),
        ).order_by(models.Match.match_date.desc())
    )
    return match.id if match else None


def import_stats():
    db = SessionLocal()
    scoreboard = fetch("https://site.api.espn.com/apis/site/v2/sports/soccer/mex.1/scoreboard")

    imported = 0
    for e in scoreboard.get("events", []):
        status = e.get("status", {}).get("type", {}).get("name", "")
        if status != "STATUS_FULL_TIME":
            continue
        espn_id = e.get("id", "")
        comps = e.get("competitions", [{}])[0].get("competitors", [])
        espn_home = next((c for c in comps if c.get("homeAway") == "home"), {}).get("team", {}).get("displayName", "")
        espn_away = next((c for c in comps if c.get("homeAway") == "away"), {}).get("team", {}).get("displayName", "")
        if not espn_home or not espn_away:
            continue

        match_id = find_match(db, espn_home, espn_away)
        if not match_id:
            continue

        # Check if stats already imported
        existing = db.scalar(
            select(models.TeamMatchStatistics).where(
                models.TeamMatchStatistics.match_id == match_id,
                models.TeamMatchStatistics.data_source == "espn-api",
            ).limit(1)
        )
        if existing:
            continue

        # Fetch detailed stats
        try:
            summary = fetch(f"https://site.api.espn.com/apis/site/v2/sports/soccer/mex.1/summary?event={espn_id}")
        except Exception:
            continue

        boxscore = summary.get("boxscore", {})
        teams_bs = boxscore.get("teams", [])
        if len(teams_bs) < 2:
            continue

        now = datetime.now(timezone.utc)
        for bs_team in teams_bs:
            bs_name = bs_team.get("team", {}).get("displayName", "")
            db_name = MX_TEAM_MAP.get(bs_name)
            if not db_name:
                continue
            tid = db.scalar(select(models.Team.id).where(models.Team.name.ilike(db_name)))
            if not tid:
                continue

            stats = {s.get("name", ""): s.get("displayValue", "0") for s in bs_team.get("statistics", [])}

            def g(name: str) -> int | None:
                v = stats.get(name, "0")
                try:
                    # Handle percentage values like "64.2"
                    if "." in v and "%" not in name:
                        return int(float(v))
                    return int(float(v))
                except (ValueError, TypeError):
                    return None

            data = {
                "possession": g("possessionPct"),
                "shots": g("totalShots"),
                "shots_on_target": g("shotsOnTarget"),
                "corners": g("wonCorners"),
                "fouls": g("foulsCommitted"),
                "yellow_cards": g("yellowCards"),
                "red_cards": g("redCards"),
                "passes": g("totalPasses"),
            }
            data = {k: v for k, v in data.items() if v is not None}

            stat_rec = db.scalar(
                select(models.TeamMatchStatistics).where(
                    models.TeamMatchStatistics.match_id == match_id,
                    models.TeamMatchStatistics.team_id == tid,
                )
            )
            if stat_rec:
                for k, v in data.items():
                    setattr(stat_rec, k, v)
                stat_rec.data_source = "espn-api"
                stat_rec.source_updated_at = now
                stat_rec.is_mock_data = False
            else:
                db.add(models.TeamMatchStatistics(
                    match_id=match_id, team_id=tid, data_source="espn-api",
                    source_updated_at=now, is_mock_data=False, **data,
                ))

        imported += 1
        print(f"  [{espn_id}] {espn_home} vs {espn_away} → stats imported")

    db.commit()
    db.close()
    return imported


if __name__ == "__main__":
    print(f"[{datetime.now().isoformat()}] Checking for finished matches...")
    n = import_stats()
    print(f"  Imported stats for {n} matches")

    if "--loop" in sys.argv:
        while True:
            time.sleep(300)
            try:
                n = import_stats()
                if n:
                    print(f"  [{datetime.now().isoformat()}] Imported {n}")
            except Exception as e:
                print(f"  [{datetime.now().isoformat()}] Error: {e}")
