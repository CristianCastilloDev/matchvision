"""
Scrape upcoming Liga MX fixtures from ESPN (free, no auth).
Outputs JSON that can be imported into the database.

Usage: python scripts/scrape_fixtures.py
"""
import json
import urllib.request
import urllib.error
import ssl
from datetime import datetime, timedelta

ssl_ctx = ssl.create_default_context()

def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    })
    with urllib.request.urlopen(req, context=ssl_ctx, timeout=15) as resp:
        return json.loads(resp.read())

def scrape_espn():
    today = datetime.now()
    start = today.strftime("%Y%m%d")
    end = (today + timedelta(days=60)).strftime("%Y%m%d")
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/mex.1/scoreboard?dates={start}-{end}"
    data = fetch(url)
    matches = []
    for e in data.get("events", []):
        comp = e.get("competitions", [{}])[0]
        comps = comp.get("competitors", [])
        if len(comps) < 2:
            continue
        home = comps[0]["team"]["displayName"]
        away = comps[1]["team"]["displayName"]
        raw_date = e.get("date", "")
        status = e.get("status", {}).get("type", {}).get("description", "")
        is_finished = status == "Final"
        home_score = None
        away_score = None
        if is_finished:
            try:
                home_score = int(comps[0].get("score", "0"))
                away_score = int(comps[1].get("score", "0"))
            except (ValueError, TypeError):
                pass

        matches.append({
            "home": home,
            "away": away,
            "date": raw_date,
            "status": "finished" if is_finished else "scheduled",
            "home_score": home_score,
            "away_score": away_score,
        })
    return matches

def save(matches):
    print(f"\nTotal: {len(matches)} partidos encontrados\n")
    for m in matches:
        score = f"{m['home_score']}-{m['away_score']}" if m['home_score'] is not None else ""
        print(f"  {m['date'][:10]}  {m['home']:25s} vs {m['away']:25s}  {score:6s} [{m['status']}]")

    outpath = "data/upcoming_fixtures.json"
    with open(outpath, "w") as f:
        json.dump(matches, f, indent=2, default=str)
    print(f"\nGuardado en {outpath}")
    print("\n--- JSON ---")
    print(json.dumps(matches, indent=2, default=str))

if __name__ == "__main__":
    print("=== Scrapeando ESPN Liga MX ===")
    save(scrape_espn())
