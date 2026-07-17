from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, schemas
from app.config import get_settings
from app.data_sources.openfootball.importer import OpenFootballImportError
from app.db import get_db
from app.services.openfootball_conflicts import (
    OpenFootballConflictError,
    OpenFootballConflictNotFound,
    OpenFootballConflictStateError,
    list_pending_openfootball_conflicts,
    resolve_openfootball_entity_conflict,
    resolve_openfootball_match_conflict,
)
from app.services.openfootball_imports import (
    MAX_PREVIEW_MATCHES,
    OpenFootballPersistenceError,
    _run_envelope,
    confirm_openfootball_import,
    delete_openfootball_import,
    list_openfootball_quality,
    preview_openfootball_uploads,
    reprocess_openfootball_import,
)


router = APIRouter(prefix="/openfootball", tags=["OpenFootball offline"])


def _run_or_404(db: Session, run_id: str) -> models.DataIngestionRun:
    run = db.get(models.DataIngestionRun, run_id)
    if run is None or run.source != "openfootball":
        raise HTTPException(status_code=404, detail="Importación OpenFootball no encontrada")
    return run


@router.post(
    "/preview",
    response_model=schemas.OpenFootballImportOut,
    status_code=status.HTTP_201_CREATED,
)
async def preview_openfootball(
    files: list[UploadFile] = File(..., min_length=1),
    relative_paths: str | None = Form(default=None),
    competition: str | None = Form(default=None, max_length=150),
    season: str | None = Form(default=None, max_length=100),
    preview_limit: int = Form(default=50, ge=1, le=MAX_PREVIEW_MATCHES),
    db: Session = Depends(get_db),
):
    """Preview uploaded files only. Deliberately accepts no server-side path."""

    settings = get_settings()
    parsed_paths: list[str] | None = None
    if relative_paths:
        try:
            decoded = json.loads(relative_paths)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=422, detail="relative_paths debe ser JSON válido") from exc
        if not isinstance(decoded, list) or not all(isinstance(value, str) for value in decoded):
            raise HTTPException(status_code=422, detail="relative_paths debe ser una lista de strings")
        parsed_paths = decoded

    uploads: list[tuple[str, bytes]] = []
    consumed = 0
    try:
        for upload in files:
            remaining = settings.max_import_bytes - consumed
            content = await upload.read(remaining + 1)
            if len(content) > remaining:
                raise HTTPException(status_code=413, detail="La carga agregada supera el límite permitido")
            consumed += len(content)
            uploads.append((upload.filename or "", content))
    finally:
        for upload in files:
            await upload.close()
    try:
        run = preview_openfootball_uploads(
            db,
            uploads=uploads,
            relative_paths=parsed_paths,
            competition=competition,
            season=season,
            preview_limit=preview_limit,
            settings=settings,
        )
    except (OpenFootballPersistenceError, OpenFootballImportError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _run_envelope(run)


@router.get("/imports", response_model=list[schemas.OpenFootballImportOut])
def list_openfootball_imports(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    runs = db.scalars(
        select(models.DataIngestionRun)
        .where(
            models.DataIngestionRun.source == "openfootball",
            models.DataIngestionRun.preview_payload.is_not(None),
        )
        .order_by(models.DataIngestionRun.started_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return [_run_envelope(run) for run in runs]


@router.get("/quality", response_model=list[schemas.OpenFootballQualityOut])
def openfootball_quality(db: Session = Depends(get_db)):
    return list_openfootball_quality(db)


@router.get("/conflicts", response_model=schemas.OpenFootballPendingConflictsOut)
def pending_openfootball_conflicts(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
):
    try:
        return list_pending_openfootball_conflicts(db, offset=offset, limit=limit)
    except OpenFootballConflictStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/conflicts/entities/{conflict_id}/resolve",
    response_model=schemas.OpenFootballEntityConflictResolutionOut,
)
def resolve_entity_conflict(
    conflict_id: int,
    payload: schemas.OpenFootballEntityConflictResolutionRequest,
    db: Session = Depends(get_db),
):
    try:
        return resolve_openfootball_entity_conflict(
            db,
            conflict_id,
            candidate_id=payload.candidate_id,
            notes=payload.notes,
        )
    except OpenFootballConflictNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OpenFootballConflictStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OpenFootballConflictError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/conflicts/matches/{source_record_id}/resolve",
    response_model=schemas.OpenFootballMatchConflictResolutionOut,
)
def resolve_match_conflict(
    source_record_id: int,
    payload: schemas.OpenFootballMatchConflictResolutionRequest,
    db: Session = Depends(get_db),
):
    try:
        return resolve_openfootball_match_conflict(
            db,
            source_record_id,
            decisions=payload.decisions,
            notes=payload.notes,
        )
    except OpenFootballConflictNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OpenFootballConflictStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OpenFootballConflictError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/imports/{run_id}", response_model=schemas.OpenFootballImportOut)
def get_openfootball_import(run_id: str, db: Session = Depends(get_db)):
    return _run_envelope(_run_or_404(db, run_id))


@router.post(
    "/imports/{run_id}/confirm",
    response_model=schemas.OpenFootballImportOut,
)
def confirm_openfootball(run_id: str, db: Session = Depends(get_db)):
    run = _run_or_404(db, run_id)
    try:
        run = confirm_openfootball_import(db, run)
    except (OpenFootballPersistenceError, OpenFootballImportError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _run_envelope(run)


@router.post(
    "/imports/{run_id}/reprocess",
    response_model=schemas.OpenFootballImportOut,
)
def reprocess_openfootball(run_id: str, db: Session = Depends(get_db)):
    run = _run_or_404(db, run_id)
    try:
        run = reprocess_openfootball_import(db, run)
    except (OpenFootballPersistenceError, OpenFootballImportError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _run_envelope(run)


@router.delete("/imports/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_openfootball_import(run_id: str, db: Session = Depends(get_db)):
    run = _run_or_404(db, run_id)
    delete_openfootball_import(db, run)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
