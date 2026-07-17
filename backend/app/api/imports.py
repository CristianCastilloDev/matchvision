from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, schemas
from app.config import get_settings
from app.db import get_db
from app.services.local_imports import (
    CSV_TEMPLATES,
    ImportValidationError,
    create_import_from_bytes,
    delete_import,
    import_match_events,
    import_player_matches,
    reprocess_import,
)


router = APIRouter(prefix="/imports", tags=["local imports"])


@router.get("/templates/{template_name}", response_class=PlainTextResponse)
def download_template(template_name: str):
    content = CSV_TEMPLATES.get(template_name)
    if content is None:
        raise HTTPException(
            status_code=404,
            detail=f"Plantilla desconocida. Disponibles: {', '.join(sorted(CSV_TEMPLATES))}",
        )
    return PlainTextResponse(
        content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="matchvision-{template_name}.csv"'},
    )


@router.post("", response_model=schemas.ImportRunOut, status_code=status.HTTP_201_CREATED)
async def upload_local_import(
    file: UploadFile = File(...),
    competition: str = Form(..., min_length=1, max_length=150),
    season: str = Form(..., min_length=1, max_length=100),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    content = await file.read(settings.max_import_bytes + 1)
    if len(content) > settings.max_import_bytes:
        raise HTTPException(status_code=413, detail="El archivo supera el límite permitido")
    try:
        run = create_import_from_bytes(
            db,
            filename=file.filename or "",
            content=content,
            competition=competition,
            season=season,
            settings=settings,
        )
    except ImportValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return run


@router.get("", response_model=list[schemas.ImportRunOut])
def list_imports(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return list(
        db.scalars(
            select(models.DataIngestionRun)
            .order_by(models.DataIngestionRun.started_at.desc())
            .offset(offset)
            .limit(limit)
        ).all()
    )


@router.get("/{run_id}", response_model=schemas.ImportRunOut)
def get_import(run_id: str, db: Session = Depends(get_db)):
    run = db.get(models.DataIngestionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Importación no encontrada")
    return run


@router.post("/{run_id}/reprocess", response_model=schemas.ImportRunOut)
def reprocess(run_id: str, db: Session = Depends(get_db)):
    run = db.get(models.DataIngestionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Importación no encontrada")
    try:
        return reprocess_import(db, run)
    except ImportValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_import(
    run_id: str,
    delete_records: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    run = db.get(models.DataIngestionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Importación no encontrada")
    try:
        delete_import(db, run, delete_records=delete_records)
    except ImportValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/player-matches", response_model=schemas.PlayerMatchImportOut, status_code=status.HTTP_201_CREATED)
def upload_player_matches(
    body: list[schemas.PlayerMatchImportItem],
    db: Session = Depends(get_db),
):
    result = import_player_matches(db, [item.model_dump() for item in body])
    return result


@router.post("/match-events", response_model=schemas.MatchEventImportOut, status_code=status.HTTP_201_CREATED)
def upload_match_events(
    body: list[schemas.MatchEventImportItem],
    db: Session = Depends(get_db),
):
    result = import_match_events(db, [item.model_dump() for item in body])
    return result
