from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, repositories, schemas
from app.services.entity_resolution import normalize_entity_name, resolve_name


class AmbiguousEntityError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        entity_type: str = "unknown",
        source_name: str = "unknown",
        candidate_ids: list[int] | None = None,
        best_score: float | None = None,
    ) -> None:
        super().__init__(message)
        self.entity_type = entity_type
        self.source_name = source_name
        self.candidate_ids = candidate_ids or []
        self.best_score = best_score


def _manual_id(prefix: str) -> str:
    return f"manual-{prefix}-{uuid.uuid4()}"


def _resolve_competition(db: Session, name: str, is_mock_data: bool) -> models.Competition:
    normalized = normalize_entity_name(name)
    competitions = list(
        db.scalars(
            select(models.Competition).where(
                models.Competition.is_mock_data == is_mock_data
            )
        ).all()
    )
    exact_ids = {
        competition.id
        for competition in competitions
        if normalize_entity_name(competition.name) == normalized
    }
    exact_ids.update(
        db.scalars(
            select(models.CompetitionAlias.competition_id)
            .join(
                models.Competition,
                models.Competition.id == models.CompetitionAlias.competition_id,
            )
            .where(
                models.CompetitionAlias.normalized_alias == normalized,
                models.Competition.is_mock_data == is_mock_data,
            )
        ).all()
    )
    if len(exact_ids) == 1:
        return db.get(models.Competition, exact_ids.pop())  # type: ignore[return-value]
    if len(exact_ids) > 1:
        raise AmbiguousEntityError(f"La competición '{name}' coincide con varias entidades")
    resolution = resolve_name(name, ((item.id, item.name) for item in competitions))
    if resolution.entity_id is not None:
        return db.get(models.Competition, resolution.entity_id)  # type: ignore[return-value]
    if resolution.status == "manual_review":
        db.add(
            models.EntityResolutionConflict(
                entity_type="competition",
                provider="manual",
                source_name=name,
                normalized_name=normalized,
                candidate_ids=[item.entity_id for item in resolution.candidates],
                best_score=resolution.score,
            )
        )
        raise AmbiguousEntityError(
            f"La competición '{name}' es parecida a otra existente y requiere revisión manual",
            entity_type="competition",
            source_name=name,
            candidate_ids=[item.entity_id for item in resolution.candidates],
            best_score=resolution.score,
        )
    entity = models.Competition(
        external_id=_manual_id("competition"),
        name=name.strip(),
        data_source="manual",
        source_updated_at=datetime.now(UTC),
        is_mock_data=is_mock_data,
    )
    db.add(entity)
    db.flush()
    db.add(
        models.CompetitionAlias(
            competition_id=entity.id,
            provider="manual",
            alias=name.strip(),
            normalized_alias=normalized,
        )
    )
    return entity


def _resolve_season(
    db: Session, competition: models.Competition, name: str, is_mock_data: bool
) -> models.Season:
    normalized = normalize_entity_name(name)
    seasons = list(
        db.scalars(
            select(models.Season).where(
                models.Season.competition_id == competition.id,
                models.Season.is_mock_data == is_mock_data,
            )
        ).all()
    )
    exact = [season for season in seasons if normalize_entity_name(season.name) == normalized]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise AmbiguousEntityError(f"La temporada '{name}' es ambigua")
    entity = models.Season(
        external_id=_manual_id("season"),
        competition_id=competition.id,
        name=name.strip(),
        data_source="manual",
        source_updated_at=datetime.now(UTC),
        is_mock_data=is_mock_data,
    )
    db.add(entity)
    db.flush()
    return entity


def _resolve_team(db: Session, name: str, is_mock_data: bool) -> models.Team:
    normalized = normalize_entity_name(name)
    teams = list(
        db.scalars(select(models.Team).where(models.Team.is_mock_data == is_mock_data)).all()
    )
    exact_ids = {team.id for team in teams if normalize_entity_name(team.name) == normalized}
    exact_ids.update(
        db.scalars(
            select(models.TeamAlias.team_id)
            .join(models.Team, models.Team.id == models.TeamAlias.team_id)
            .where(
                models.TeamAlias.normalized_alias == normalized,
                models.Team.is_mock_data == is_mock_data,
            )
        ).all()
    )
    if len(exact_ids) == 1:
        return db.get(models.Team, exact_ids.pop())  # type: ignore[return-value]
    if len(exact_ids) > 1:
        raise AmbiguousEntityError(f"El equipo '{name}' coincide con varias entidades")
    resolution = resolve_name(name, ((team.id, team.name) for team in teams))
    if resolution.entity_id is not None:
        return db.get(models.Team, resolution.entity_id)  # type: ignore[return-value]
    if resolution.status == "manual_review":
        db.add(
            models.EntityResolutionConflict(
                entity_type="team",
                provider="manual",
                source_name=name,
                normalized_name=normalized,
                candidate_ids=[item.entity_id for item in resolution.candidates],
                best_score=resolution.score,
            )
        )
        raise AmbiguousEntityError(
            f"El equipo '{name}' es parecido a otro existente y requiere revisión manual",
            entity_type="team",
            source_name=name,
            candidate_ids=[item.entity_id for item in resolution.candidates],
            best_score=resolution.score,
        )
    entity = models.Team(
        external_id=_manual_id("team"),
        name=name.strip(),
        data_source="manual",
        source_updated_at=datetime.now(UTC),
        manual_last_updated_at=datetime.now(UTC),
        is_mock_data=is_mock_data,
    )
    db.add(entity)
    db.flush()
    db.add(
        models.TeamAlias(
            team_id=entity.id,
            provider="manual",
            alias=name.strip(),
            normalized_alias=normalized,
        )
    )
    return entity


def _resolve_player(
    db: Session,
    name: str,
    team: models.Team,
    is_mock_data: bool,
    *,
    position: str | None = None,
    expected_minutes: float | None = None,
) -> models.Player:
    normalized = normalize_entity_name(name)
    players = list(
        db.scalars(
            select(models.Player).where(
                models.Player.current_team_id == team.id,
                models.Player.is_mock_data == is_mock_data,
            )
        ).all()
    )
    exact_ids = {
        player.id for player in players if normalize_entity_name(player.name) == normalized
    }
    player_ids = [player.id for player in players]
    if player_ids:
        exact_ids.update(
            db.scalars(
                select(models.PlayerAlias.player_id).where(
                    models.PlayerAlias.player_id.in_(player_ids),
                    models.PlayerAlias.normalized_alias == normalized,
                )
            ).all()
        )
    if len(exact_ids) == 1:
        return db.get(models.Player, exact_ids.pop())  # type: ignore[return-value]
    if len(exact_ids) > 1:
        raise AmbiguousEntityError(f"El jugador '{name}' coincide con varias entidades")
    resolution = resolve_name(name, ((player.id, player.name) for player in players))
    if resolution.entity_id is not None:
        return db.get(models.Player, resolution.entity_id)  # type: ignore[return-value]
    if resolution.status == "manual_review":
        db.add(
            models.EntityResolutionConflict(
                entity_type="player",
                provider="manual",
                source_name=name,
                normalized_name=normalized,
                candidate_ids=[item.entity_id for item in resolution.candidates],
                best_score=resolution.score,
            )
        )
        raise AmbiguousEntityError(
            f"El jugador '{name}' es parecido a otro existente y requiere revisión manual",
            entity_type="player",
            source_name=name,
            candidate_ids=[item.entity_id for item in resolution.candidates],
            best_score=resolution.score,
        )
    entity = models.Player(
        external_id=_manual_id("player"),
        current_team_id=team.id,
        name=name.strip(),
        primary_position=position,
        expected_minutes=expected_minutes,
        active=True,
        data_source="manual",
        source_updated_at=datetime.now(UTC),
        is_mock_data=is_mock_data,
    )
    db.add(entity)
    db.flush()
    db.add(
        models.PlayerAlias(
            player_id=entity.id,
            provider="manual",
            alias=name.strip(),
            normalized_alias=normalized,
        )
    )
    return entity


def _resolve_referee(db: Session, name: str | None, is_mock_data: bool) -> models.Referee | None:
    if not name:
        return None
    normalized = normalize_entity_name(name)
    referees = list(
        db.scalars(
            select(models.Referee).where(models.Referee.is_mock_data == is_mock_data)
        ).all()
    )
    exact = [item for item in referees if normalize_entity_name(item.name) == normalized]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise AmbiguousEntityError(f"El árbitro '{name}' es ambiguo")
    resolution = resolve_name(name, ((item.id, item.name) for item in referees))
    if resolution.entity_id is not None:
        return db.get(models.Referee, resolution.entity_id)
    if resolution.status == "manual_review":
        raise AmbiguousEntityError(f"El árbitro '{name}' requiere revisión manual")
    entity = models.Referee(
        external_id=_manual_id("referee"),
        name=name.strip(),
        data_source="manual",
        source_updated_at=datetime.now(UTC),
        is_mock_data=is_mock_data,
    )
    db.add(entity)
    db.flush()
    return entity


def _side_team(value: str, home: models.Team, away: models.Team) -> models.Team:
    normalized = normalize_entity_name(value)
    if normalized in {"home", "local", normalize_entity_name(home.name)}:
        return home
    if normalized in {"away", "visitor", "visitante", normalize_entity_name(away.name)}:
        return away
    raise ValueError(f"El selector de equipo '{value}' no corresponde al local ni visitante")


def create_manual_match(
    db: Session, payload: schemas.ManualMatchCreate
) -> models.Match:
    try:
        match_date = payload.match_date
        normalized_date = (
            match_date.replace(tzinfo=UTC)
            if match_date.tzinfo is None
            else match_date.astimezone(UTC)
        )
        if normalized_date <= datetime.now(UTC):
            raise ValueError("La fecha del próximo partido debe estar en el futuro")
        competition = _resolve_competition(db, payload.competition, payload.is_mock_data)
        season = _resolve_season(db, competition, payload.season, payload.is_mock_data)
        home = _resolve_team(db, payload.home_team, payload.is_mock_data)
        away = _resolve_team(db, payload.away_team, payload.is_mock_data)
        if home.id == away.id:
            raise AmbiguousEntityError("Los nombres resuelven al mismo equipo interno")
        referee = _resolve_referee(db, payload.referee, payload.is_mock_data)
        now = datetime.now(UTC)
        match = models.Match(
            external_id=_manual_id("match"),
            competition_id=competition.id,
            season_id=season.id,
            home_team_id=home.id,
            away_team_id=away.id,
            match_date=payload.match_date,
            venue=payload.venue,
            referee_id=referee.id if referee else None,
            round_name=payload.round_name,
            weather=payload.weather,
            importance=payload.importance,
            notes=payload.notes,
            status="scheduled",
            data_source="manual",
            source_updated_at=now,
            is_mock_data=payload.is_mock_data,
        )
        db.add(match)
        db.flush()
        seen_players: set[int] = set()
        starters: dict[int, int] = {home.id: 0, away.id: 0}
        for entry in payload.lineups:
            team = _side_team(entry.team, home, away)
            player = _resolve_player(
                db,
                entry.player_name,
                team,
                payload.is_mock_data,
                position=entry.position,
                expected_minutes=entry.expected_minutes,
            )
            if player.id in seen_players:
                raise ValueError(f"El jugador '{entry.player_name}' está repetido")
            seen_players.add(player.id)
            starters[team.id] += int(entry.started)
            if starters[team.id] > 11:
                raise ValueError("Un equipo no puede tener más de 11 titulares")
            db.add(
                models.Lineup(
                    match_id=match.id,
                    team_id=team.id,
                    player_id=player.id,
                    started=entry.started,
                    confirmed=entry.confirmed,
                    position=entry.position,
                    shirt_number=entry.shirt_number,
                    expected_minutes=entry.expected_minutes,
                    data_source="manual",
                    source_updated_at=now,
                    is_mock_data=payload.is_mock_data,
                )
            )
        for entry in payload.injuries:
            team = _side_team(entry.team, home, away)
            player = _resolve_player(db, entry.player_name, team, payload.is_mock_data)
            db.add(
                models.Injury(
                    player_id=player.id,
                    team_id=team.id,
                    description=entry.description,
                    start_date=entry.start_date,
                    expected_return_date=entry.end_date,
                    active=True,
                    data_source="manual",
                    source_updated_at=now,
                    is_mock_data=payload.is_mock_data,
                )
            )
        for entry in payload.suspensions:
            team = _side_team(entry.team, home, away)
            player = _resolve_player(db, entry.player_name, team, payload.is_mock_data)
            db.add(
                models.Suspension(
                    player_id=player.id,
                    team_id=team.id,
                    reason=entry.reason or entry.description,
                    start_date=entry.start_date,
                    end_date=entry.end_date,
                    active=True,
                    data_source="manual",
                    source_updated_at=now,
                    is_mock_data=payload.is_mock_data,
                )
            )
        db.commit()
        loaded = repositories.load_match(db, match.id)
        assert loaded is not None
        return loaded
    except Exception:
        db.rollback()
        raise
