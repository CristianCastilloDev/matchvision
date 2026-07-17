from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Select, delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import models, schemas
from app.services.entity_resolution import normalize_entity_name


class ConflictError(ValueError):
    pass


def get_or_404(db: Session, model: type[models.Base], entity_id: object):
    return db.get(model, entity_id)


def create_competition(db: Session, payload: schemas.CompetitionCreate) -> models.Competition:
    entity = models.Competition(**payload.model_dump())
    db.add(entity)
    db.commit()
    db.refresh(entity)
    return entity


def create_season(db: Session, competition_id: int, payload: schemas.SeasonCreate) -> models.Season:
    entity = models.Season(competition_id=competition_id, **payload.model_dump())
    db.add(entity)
    db.commit()
    db.refresh(entity)
    return entity


def create_team(db: Session, payload: schemas.TeamCreate) -> models.Team:
    values = payload.model_dump(exclude={"aliases"})
    values["manual_last_updated_at"] = datetime.now(UTC)
    team = models.Team(**values)
    db.add(team)
    db.flush()
    aliases = {payload.name, *payload.aliases}
    for alias in aliases:
        db.add(
            models.TeamAlias(
                team_id=team.id,
                provider=payload.data_source,
                alias=alias,
                normalized_alias=normalize_entity_name(alias),
            )
        )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("Ya existe un equipo o alias equivalente para este origen") from exc
    db.refresh(team)
    return team


def update_team(db: Session, team: models.Team, payload: schemas.TeamUpdate) -> models.Team:
    values = payload.model_dump(exclude_unset=True, exclude={"active_aliases"})
    for field, value in values.items():
        setattr(team, field, value)
    team.manual_last_updated_at = datetime.now(UTC)
    if payload.active_aliases is not None:
        db.execute(delete(models.TeamAlias).where(models.TeamAlias.team_id == team.id))
        for alias in {team.name, *payload.active_aliases}:
            db.add(
                models.TeamAlias(
                    team_id=team.id,
                    provider=team.data_source,
                    alias=alias,
                    normalized_alias=normalize_entity_name(alias),
                )
            )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("El nombre o alias entra en conflicto con otro equipo") from exc
    db.refresh(team)
    return team


def create_player(db: Session, payload: schemas.PlayerCreate) -> models.Player:
    values = payload.model_dump(exclude={"aliases"})
    player = models.Player(**values)
    db.add(player)
    db.flush()
    for alias in {payload.name, *payload.aliases}:
        db.add(
            models.PlayerAlias(
                player_id=player.id,
                provider=payload.data_source,
                alias=alias,
                normalized_alias=normalize_entity_name(alias),
            )
        )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("Ya existe un jugador o alias equivalente para este origen") from exc
    db.refresh(player)
    return player


def update_player(db: Session, player: models.Player, payload: schemas.PlayerUpdate) -> models.Player:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(player, field, value)
    db.commit()
    db.refresh(player)
    return player


def validate_match_references(db: Session, payload: schemas.MatchCreate | schemas.MatchUpdate) -> None:
    data = payload.model_dump(exclude_unset=True)
    for field, model in (
        ("competition_id", models.Competition),
        ("season_id", models.Season),
        ("home_team_id", models.Team),
        ("away_team_id", models.Team),
    ):
        value = data.get(field)
        if value is not None and db.get(model, value) is None:
            raise ValueError(f"{field}={value} no existe")
    if data.get("competition_id") and data.get("season_id"):
        season = db.get(models.Season, data["season_id"])
        if season and season.competition_id != data["competition_id"]:
            raise ValueError("La temporada no pertenece a la competición indicada")


def create_match(db: Session, payload: schemas.MatchCreate) -> models.Match:
    validate_match_references(db, payload)
    match_date = payload.match_date
    if match_date.tzinfo is None:
        match_date = match_date.replace(tzinfo=UTC)
    else:
        match_date = match_date.astimezone(UTC)
    if match_date <= datetime.now(UTC):
        raise ValueError("La fecha del próximo partido debe estar en el futuro")
    values = payload.model_dump()
    values["status"] = "scheduled"
    values["source_updated_at"] = datetime.now(UTC)
    if not values.get("external_id"):
        values["external_id"] = None
    match = models.Match(**values)
    db.add(match)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("El partido ya existe o sus datos son inconsistentes") from exc
    return load_match(db, match.id)


def update_match(db: Session, match: models.Match, payload: schemas.MatchUpdate) -> models.Match:
    validate_match_references(db, payload)
    values = payload.model_dump(exclude_unset=True)
    if "match_date" in values:
        match_date = values["match_date"]
        normalized_date = (
            match_date.replace(tzinfo=UTC)
            if match_date.tzinfo is None
            else match_date.astimezone(UTC)
        )
        if normalized_date <= datetime.now(UTC):
            raise ValueError("La fecha del próximo partido debe estar en el futuro")
    home_id = values.get("home_team_id", match.home_team_id)
    away_id = values.get("away_team_id", match.away_team_id)
    if home_id == away_id:
        raise ValueError("Los equipos local y visitante deben ser distintos")
    competition_id = values.get("competition_id", match.competition_id)
    season_id = values.get("season_id", match.season_id)
    season = db.get(models.Season, season_id)
    if season is None or season.competition_id != competition_id:
        raise ValueError("La temporada no pertenece a la competición indicada")
    for field, value in values.items():
        setattr(match, field, value)
    match.source_updated_at = datetime.now(UTC)
    db.commit()
    return load_match(db, match.id)


def match_query() -> Select[tuple[models.Match]]:
    return select(models.Match).options(
        selectinload(models.Match.competition),
        selectinload(models.Match.season),
        selectinload(models.Match.home_team),
        selectinload(models.Match.away_team),
    )


def load_match(db: Session, match_id: int) -> models.Match | None:
    return db.scalar(match_query().where(models.Match.id == match_id))


def serialize_match(match: models.Match) -> schemas.MatchOut:
    return schemas.MatchOut(
        id=match.id,
        external_id=match.external_id,
        competition={"id": match.competition.id, "name": match.competition.name},
        season={"id": match.season.id, "name": match.season.name},
        home_team={"id": match.home_team.id, "name": match.home_team.name},
        away_team={"id": match.away_team.id, "name": match.away_team.name},
        match_date=match.match_date,
        venue=match.venue,
        round_name=match.round_name,
        weather=match.weather,
        importance=match.importance,
        notes=match.notes,
        result_details=match.result_details,
        status=match.status,
        home_score=match.home_score,
        away_score=match.away_score,
        halftime_home_score=match.halftime_home_score,
        halftime_away_score=match.halftime_away_score,
        data_source=match.data_source,
        source_updated_at=match.source_updated_at,
        is_mock_data=match.is_mock_data,
    )


def list_matches(
    db: Session,
    *,
    upcoming: bool = False,
    competition_id: int | None = None,
    status_filter: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    offset: int = 0,
    limit: int = 50,
) -> list[models.Match]:
    stmt = match_query()
    if upcoming:
        stmt = stmt.where(models.Match.status.in_(["scheduled", "postponed"]))
        stmt = stmt.order_by(models.Match.match_date)
        if date_from is None:
            stmt = stmt.where(models.Match.match_date >= datetime.now(UTC))
    else:
        stmt = stmt.order_by(models.Match.match_date.desc())
    if status_filter:
        stmt = stmt.where(models.Match.status == status_filter)
    if competition_id:
        stmt = stmt.where(models.Match.competition_id == competition_id)
    if date_from:
        stmt = stmt.where(models.Match.match_date >= date_from)
    if date_to:
        stmt = stmt.where(models.Match.match_date <= date_to)
    return list(db.scalars(stmt.offset(offset).limit(limit)).all())


def historical_team_matches(
    db: Session,
    team_id: int,
    before: datetime,
    *,
    is_mock_data: bool | None = None,
) -> list[models.Match]:
    stmt = (
        match_query()
        .where(
            models.Match.status == "finished",
            models.Match.match_date < before,
            or_(models.Match.home_team_id == team_id, models.Match.away_team_id == team_id),
            models.Match.home_score.is_not(None),
            models.Match.away_score.is_not(None),
        )
        .order_by(models.Match.match_date.desc())
    )
    if is_mock_data is not None:
        stmt = stmt.where(models.Match.is_mock_data == is_mock_data)
    return list(db.scalars(stmt).all())
