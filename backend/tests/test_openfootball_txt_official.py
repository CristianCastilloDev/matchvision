from __future__ import annotations

import pytest

from app.data_sources.openfootball.football_txt_parser import parse_football_txt_data


def test_official_2026_team_schedule_keeps_date_round_scores_and_annotation() -> None:
    dataset = parse_football_txt_data(
        """
= Regionalliga Ost | Kremser SC

Fri 10.04.2026 19:30  ▪26  (h) Wiener Viktoria  1-0 (0-0)
  (Michael Ambichl 90'+3)
"""
    )

    assert dataset.competition == "Regionalliga Ost"
    assert dataset.season is None
    match = dataset.matches[0]
    assert (match.date, match.kickoff_time) == ("2026-04-10", "19:30")
    assert (match.round, match.matchday) == ("Matchday 26", 26)
    assert (match.home_team, match.away_team) == ("Kremser SC", "Wiener Viktoria")
    assert (match.fulltime_home_goals, match.fulltime_away_goals) == (1, 0)
    assert (match.halftime_home_goals, match.halftime_away_goals) == (0, 0)
    assert match.notes == "(Michael Ambichl 90'+3)"


@pytest.mark.parametrize(
    "date_line",
    (
        "Friday, July 10, 2026",
        "Fri, 10 July, 2026",
        "Fri, 10.07.2026",
    ),
)
def test_official_comma_and_numeric_dates_do_not_become_scores(date_line: str) -> None:
    match = parse_football_txt_data(
        f"= World Cup 2026\n{date_line}\nBrazil - Mexico"
    ).matches[0]

    assert match.date == "2026-07-10"
    assert (match.home_team, match.away_team) == ("Brazil", "Mexico")
    assert match.status.value == "scheduled"
    assert match.fulltime_home_goals is None
    assert match.fulltime_away_goals is None


@pytest.mark.parametrize("periods_on_next_line", (False, True))
def test_liga_mx_penalty_result_keeps_pen_ft_ht_and_no_invented_et(
    periods_on_next_line: bool,
) -> None:
    separator = "\n" if periods_on_next_line else " "
    match = parse_football_txt_data(
        "= Liga MX 2024/25\n"
        "Thu Nov 21\n"
        f"21:00 Club Tijuana v CF América 2-3 pen.{separator}(2-2, 1-0)"
    ).matches[0]

    assert (match.penalty_home_goals, match.penalty_away_goals) == (2, 3)
    assert (match.fulltime_home_goals, match.fulltime_away_goals) == (2, 2)
    assert (match.halftime_home_goals, match.halftime_away_goals) == (1, 0)
    assert match.extra_time_home_goals is None
    assert match.extra_time_away_goals is None


def test_premier_league_liverpool_reference_line_still_parses() -> None:
    match = parse_football_txt_data(
        """
= English Premier League 2025/26
▪ Matchday 1
Fri Aug 15
20:00 Liverpool FC v AFC Bournemouth 4-2 (1-0)
""",
        source_file="england/2025-26/1-premierleague.txt",
    ).matches[0]

    assert match.competition == "English Premier League"
    assert match.season == "2025-26"
    assert (match.date, match.kickoff_time) == ("2025-08-15", "20:00")
    assert (match.home_team, match.away_team) == (
        "Liverpool FC",
        "AFC Bournemouth",
    )
    assert (match.fulltime_home_goals, match.fulltime_away_goals) == (4, 2)
    assert (match.halftime_home_goals, match.halftime_away_goals) == (1, 0)


def test_laliga_unicode_fixture_with_dash_stays_scheduled() -> None:
    match = parse_football_txt_data(
        "= LaLiga 2025/26\nFri, August 15, 2025\nAtlético Madrid - Real Madrid"
    ).matches[0]

    assert match.competition == "LaLiga"
    assert match.date == "2025-08-15"
    assert (match.home_team, match.away_team) == ("Atlético Madrid", "Real Madrid")
    assert match.status.value == "scheduled"
    assert match.fulltime_home_goals is None


@pytest.mark.parametrize(
    ("line", "expected_ft", "expected_ht", "expected_et", "expected_pen"),
    (
        (
            "Bayern München v Chelsea 1-1 aet, 3-4 pen",
            None,
            None,
            (1, 1),
            (3, 4),
        ),
        (
            "Bayern München v Chelsea 1-1 aet (1-1, 0-0) 3-4 pen",
            (1, 1),
            (0, 0),
            (1, 1),
            (3, 4),
        ),
        (
            "Bayern München v Chelsea 3-4 pen (1-1, 1-1, 0-0)",
            (1, 1),
            (0, 0),
            (1, 1),
            (3, 4),
        ),
        (
            "Bayern München v Chelsea 3-4 pen 1-1 aet (1-1, 0-0)",
            (1, 1),
            (0, 0),
            (1, 1),
            (3, 4),
        ),
        (
            "Bayern München v Chelsea 3-4 pen 1-1 aet",
            None,
            None,
            (1, 1),
            (3, 4),
        ),
        (
            "Bayern München 3-4 pen. 1-1 a.e.t. Chelsea",
            None,
            None,
            (1, 1),
            (3, 4),
        ),
    ),
)
def test_official_aet_penalty_variants_do_not_regress(
    line: str,
    expected_ft: tuple[int, int] | None,
    expected_ht: tuple[int, int] | None,
    expected_et: tuple[int, int],
    expected_pen: tuple[int, int],
) -> None:
    match = parse_football_txt_data(
        f"= Cup 2026\n2026-01-01\n{line}", competition="Cup", season="2026"
    ).matches[0]

    assert (match.home_team, match.away_team) == ("Bayern München", "Chelsea")
    actual_ft = (match.fulltime_home_goals, match.fulltime_away_goals)
    actual_ht = (match.halftime_home_goals, match.halftime_away_goals)
    assert actual_ft == (expected_ft if expected_ft is not None else (None, None))
    assert actual_ht == (expected_ht if expected_ht is not None else (None, None))
    assert (match.extra_time_home_goals, match.extra_time_away_goals) == expected_et
    assert (match.penalty_home_goals, match.penalty_away_goals) == expected_pen
