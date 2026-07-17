from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import models, repositories, schemas
from app.db import get_db
from sqlalchemy import select
from app.services.predictions import evaluate_prediction
from app.services.entity_resolution import normalize_entity_name
from app.services.manual_match import AmbiguousEntityError, create_manual_match


router = APIRouter(prefix="/matches", tags=["matches"])


def _match_or_404(db: Session, match_id: int) -> models.Match:
    match = repositories.load_match(db, match_id)
    if match is None:
        raise HTTPException(status_code=404, detail="Partido no encontrado")
    return match


@router.get("", response_model=list[schemas.MatchOut])
def list_all_matches(
    competition_id: int | None = None,
    status: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=5000),
    db: Session = Depends(get_db),
):
    matches = repositories.list_matches(
        db,
        upcoming=False,
        competition_id=competition_id,
        date_from=date_from,
        date_to=date_to,
        status_filter=status,
        offset=offset,
        limit=limit,
    )
    return [repositories.serialize_match(match) for match in matches]


@router.get("/upcoming", response_model=list[schemas.MatchOut])
def upcoming_matches(
    competition_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=5000),
    db: Session = Depends(get_db),
):
    matches = repositories.list_matches(
        db,
        upcoming=True,
        competition_id=competition_id,
        date_from=date_from,
        date_to=date_to,
        offset=offset,
        limit=limit,
    )
    return [repositories.serialize_match(match) for match in matches]


@router.post("", response_model=schemas.MatchOut, status_code=status.HTTP_201_CREATED)
def create_future_match(payload: schemas.MatchCreate, db: Session = Depends(get_db)):
    try:
        match = repositories.create_match(db, payload)
    except (ValueError, repositories.ConflictError) as exc:
        code = 409 if isinstance(exc, repositories.ConflictError) else 422
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    return repositories.serialize_match(match)


@router.post("/manual", response_model=schemas.MatchOut, status_code=status.HTTP_201_CREATED)
def create_future_match_by_name(
    payload: schemas.ManualMatchCreate, db: Session = Depends(get_db)
):
    """Create a fully manual fixture from safe exact/alias entity resolution."""

    try:
        match = create_manual_match(db, payload)
    except AmbiguousEntityError as exc:
        db.add(
            models.EntityResolutionConflict(
                entity_type=exc.entity_type,
                provider="manual",
                source_name=exc.source_name,
                normalized_name=normalize_entity_name(exc.source_name),
                candidate_ids=exc.candidate_ids,
                best_score=exc.best_score,
                status="pending",
                resolution_notes=str(exc),
            )
        )
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return repositories.serialize_match(match)


@router.get("/{match_id}", response_model=schemas.MatchOut)
def get_match(match_id: int, db: Session = Depends(get_db)):
    return repositories.serialize_match(_match_or_404(db, match_id))


@router.patch("/{match_id}", response_model=schemas.MatchOut)
def update_match(
    match_id: int, payload: schemas.MatchUpdate, db: Session = Depends(get_db)
):
    match = _match_or_404(db, match_id)
    if match.status == "finished":
        raise HTTPException(status_code=409, detail="Un partido finalizado no puede editarse")
    structural_fields = {
        "competition_id",
        "season_id",
        "home_team_id",
        "away_team_id",
        "match_date",
    }
    has_prediction = db.scalar(
        select(models.Prediction.id).where(models.Prediction.match_id == match.id).limit(1)
    )
    if has_prediction and payload.model_fields_set & structural_fields:
        raise HTTPException(
            status_code=409,
            detail="No se cambia la estructura de un fixture que ya tiene snapshots de predicción",
        )
    try:
        updated = repositories.update_match(db, match, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return repositories.serialize_match(updated)


@router.delete("/{match_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_match(match_id: int, db: Session = Depends(get_db)):
    match = _match_or_404(db, match_id)
    if match.predictions:
        raise HTTPException(
            status_code=409,
            detail="No se puede eliminar un partido con predicciones; se preservan para auditoría",
        )
    db.delete(match)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{match_id}/lineups", response_model=list[schemas.LineupOut])
def get_lineups(match_id: int, db: Session = Depends(get_db)):
    _match_or_404(db, match_id)
    rows = db.execute(
        select(models.Lineup, models.Player.name)
        .join(models.Player, models.Lineup.player_id == models.Player.id, isouter=True)
        .where(models.Lineup.match_id == match_id)
        .order_by(models.Lineup.team_id, models.Lineup.started.desc(), models.Lineup.id)
    ).all()
    return [
        schemas.LineupOut(
            id=row.Lineup.id,
            match_id=row.Lineup.match_id,
            team_id=row.Lineup.team_id,
            player_id=row.Lineup.player_id,
            player_name=row[1],
            started=row.Lineup.started,
            confirmed=row.Lineup.confirmed,
            position=row.Lineup.position,
            shirt_number=row.Lineup.shirt_number,
            expected_minutes=row.Lineup.expected_minutes,
            data_source=row.Lineup.data_source,
            is_mock_data=row.Lineup.is_mock_data,
        )
        for row in rows
    ]


@router.put("/{match_id}/lineups", response_model=list[schemas.LineupOut])
def replace_lineups(
    match_id: int, payload: schemas.LineupUpsert, db: Session = Depends(get_db)
):
    match = _match_or_404(db, match_id)
    if match.status == "finished":
        raise HTTPException(status_code=409, detail="No se modifica la alineación de un partido finalizado")
    valid_teams = {match.home_team_id, match.away_team_id}
    starters_by_team: dict[int, int] = {team_id: 0 for team_id in valid_teams}
    for entry in payload.entries:
        if entry.team_id not in valid_teams:
            raise HTTPException(status_code=422, detail="La alineación contiene un equipo ajeno al partido")
        player = db.get(models.Player, entry.player_id)
        if player is None:
            raise HTTPException(status_code=422, detail=f"El jugador {entry.player_id} no existe")
        if player.current_team_id != entry.team_id:
            raise HTTPException(
                status_code=422,
                detail=f"El jugador {entry.player_id} no pertenece al equipo {entry.team_id}",
            )
        if entry.started:
            starters_by_team[entry.team_id] += 1
    if any(count > 11 for count in starters_by_team.values()):
        raise HTTPException(status_code=422, detail="Un equipo no puede tener más de 11 titulares")
    db.execute(delete(models.Lineup).where(models.Lineup.match_id == match_id))
    now = datetime.now(UTC)
    for entry in payload.entries:
        db.add(
            models.Lineup(
                match_id=match.id,
                **entry.model_dump(),
                data_source="manual",
                source_updated_at=now,
                is_mock_data=match.is_mock_data,
            )
        )
    db.commit()
    return get_lineups(match_id, db)


@router.delete("/{match_id}/lineups", status_code=status.HTTP_204_NO_CONTENT)
def delete_lineups(match_id: int, db: Session = Depends(get_db)):
    match = _match_or_404(db, match_id)
    if match.status == "finished":
        raise HTTPException(status_code=409, detail="No se modifica un partido finalizado")
    db.execute(delete(models.Lineup).where(models.Lineup.match_id == match_id))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{match_id}/statistics", response_model=list[schemas.TeamStatisticsOut])
def get_statistics(match_id: int, db: Session = Depends(get_db)):
    _match_or_404(db, match_id)
    return list(
        db.scalars(
            select(models.TeamMatchStatistics).where(
                models.TeamMatchStatistics.match_id == match_id
            )
        ).all()
    )


@router.post("/{match_id}/result", response_model=schemas.MatchOut)
def register_result(
    match_id: int, payload: schemas.MatchResultCreate, db: Session = Depends(get_db)
):
    match = _match_or_404(db, match_id)
    if match.status == "finished":
        if match.home_score != payload.home_score or match.away_score != payload.away_score:
            raise HTTPException(
                status_code=409,
                detail="El resultado final ya fue registrado; no se sobrescribe el dato auditado",
            )
    else:
        match.home_score = payload.home_score
        match.away_score = payload.away_score
        match.halftime_home_score = payload.halftime_home_score
        match.halftime_away_score = payload.halftime_away_score
        match.status = payload.status
        match.result_details = payload.details
        match.source_updated_at = datetime.now(UTC)
    if payload.status == "finished":
        predictions = list(
            db.scalars(select(models.Prediction).where(models.Prediction.match_id == match.id)).all()
        )
        try:
            for prediction in predictions:
                evaluate_prediction(
                    db,
                    prediction,
                    home_score=payload.home_score,
                    away_score=payload.away_score,
                    commit=False,
                )
            db.commit()
        except Exception as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"No se registró el resultado: evaluación atómica fallida ({exc})",
            ) from exc
    else:
        db.commit()
    return repositories.serialize_match(_match_or_404(db, match_id))


@router.post("/{match_id}/manual-stats")
def manual_stats(match_id: int, payload: schemas.ManualStatsCreate, db: Session = Depends(get_db)):
    match = _match_or_404(db, match_id)
    now = datetime.now(UTC)

    def upsert_stat(tid: int, data: dict):
        stat = db.scalar(select(models.TeamMatchStatistics).where(
            models.TeamMatchStatistics.match_id == match_id,
            models.TeamMatchStatistics.team_id == tid,
        ))
        if stat:
            for k, v in data.items(): setattr(stat, k, v)
            stat.source_updated_at = now; stat.data_source = "manual"
        else:
            db.add(models.TeamMatchStatistics(
                match_id=match_id, team_id=tid, data_source="manual",
                source_updated_at=now, is_mock_data=False, **data))

    hid, aid = match.home_team_id, match.away_team_id
    sd: dict[int, dict] = {hid: {}, aid: {}}
    if payload.home_corners is not None: sd[hid]["corners"] = payload.home_corners
    if payload.away_corners is not None: sd[aid]["corners"] = payload.away_corners
    if payload.home_yellow_cards is not None: sd[hid]["yellow_cards"] = payload.home_yellow_cards
    if payload.away_yellow_cards is not None: sd[aid]["yellow_cards"] = payload.away_yellow_cards
    if payload.home_red_cards is not None: sd[hid]["red_cards"] = payload.home_red_cards
    if payload.away_red_cards is not None: sd[aid]["red_cards"] = payload.away_red_cards
    if payload.home_shots is not None: sd[hid]["shots"] = payload.home_shots
    if payload.away_shots is not None: sd[aid]["shots"] = payload.away_shots
    for tid, data in sd.items():
        if data: upsert_stat(tid, data)

    def resolve_player(pid: int | None, name: str | None, tid: int) -> int | None:
        if pid:
            return pid
        if name:
            player = db.scalar(select(models.Player).where(models.Player.name.ilike(name)))
            if not player:
                player = models.Player(name=name, data_source="manual", is_mock_data=False)
                db.add(player); db.flush()
            return player.id
        return None

    for s in payload.scorers:
        tid = hid if s.team == "home" else aid
        resolved = resolve_player(s.player_id, s.player_name, tid)
        if resolved is None: continue
        pm = db.scalar(select(models.PlayerMatch).where(
            models.PlayerMatch.match_id == match_id, models.PlayerMatch.player_id == resolved,
        ))
        if pm:
            pm.goals = s.goals
        else:
            db.add(models.PlayerMatch(
                match_id=match_id, player_id=resolved, team_id=tid, goals=s.goals,
                started=False, data_source="manual", is_mock_data=False))

    for c in payload.cards:
        tid = hid if c.team == "home" else aid
        resolved = resolve_player(c.player_id, c.player_name, tid)
        if resolved is None: continue
        pm = db.scalar(select(models.PlayerMatch).where(
            models.PlayerMatch.match_id == match_id, models.PlayerMatch.player_id == resolved,
        ))
        if pm:
            if c.yellow: pm.yellow_cards = (pm.yellow_cards or 0) + 1
            if c.red: pm.red_cards = (pm.red_cards or 0) + 1
        else:
            db.add(models.PlayerMatch(
                match_id=match_id, player_id=resolved, team_id=tid,
                yellow_cards=1 if c.yellow else 0, red_cards=1 if c.red else 0,
                started=False, goals=0, data_source="manual", is_mock_data=False))

    db.commit()
    return {"ok": True, "match_id": match_id}
