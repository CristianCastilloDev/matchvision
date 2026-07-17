from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import __version__
from app.config import get_settings
from app.db import get_db
from app.schemas import HealthOut


router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return HealthOut(
        status="ok",
        service=get_settings().app_name,
        version=__version__,
        database="ok",
        mode="offline",
        timestamp=datetime.now(UTC),
    )
