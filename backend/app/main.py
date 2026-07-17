from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api.router import api_router
from app.config import get_settings
from app.db import SessionLocal, create_schema
from app.logging_config import configure_logging
from app.seed import seed_demo_data


settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    create_schema()
    settings.data_cache_dir.mkdir(parents=True, exist_ok=True)
    settings.import_dir.mkdir(parents=True, exist_ok=True)
    settings.model_dir.mkdir(parents=True, exist_ok=True)
    if settings.auto_seed_demo:
        with SessionLocal() as db:
            demo_match_id = seed_demo_data(db)
        logger.info("database_ready", extra={"demo_match_id": demo_match_id, "offline": True})
    yield


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description=(
        "API educativa y completamente offline para análisis probabilístico de fútbol. "
        "No consume APIs deportivas ni realiza llamadas salientes."
    ),
    lifespan=lifespan,
)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Request-ID"],
)

_requests: dict[str, deque[float]] = defaultdict(deque)


@app.middleware("http")
async def request_context_and_rate_limit(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    bucket = _requests[client]
    while bucket and bucket[0] <= now - 60:
        bucket.popleft()
    if len(bucket) >= settings.rate_limit_per_minute:
        return JSONResponse(
            status_code=429,
            content={"detail": "Límite de solicitudes excedido"},
            headers={"X-Request-ID": request_id, "Retry-After": "60"},
        )
    bucket.append(now)
    started = time.monotonic()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request_completed",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        },
    )
    return response


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    logger.exception(
        "unhandled_exception",
        extra={"request_id": request_id, "path": request.url.path},
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Error interno controlado", "request_id": request_id},
    )


app.include_router(api_router, prefix=settings.api_v1_prefix)
