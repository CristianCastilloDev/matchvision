from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.services.entity_resolution import normalize_entity_name


DEMO_SOURCE = "demo"


def seed_demo_data(db: Session) -> int:
    """Create a small, deterministic and explicitly labelled offline dataset."""

    existing = db.scalar(
        select(models.Competition).where(
            models.Competition.data_source == DEMO_SOURCE,
            models.Competition.external_id == "demo-competition",
        )
    )
    if existing:
        upcoming = db.scalar(
            select(models.Match).where(
                models.Match.data_source == DEMO_SOURCE,
                models.Match.external_id == "demo-upcoming",
            )
        )
        return upcoming.id if upcoming else 0

    now = datetime.now(UTC).replace(microsecond=0)
    competition = models.Competition(
        external_id="demo-competition",
        name="Liga Demostración",
        country="MX",
        gender="masculino",
        data_source=DEMO_SOURCE,
        source_updated_at=now,
        is_mock_data=True,
    )
    db.add(competition)
    db.flush()
    season = models.Season(
        external_id="demo-season",
        competition_id=competition.id,
        name="2026 Demo",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        data_source=DEMO_SOURCE,
        source_updated_at=now,
        is_mock_data=True,
    )
    db.add(season)
    db.flush()

    names = ["Atlético Aurora", "Deportivo Pacífico", "Unión Sierra", "Real Bahía"]
    teams: list[models.Team] = []
    for index, name in enumerate(names, start=1):
        team = models.Team(
            external_id=f"demo-team-{index}",
            name=name,
            short_name=name.split()[0],
            country="MX",
            data_source=DEMO_SOURCE,
            source_updated_at=now,
            is_mock_data=True,
        )
        db.add(team)
        db.flush()
        db.add(
            models.TeamAlias(
                team_id=team.id,
                provider=DEMO_SOURCE,
                alias=name,
                normalized_alias=normalize_entity_name(name),
            )
        )
        teams.append(team)

    positions = ["Delantero", "Mediocampista", "Defensa"]
    players_by_team: dict[int, list[models.Player]] = {}
    for team_index, team in enumerate(teams, start=1):
        players_by_team[team.id] = []
        for player_index, position in enumerate(positions, start=1):
            name = f"Jugador {team_index}-{player_index}"
            player = models.Player(
                external_id=f"demo-player-{team_index}-{player_index}",
                current_team_id=team.id,
                name=name,
                primary_position=position,
                active=True,
                data_source=DEMO_SOURCE,
                source_updated_at=now,
                is_mock_data=True,
            )
            db.add(player)
            db.flush()
            db.add(
                models.PlayerAlias(
                    player_id=player.id,
                    provider=DEMO_SOURCE,
                    alias=name,
                    normalized_alias=normalize_entity_name(name),
                )
            )
            players_by_team[team.id].append(player)

    schedule = [
        (0, 1, 2, 1),
        (2, 3, 0, 0),
        (1, 2, 1, 2),
        (3, 0, 1, 1),
        (0, 2, 3, 0),
        (1, 3, 2, 2),
        (1, 0, 0, 1),
        (3, 2, 1, 2),
        (2, 1, 1, 1),
        (0, 3, 2, 0),
        (2, 0, 2, 2),
        (3, 1, 0, 1),
        (0, 1, 1, 0),
        (2, 3, 2, 1),
        (1, 2, 2, 0),
        (3, 0, 1, 3),
    ]
    latest_historical = now - timedelta(days=10)
    for index, (home_idx, away_idx, home_goals, away_goals) in enumerate(schedule):
        match_date = latest_historical - timedelta(days=7 * (len(schedule) - index - 1))
        home = teams[home_idx]
        away = teams[away_idx]
        match = models.Match(
            external_id=f"demo-history-{index + 1}",
            competition_id=competition.id,
            season_id=season.id,
            home_team_id=home.id,
            away_team_id=away.id,
            match_date=match_date,
            venue=f"Estadio {home.short_name}",
            status="finished",
            home_score=home_goals,
            away_score=away_goals,
            halftime_home_score=min(home_goals, index % 2),
            halftime_away_score=min(away_goals, (index + 1) % 2),
            data_source=DEMO_SOURCE,
            source_updated_at=match_date + timedelta(hours=3),
            is_mock_data=True,
        )
        db.add(match)
        db.flush()
        home_cards = 2 + index % 3
        away_cards = 1 + (index + 1) % 3
        db.add_all(
            [
                models.TeamMatchStatistics(
                    match_id=match.id,
                    team_id=home.id,
                    possession=52 + index % 7,
                    shots=10 + index % 6,
                    shots_on_target=4 + index % 4,
                    corners=4 + index % 5,
                    fouls=10 + index % 5,
                    yellow_cards=home_cards,
                    red_cards=0,
                    xg=round(0.65 + home_goals * 0.62, 2),
                    data_source=DEMO_SOURCE,
                    source_updated_at=match.source_updated_at,
                    is_mock_data=True,
                ),
                models.TeamMatchStatistics(
                    match_id=match.id,
                    team_id=away.id,
                    possession=48 - index % 7,
                    shots=8 + index % 7,
                    shots_on_target=3 + index % 4,
                    corners=3 + index % 5,
                    fouls=11 + index % 5,
                    yellow_cards=away_cards,
                    red_cards=1 if index == 7 else 0,
                    xg=round(0.55 + away_goals * 0.60, 2),
                    data_source=DEMO_SOURCE,
                    source_updated_at=match.source_updated_at,
                    is_mock_data=True,
                ),
            ]
        )
        for team, goals, cards in ((home, home_goals, home_cards), (away, away_goals, away_cards)):
            players = players_by_team[team.id]
            goal_allocations = [goals, 0, 0]
            card_allocations = [cards % 2, cards // 2, cards - (cards % 2) - (cards // 2)]
            for pindex, player in enumerate(players):
                db.add(
                    models.PlayerMatch(
                        player_id=player.id,
                        match_id=match.id,
                        team_id=team.id,
                        started=True,
                        minutes_played=90,
                        position=player.primary_position,
                        goals=goal_allocations[pindex],
                        assists=0,
                        shots=3 if pindex == 0 else 1,
                        shots_on_target=2 if pindex == 0 else 0,
                        xg=0.55 if pindex == 0 else 0.08,
                        yellow_cards=card_allocations[pindex],
                        red_cards=0,
                        fouls=1 + pindex,
                        data_source=DEMO_SOURCE,
                        source_updated_at=match.source_updated_at,
                        is_mock_data=True,
                    )
                )

    upcoming = models.Match(
        external_id="demo-upcoming",
        competition_id=competition.id,
        season_id=season.id,
        home_team_id=teams[0].id,
        away_team_id=teams[1].id,
        match_date=now + timedelta(days=7),
        venue="Estadio Aurora",
        round_name="Jornada demostración",
        status="scheduled",
        data_source=DEMO_SOURCE,
        source_updated_at=now,
        is_mock_data=True,
    )
    db.add(upcoming)
    db.flush()
    for team in (teams[0], teams[1]):
        for pindex, player in enumerate(players_by_team[team.id]):
            db.add(
                models.Lineup(
                    match_id=upcoming.id,
                    team_id=team.id,
                    player_id=player.id,
                    started=True,
                    confirmed=False,
                    position=player.primary_position,
                    shirt_number=9 + pindex,
                    expected_minutes=82 - pindex * 8,
                    data_source=DEMO_SOURCE,
                    source_updated_at=now,
                    is_mock_data=True,
                )
            )

    model = models.ModelVersion(
        name="goals-independent-poisson",
        version="1.0.0",
        trained_at=now,
        max_data_date=latest_historical,
        features=[
            "home_recent_goals_for",
            "home_recent_goals_against",
            "away_recent_goals_for",
            "away_recent_goals_against",
            "competition_home_goal_average",
            "competition_away_goal_average",
        ],
        hyperparameters={"score_min": 0, "score_max": 8, "recency_window": 10},
        data_sources=[DEMO_SOURCE],
        dataset_hash="demo-dataset-not-for-production",
        status="active",
        is_mock_data=True,
    )
    db.add(model)
    db.commit()
    return upcoming.id
