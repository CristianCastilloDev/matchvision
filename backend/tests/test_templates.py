from pathlib import Path

from app.services.local_imports import CSV_TEMPLATES


EXPECTED = {
    "matches": "competition,season,match_date,home_team,away_team,home_goals,away_goals,home_yellow_cards,away_yellow_cards,home_red_cards,away_red_cards,home_corners,away_corners\n",
    "players": "player_name,team,position,active,penalty_taker,free_kick_taker\n",
    "player_matches": "match_id,player_name,team,started,minutes_played,goals,shots,shots_on_target,xg,yellow_cards,red_cards\n",
    "upcoming_matches": "competition,season,match_date,home_team,away_team,venue,referee\n",
}


def test_template_keys_and_headers_are_literal_contract() -> None:
    assert CSV_TEMPLATES == EXPECTED
    templates = Path(__file__).parents[1] / "data" / "templates"
    for name, header in EXPECTED.items():
        assert (templates / f"{name}.csv").read_text(encoding="utf-8") == header
