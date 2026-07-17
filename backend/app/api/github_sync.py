"""Import and train from OpenFootball files already available on disk.

The historic route name is kept for frontend compatibility.  This endpoint never
clones, fetches or calls GitHub: a remote source must be configured explicitly
before that capability is added.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.db import get_db
from app.ml.features import FeatureBuilder
from app.ml.training import (
    DependencyUnavailableError,
    TrainingError,
    save_model as save_poisson_model,
    train_evaluate_poisson,
)
from app.ml.xgboost_model import train_evaluate_xgboost
from app.services.model_inputs import database_match_records
from app.services.openfootball_imports import (
    OpenFootballImportError,
    OpenFootballPersistenceError,
    confirm_openfootball_import,
    preview_openfootball_path,
)


router = APIRouter(prefix="/github-sync", tags=["local-data-sync"])

MIN_TRAINING_RECORDS = 10
MIN_XGBOOST_ACTIVATION_ROWS = 30
OPENFOOTBALL_EXTENSIONS = frozenset({".json", ".txt"})


class SyncStatus(BaseModel):
    matches: int
    teams: int
    competitions: int
    models_trained: int


class TrainedModel(BaseModel):
    name: str
    version: str
    status: str
    validation_rows: int
    metrics: dict[str, Any]


class SyncResult(BaseModel):
    status: str
    imported_folders: list[str] = Field(default_factory=list)
    features_built: int = 0
    poisson_metrics: dict[str, Any] | None = None
    xgboost_metrics: dict[str, Any] | None = None
    models: list[TrainedModel] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _has_match_source(path: Path) -> bool:
    """Avoid creating import runs for placeholder-only folders."""

    return any(
        candidate.is_file()
        and candidate.suffix.casefold() in OPENFOOTBALL_EXTENSIONS
        and not candidate.name.startswith(".")
        for candidate in path.rglob("*")
    )


def _matches_found(run: models.DataIngestionRun) -> int:
    raw = (run.import_metrics or {}).get("matches_found", 0)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _write_feature_records(records: list[dict[str, Any]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(records, default=str, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _train_and_register_model(
    db: Session,
    model_type: str,
    records: list[dict[str, Any]],
    version: str,
) -> tuple[models.ModelVersion, dict[str, Any], int]:
    if model_type == "poisson":
        report = train_evaluate_poisson(records, model_version=version)
        filename = f"goals-poisson-regression-{version}.joblib"
    elif model_type == "xgboost":
        report = train_evaluate_xgboost(records, model_version=version)
        filename = f"goals-xgboost-{version}.joblib"
    else:  # pragma: no cover - protected by the endpoint call sites
        raise ValueError("Invalid model type")

    destination = get_settings().model_dir / filename
    artifact = save_poisson_model(report.model, destination)
    metadata = report.model.metadata
    existing = db.scalar(
        select(models.ModelVersion).where(
            models.ModelVersion.name == metadata.model_name,
            models.ModelVersion.version == metadata.model_version,
        )
    )
    if existing is not None:
        raise TrainingError("La versión del modelo ya existe; no se sobrescribe")

    model = models.ModelVersion(
        name=metadata.model_name,
        version=metadata.model_version,
        trained_at=datetime.fromisoformat(metadata.trained_at),
        max_data_date=datetime.fromisoformat(metadata.max_training_date),
        features=list(metadata.feature_names),
        hyperparameters=metadata.hyperparameters,
        data_sources=[metadata.data_source],
        dataset_hash=metadata.dataset_hash,
        artifact_path=str(artifact.resolve()),
        status="candidate",
        is_mock_data=any(bool(row.get("is_mock_data")) for row in records),
    )
    db.add(model)
    db.flush()
    for scope, values in report.metrics.items():
        if not isinstance(values, dict):
            continue
        for name, value in values.items():
            db.add(
                models.ModelMetric(
                    model_version_id=model.id,
                    metric_name=name,
                    value=float(value),
                    split="validation",
                    scope=scope,
                    sample_size=report.validation_rows,
                )
            )
    db.commit()
    db.refresh(model)
    return model, report.metrics, report.validation_rows


def _combined_mae(metrics: dict[str, Any] | None) -> float | None:
    if not isinstance(metrics, dict):
        return None
    combined = metrics.get("combined")
    if not isinstance(combined, dict):
        return None
    try:
        return float(combined["mae"])
    except (KeyError, TypeError, ValueError):
        return None


def _activate_xgboost_if_qualified(
    db: Session,
    *,
    model: models.ModelVersion,
    training_rows: int,
    poisson_metrics: dict[str, Any] | None,
    xgboost_metrics: dict[str, Any],
) -> str:
    """Promote only a sufficiently sized model that beats its same-split baseline."""

    poisson_mae = _combined_mae(poisson_metrics)
    xgboost_mae = _combined_mae(xgboost_metrics)
    if (
        training_rows < MIN_XGBOOST_ACTIVATION_ROWS
        or poisson_mae is None
        or xgboost_mae is None
        or xgboost_mae >= poisson_mae
    ):
        return model.status

    previous = list(
        db.scalars(
            select(models.ModelVersion).where(
                models.ModelVersion.name == model.name,
                models.ModelVersion.status == "active",
                models.ModelVersion.is_mock_data == model.is_mock_data,
            )
        ).all()
    )
    for active in previous:
        active.status = "superseded"
    model.status = "active"
    db.commit()
    return model.status


def _model_summary(
    model: models.ModelVersion, metrics: dict[str, Any], validation_rows: int
) -> TrainedModel:
    return TrainedModel(
        name=model.name,
        version=model.version,
        status=model.status,
        validation_rows=validation_rows,
        metrics=metrics,
    )


@router.post("/load-local-data", response_model=SyncResult)
def load_local_data(db: Session = Depends(get_db)) -> SyncResult:
    """Import local OpenFootball files, build real-data features and train candidates."""

    settings = get_settings()
    base_dir = (settings.data_root / "external" / "openfootball").expanduser().resolve()
    imported: list[str] = []
    warnings: list[str] = []
    if not base_dir.is_dir():
        warnings.append(
            "No existe data/external/openfootball en el DATA_ROOT configurado; no se descargó nada."
        )
    else:
        candidates = [
            path
            for path in sorted(base_dir.iterdir(), key=lambda item: item.name.casefold())
            if path.is_dir() and not path.name.startswith(".") and _has_match_source(path)
        ]
        if not candidates:
            warnings.append("No hay archivos .json o .txt de OpenFootball para importar.")
        for path in candidates:
            try:
                run = preview_openfootball_path(
                    db,
                    path,
                    competition=None,
                    season=None,
                    preview_limit=10,
                )
                if _matches_found(run) == 0:
                    warnings.append(f"{path.name}: no contiene partidos importables.")
                    continue
                confirmed = confirm_openfootball_import(db, run)
                if confirmed.valid_records:
                    imported.append(path.name)
                else:
                    warnings.append(f"{path.name}: no se persistieron registros válidos.")
            except (OpenFootballImportError, OpenFootballPersistenceError, OSError, ValueError) as exc:
                warnings.append(f"{path.name}: {exc}")

    # Never train a real model from seeded/demo rows just because the local source is empty.
    records = database_match_records(db, is_mock_data=False)
    if not records:
        return SyncResult(
            status="empty",
            imported_folders=imported,
            warnings=[
                *warnings,
                "No hay partidos reales disponibles. Añade archivos locales antes de entrenar.",
            ],
        )

    features = FeatureBuilder().build(records)
    _write_feature_records(features, settings.data_root / "processed" / "features.json")
    train_records = [
        row
        for row in features
        if row.get("target_home_goals") is not None
        and row.get("target_away_goals") is not None
    ]
    if len(train_records) < MIN_TRAINING_RECORDS:
        return SyncResult(
            status="partial",
            imported_folders=imported,
            features_built=len(features),
            warnings=[
                *warnings,
                f"Se necesitan al menos {MIN_TRAINING_RECORDS} partidos finalizados; hay {len(train_records)}.",
            ],
        )

    version = f"1.0.{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
    poisson_metrics: dict[str, Any] | None = None
    xgboost_metrics: dict[str, Any] | None = None
    trained: list[TrainedModel] = []
    try:
        poisson_model, poisson_metrics, poisson_rows = _train_and_register_model(
            db, "poisson", train_records, version
        )
        trained.append(_model_summary(poisson_model, poisson_metrics, poisson_rows))
    except (DependencyUnavailableError, TrainingError, OSError, ValueError) as exc:
        warnings.append(f"Poisson no se entrenó: {exc}")

    try:
        xgboost_model, xgboost_metrics, xgboost_rows = _train_and_register_model(
            db, "xgboost", train_records, version
        )
        _activate_xgboost_if_qualified(
            db,
            model=xgboost_model,
            training_rows=len(train_records),
            poisson_metrics=poisson_metrics,
            xgboost_metrics=xgboost_metrics,
        )
        trained.append(_model_summary(xgboost_model, xgboost_metrics, xgboost_rows))
        if xgboost_model.status != "active":
            warnings.append(
                "XGBoost quedó como candidato: requiere al menos 30 partidos y un MAE menor que Poisson en la misma validación."
            )
    except (DependencyUnavailableError, TrainingError, OSError, ValueError) as exc:
        warnings.append(f"XGBoost no se entrenó: {exc}")

    return SyncResult(
        status="success" if trained and not warnings else "partial",
        imported_folders=imported,
        features_built=len(features),
        poisson_metrics=poisson_metrics,
        xgboost_metrics=xgboost_metrics,
        models=trained,
        warnings=warnings,
    )


@router.get("/status", response_model=SyncStatus)
def get_sync_status(db: Session = Depends(get_db)) -> SyncStatus:
    """Return quick local database counts for the data source panel."""

    matches = db.scalar(select(func.count()).select_from(models.Match)) or 0
    teams = db.scalar(select(func.count()).select_from(models.Team)) or 0
    competitions = db.scalar(select(func.count()).select_from(models.Competition)) or 0
    models_trained = db.scalar(select(func.count()).select_from(models.ModelVersion)) or 0
    return SyncStatus(
        matches=matches,
        teams=teams,
        competitions=competitions,
        models_trained=models_trained,
    )
