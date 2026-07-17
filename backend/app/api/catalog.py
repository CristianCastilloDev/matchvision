from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import models, repositories, schemas
from app.db import get_db


router = APIRouter(tags=["catalog"])


def _not_found(label: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{label} no encontrado")


@router.get("/competitions", response_model=list[schemas.CompetitionOut])
def list_competitions(db: Session = Depends(get_db)):
    return list(db.scalars(select(models.Competition).order_by(models.Competition.name)).all())


@router.post(
    "/competitions", response_model=schemas.CompetitionOut, status_code=status.HTTP_201_CREATED
)
def create_competition(payload: schemas.CompetitionCreate, db: Session = Depends(get_db)):
    try:
        return repositories.create_competition(db, payload)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="La competición ya existe") from exc


@router.get(
    "/competitions/{competition_id}/seasons", response_model=list[schemas.SeasonOut]
)
def list_seasons(competition_id: int, db: Session = Depends(get_db)):
    if db.get(models.Competition, competition_id) is None:
        raise _not_found("Competición")
    return list(
        db.scalars(
            select(models.Season)
            .where(models.Season.competition_id == competition_id)
            .order_by(models.Season.start_date.desc())
        ).all()
    )


@router.get(
    "/competitions/{competition_id}/standings",
    response_model=list[schemas.StandingsEntry],
)
def get_standings(
    competition_id: int,
    season_id: int | None = None,
    db: Session = Depends(get_db),
):
    comp = db.get(models.Competition, competition_id)
    if comp is None:
        raise _not_found("Competición")
    stmt = select(models.Match).where(
        models.Match.competition_id == competition_id,
        models.Match.status == "finished",
        models.Match.home_score.is_not(None),
        models.Match.away_score.is_not(None),
    )
    if season_id:
        stmt = stmt.where(models.Match.season_id == season_id)
    matches = list(db.scalars(
        stmt.options(
            selectinload(models.Match.home_team),
            selectinload(models.Match.away_team),
        )
    ).all())
    if not matches:
        return []
    stats: dict[int, dict] = {}
    for m in matches:
        for team_id in (m.home_team_id, m.away_team_id):
            if team_id not in stats:
                team = m.home_team if m.home_team_id == team_id else m.away_team
                stats[team_id] = {"team_id": team_id, "team_name": team.name, "played": 0, "won": 0, "drawn": 0, "lost": 0, "goals_for": 0, "goals_against": 0, "form": []}
        h, a = m.home_score, m.away_score
        stats[m.home_team_id]["played"] += 1
        stats[m.away_team_id]["played"] += 1
        stats[m.home_team_id]["goals_for"] += h
        stats[m.home_team_id]["goals_against"] += a
        stats[m.away_team_id]["goals_for"] += a
        stats[m.away_team_id]["goals_against"] += h
        if h > a:
            stats[m.home_team_id]["won"] += 1
            stats[m.away_team_id]["lost"] += 1
            stats[m.home_team_id]["form"].append("W")
            stats[m.away_team_id]["form"].append("L")
        elif a > h:
            stats[m.away_team_id]["won"] += 1
            stats[m.home_team_id]["lost"] += 1
            stats[m.away_team_id]["form"].append("W")
            stats[m.home_team_id]["form"].append("L")
        else:
            stats[m.home_team_id]["drawn"] += 1
            stats[m.away_team_id]["drawn"] += 1
            stats[m.home_team_id]["form"].append("D")
            stats[m.away_team_id]["form"].append("D")
    for r in stats.values():
        r["points"] = r["won"] * 3 + r["drawn"]
        r["goal_difference"] = r["goals_for"] - r["goals_against"]
    rows = sorted(stats.values(), key=lambda r: (r["points"], r["goal_difference"], r["goals_for"]), reverse=True)
    return [schemas.StandingsEntry(position=i + 1, **r) for i, r in enumerate(rows)]

@router.post(
    "/competitions/{competition_id}/seasons",
    response_model=schemas.SeasonOut,
    status_code=status.HTTP_201_CREATED,
)
def create_season(
    competition_id: int, payload: schemas.SeasonCreate, db: Session = Depends(get_db)
):
    if db.get(models.Competition, competition_id) is None:
        raise _not_found("Competición")
    try:
        return repositories.create_season(db, competition_id, payload)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="La temporada ya existe") from exc


@router.get("/teams", response_model=list[schemas.TeamOut])
def list_teams(
    search: str | None = Query(default=None, max_length=100),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    stmt = select(models.Team).order_by(models.Team.name)
    if search:
        stmt = stmt.where(models.Team.name.ilike(f"%{search}%"))
    return list(db.scalars(stmt.offset(offset).limit(limit)).all())


@router.post("/teams", response_model=schemas.TeamOut, status_code=status.HTTP_201_CREATED)
def create_team(payload: schemas.TeamCreate, db: Session = Depends(get_db)):
    try:
        return repositories.create_team(db, payload)
    except repositories.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/teams/{team_id}", response_model=schemas.TeamOut)
def get_team(team_id: int, db: Session = Depends(get_db)):
    team = db.get(models.Team, team_id)
    if team is None:
        raise _not_found("Equipo")
    return team


@router.patch("/teams/{team_id}", response_model=schemas.TeamOut)
def update_team(team_id: int, payload: schemas.TeamUpdate, db: Session = Depends(get_db)):
    team = db.get(models.Team, team_id)
    if team is None:
        raise _not_found("Equipo")
    try:
        return repositories.update_team(db, team, payload)
    except repositories.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/teams/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_team(team_id: int, db: Session = Depends(get_db)):
    team = db.get(models.Team, team_id)
    if team is None:
        raise _not_found("Equipo")
    db.delete(team)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="No se puede eliminar un equipo vinculado a partidos o jugadores"
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/players", response_model=list[schemas.PlayerOut])
def list_players(
    team_id: int | None = None,
    search: str | None = Query(default=None, max_length=100),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    stmt = select(models.Player).order_by(models.Player.name)
    if team_id is not None:
        stmt = stmt.where(models.Player.current_team_id == team_id)
    if search:
        stmt = stmt.where(models.Player.name.ilike(f"%{search}%"))
    return list(db.scalars(stmt.offset(offset).limit(limit)).all())


@router.post("/players", response_model=schemas.PlayerOut, status_code=status.HTTP_201_CREATED)
def create_player(payload: schemas.PlayerCreate, db: Session = Depends(get_db)):
    if payload.current_team_id and db.get(models.Team, payload.current_team_id) is None:
        raise _not_found("Equipo")
    try:
        return repositories.create_player(db, payload)
    except repositories.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/players/{player_id}", response_model=schemas.PlayerOut)
def get_player(player_id: int, db: Session = Depends(get_db)):
    player = db.get(models.Player, player_id)
    if player is None:
        raise _not_found("Jugador")
    return player


@router.patch("/players/{player_id}", response_model=schemas.PlayerOut)
def update_player(
    player_id: int, payload: schemas.PlayerUpdate, db: Session = Depends(get_db)
):
    player = db.get(models.Player, player_id)
    if player is None:
        raise _not_found("Jugador")
    if payload.current_team_id and db.get(models.Team, payload.current_team_id) is None:
        raise _not_found("Equipo")
    return repositories.update_player(db, player, payload)


@router.delete("/players/{player_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_player(player_id: int, db: Session = Depends(get_db)):
    player = db.get(models.Player, player_id)
    if player is None:
        raise _not_found("Jugador")
    db.delete(player)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="El jugador tiene datos históricos vinculados") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/availability/injuries", response_model=list[schemas.InjuryOut])
def list_injuries(team_id: int | None = None, db: Session = Depends(get_db)):
    stmt = select(models.Injury).order_by(models.Injury.created_at.desc())
    if team_id:
        stmt = stmt.where(models.Injury.team_id == team_id)
    return list(db.scalars(stmt).all())


@router.post(
    "/availability/injuries", response_model=schemas.InjuryOut, status_code=status.HTTP_201_CREATED
)
def create_injury(payload: schemas.AvailabilityCreate, db: Session = Depends(get_db)):
    player = db.get(models.Player, payload.player_id)
    team = db.get(models.Team, payload.team_id)
    if player is None or team is None:
        raise HTTPException(status_code=404, detail="Equipo o jugador no encontrado")
    if player.current_team_id != team.id:
        raise HTTPException(status_code=422, detail="El jugador no pertenece al equipo")
    injury = models.Injury(
        player_id=payload.player_id,
        team_id=payload.team_id,
        description=payload.description,
        start_date=payload.start_date,
        expected_return_date=payload.end_date,
        active=payload.active,
        data_source="manual",
        source_updated_at=datetime.now(UTC),
        is_mock_data=payload.is_mock_data,
    )
    db.add(injury)
    db.commit()
    db.refresh(injury)
    return injury


@router.get("/availability/suspensions", response_model=list[schemas.SuspensionOut])
def list_suspensions(team_id: int | None = None, db: Session = Depends(get_db)):
    stmt = select(models.Suspension).order_by(models.Suspension.created_at.desc())
    if team_id:
        stmt = stmt.where(models.Suspension.team_id == team_id)
    return list(db.scalars(stmt).all())


@router.post(
    "/availability/suspensions",
    response_model=schemas.SuspensionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_suspension(payload: schemas.AvailabilityCreate, db: Session = Depends(get_db)):
    player = db.get(models.Player, payload.player_id)
    team = db.get(models.Team, payload.team_id)
    if player is None or team is None:
        raise HTTPException(status_code=404, detail="Equipo o jugador no encontrado")
    if player.current_team_id != team.id:
        raise HTTPException(status_code=422, detail="El jugador no pertenece al equipo")
    suspension = models.Suspension(
        player_id=payload.player_id,
        team_id=payload.team_id,
        reason=payload.reason,
        start_date=payload.start_date,
        end_date=payload.end_date,
        active=payload.active,
        data_source="manual",
        source_updated_at=datetime.now(UTC),
        is_mock_data=payload.is_mock_data,
    )
    db.add(suspension)
    db.commit()
    db.refresh(suspension)
    return suspension
