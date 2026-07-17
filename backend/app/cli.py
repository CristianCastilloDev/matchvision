from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import typer
from sqlalchemy import select

from app import models
from app.config import get_settings
from app.data_sources.openfootball.importer import (
    OpenFootballImportError,
    import_openfootball_repository,
    validate_openfootball_path,
)
from app.db import SessionLocal, create_schema
from app.ml.evaluation import evaluate_counts
from app.ml.features import FeatureBuilder
from app.ml.training import save_model, train_evaluate_poisson
from app.providers.football_data import FootballDataLocalProvider
from app.providers.statsbomb import StatsBombLocalProvider
from app.schemas import PredictionRequest
from app.services.local_imports import (
    create_import_from_records,
    persist_match_rows,
)
from app.services.openfootball_imports import (
    OpenFootballPersistenceError,
    _run_envelope,
    confirm_openfootball_import,
    preview_openfootball_path,
)
from app.services.openfootball_catalogs import discover_openfootball_catalogs
from app.services.model_inputs import database_match_records
from app.services.predictions import generate_prediction


app = typer.Typer(
    name="matchvision",
    no_args_is_help=True,
    help="Herramientas offline de datos y modelos de MatchVision AI.",
)


def _ensure_db() -> None:
    create_schema()


def _json_output(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _database_match_records() -> list[dict[str, Any]]:
    with SessionLocal() as db:
        return database_match_records(db)


def _write_records(records: list[dict[str, Any]], output: Path) -> None:
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    suffix = output.suffix.casefold()
    if suffix == ".csv":
        pd.DataFrame(records).to_csv(output, index=False)
    elif suffix == ".jsonl":
        output.write_text(
            "".join(json.dumps(row, default=str, ensure_ascii=False) + "\n" for row in records),
            encoding="utf-8",
        )
    elif suffix == ".json":
        output.write_text(json.dumps(records, default=str, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        raise typer.BadParameter("La salida debe terminar en .csv, .json o .jsonl")


def _read_records(path: Path) -> list[dict[str, Any]]:
    resolved = path.expanduser().resolve(strict=True)
    if resolved.suffix.casefold() == ".csv":
        frame = pd.read_csv(resolved)
        return frame.where(pd.notna(frame), None).to_dict(orient="records")
    if resolved.suffix.casefold() == ".jsonl":
        return [json.loads(line) for line in resolved.read_text(encoding="utf-8").splitlines() if line.strip()]
    if resolved.suffix.casefold() == ".json":
        value = json.loads(resolved.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise typer.BadParameter("El JSON debe contener una lista")
        return [dict(row) for row in value]
    raise typer.BadParameter("El archivo debe ser .csv, .json o .jsonl")


@app.command("import-football-data")
def import_football_data(
    file: Path = typer.Option(..., "--file", exists=True, dir_okay=False, readable=True),
    competition: str = typer.Option(..., "--competition"),
    season: str = typer.Option(..., "--season"),
) -> None:
    """Importa un CSV/ZIP local; nunca descarga ni accede a internet."""

    _ensure_db()
    provider = FootballDataLocalProvider()
    dataset = provider.import_file(file, competition=competition, season=season)
    with SessionLocal() as db:
        resolved = file.expanduser().resolve(strict=True)
        run = create_import_from_records(
            db,
            filename=resolved.name,
            content=resolved.read_bytes(),
            records=list(dataset.records),
            competition=competition,
            season=season,
            source="football_data_local_file",
        )
        if dataset.warnings:
            run.errors = [*run.errors, *(
                {"row": None, "code": "column_warning", "severity": "warning", "message": message}
                for message in dataset.warnings
            )]
            db.commit()
        _json_output(
            {
                "run_id": run.id,
                "status": run.status,
                "records": run.valid_records,
                "rejected": run.rejected_records + dataset.rejected_rows,
                "columns": dataset.column_report.to_dict(),
                "offline": True,
            }
        )


@app.command("ingest-football-data", hidden=True)
def ingest_football_data_alias(
    file: Path = typer.Option(..., "--file", exists=True, dir_okay=False, readable=True),
    competition: str = typer.Option(..., "--competition"),
    season: str = typer.Option(..., "--season"),
) -> None:
    import_football_data(file=file, competition=competition, season=season)


@app.command("import-statsbomb")
def import_statsbomb(
    directory: Path = typer.Option(..., "--directory", exists=True, file_okay=False),
    competition_id: int = typer.Option(..., "--competition-id", min=0),
    season_id: int = typer.Option(..., "--season-id", min=0),
    competition: str | None = typer.Option(None, "--competition"),
    season: str | None = typer.Option(None, "--season"),
    include_events: bool = typer.Option(True, "--include-events/--skip-events"),
    include_lineups: bool = typer.Option(True, "--include-lineups/--skip-lineups"),
) -> None:
    """Importa exclusivamente una carpeta local statsbomb/open-data."""

    _ensure_db()
    provider = StatsBombLocalProvider(directory)
    manifest = provider.import_competition(
        competition_id,
        season_id,
        include_events=include_events,
        include_lineups=include_lineups,
    )
    metadata = provider.get_competitions(normalize=True)
    selected = next(
        (
            row
            for row in metadata
            if str(row.get("external_id")) == str(competition_id)
            and str(row.get("season_external_id")) == str(season_id)
        ),
        {},
    )
    competition_name = competition or str(selected.get("name") or f"StatsBomb {competition_id}")
    season_name = season or str(selected.get("season_name") or season_id)
    raw_records = provider.get_matches(competition_id, season_id, normalize=True)
    records = [
        {
            **row,
            "home_goals": row.get("home_score"),
            "away_goals": row.get("away_score"),
            "halftime_home_goals": row.get("halftime_home_score"),
            "halftime_away_goals": row.get("halftime_away_score"),
        }
        for row in raw_records
    ]
    started = datetime.now(UTC)
    with SessionLocal() as db:
        run = models.DataIngestionRun(
            source="statsbomb_local_folder",
            original_filename=directory.name,
            stored_path=str(directory.expanduser().resolve()),
            import_options={
                "competition": competition_name,
                "season": season_name,
                "competition_id": competition_id,
                "season_id": season_id,
            },
            status="running",
            started_at=started,
            pipeline_version="1.0.0",
            is_mock_data=False,
        )
        db.add(run)
        db.commit()
        persist_match_rows(
            db,
            records,
            competition_name=competition_name,
            season_name=season_name,
            run=run,
        )
        run = db.get(models.DataIngestionRun, run.id)
        assert run is not None
        run.status = "completed_with_errors" if run.rejected_records or manifest.errors else "completed"
        run.completed_at = datetime.now(UTC)
        run.duration_seconds = (run.completed_at - started).total_seconds()
        run.errors = [*run.errors, *(
            {"row": None, "code": "statsbomb_artifact", "severity": "error", "message": message}
            for message in manifest.errors
        )]
        db.commit()
        _json_output({"run_id": run.id, "status": run.status, "manifest": asdict(manifest)})


@app.command("ingest-statsbomb", hidden=True)
def ingest_statsbomb_alias(
    directory: Path = typer.Option(..., "--directory", exists=True, file_okay=False),
    competition_id: int = typer.Option(..., "--competition-id"),
    season_id: int = typer.Option(..., "--season-id"),
) -> None:
    import_statsbomb(directory, competition_id, season_id, None, None, True, True)


@app.command("build-features")
def build_features(
    output: Path = typer.Option(Path("data/processed/features.json"), "--output"),
) -> None:
    _ensure_db()
    records = _database_match_records()
    features = FeatureBuilder().build(records)
    _write_records(features, output)
    _json_output({"matches": len(records), "feature_rows": len(features), "output": str(output)})


@app.command("train-goals-model")
def train_goals_model(
    features: Path = typer.Option(..., "--features", exists=True, dir_okay=False),
    version: str = typer.Option("1.0.0", "--version"),
    output: Path | None = typer.Option(None, "--output"),
) -> None:
    _ensure_db()
    all_records = _read_records(features)
    records = [
        row
        for row in all_records
        if row.get("target_home_goals") is not None
        and row.get("target_away_goals") is not None
    ]
    if not records:
        raise typer.BadParameter(
            "No hay partidos terminados con ambos marcadores para entrenar"
        )
    report = train_evaluate_poisson(records, model_version=version)
    destination = output or get_settings().model_dir / f"goals-poisson-regression-{version}.joblib"
    artifact = save_model(report.model, destination)
    metadata = report.model.metadata
    with SessionLocal() as db:
        existing = db.scalar(
            select(models.ModelVersion).where(
                models.ModelVersion.name == metadata.model_name,
                models.ModelVersion.version == metadata.model_version,
            )
        )
        if existing:
            raise typer.BadParameter("La versión ya está registrada; no se reemplaza silenciosamente")
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
    _json_output(
        {
            "artifact": str(artifact),
            "training_rows": len(records),
            "excluded_unfinished_rows": len(all_records) - len(records),
            "validation_rows": report.validation_rows,
            "metrics": report.metrics,
        }
    )


def _train_generic_count_model(records: list[dict[str, Any]], target: str, output: Path) -> dict[str, Any]:
    from joblib import dump
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import PoissonRegressor
    from sklearn.pipeline import make_pipeline

    frame = pd.DataFrame(records).sort_values("match_date")
    if target not in frame or len(frame) < 10:
        raise typer.BadParameter(f"Se requieren al menos 10 filas y la columna {target}")
    forbidden = {target, "match_date", "match_id", "home_score", "away_score", "actual_outcome"}
    columns = [name for name in frame.select_dtypes(include="number").columns if name not in forbidden]
    if not columns:
        raise typer.BadParameter("No hay variables numéricas seguras")
    cutoff = max(3, int(len(frame) * 0.8))
    train, test = frame.iloc[:cutoff], frame.iloc[cutoff:]
    model = make_pipeline(SimpleImputer(strategy="median"), PoissonRegressor(alpha=0.1, max_iter=1000))
    model.fit(train[columns], train[target])
    predicted = model.predict(test[columns])
    metrics = evaluate_counts(test[target], predicted)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise typer.BadParameter("El artefacto ya existe; no se reemplaza silenciosamente")
    dump({"model": model, "features": columns, "target": target, "metrics": metrics}, output)
    return metrics


@app.command("train-cards-model")
def train_cards_model(
    features: Path = typer.Option(..., "--features", exists=True, dir_okay=False),
    output: Path = typer.Option(Path("models/cards-poisson-1.0.0.joblib"), "--output"),
) -> None:
    metrics = _train_generic_count_model(_read_records(features), "target_total_cards", output)
    _json_output({"artifact": str(output), "metrics": metrics})


@app.command("train-scorer-model")
def train_scorer_model(
    features: Path = typer.Option(..., "--features", exists=True, dir_okay=False),
    output: Path = typer.Option(Path("models/scorer-logistic-1.0.0.joblib"), "--output"),
) -> None:
    from joblib import dump
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import brier_score_loss, log_loss
    from sklearn.pipeline import make_pipeline

    frame = pd.DataFrame(_read_records(features)).sort_values("match_date")
    target = "scored"
    if target not in frame or len(frame) < 20 or frame[target].nunique() < 2:
        raise typer.BadParameter("Se requieren 20 filas y ambas clases en 'scored'")
    forbidden = {target, "match_date", "match_id", "player_id", "actual_outcome"}
    columns = [name for name in frame.select_dtypes(include="number").columns if name not in forbidden]
    cutoff = max(10, int(len(frame) * 0.8))
    train, test = frame.iloc[:cutoff], frame.iloc[cutoff:]
    model = make_pipeline(SimpleImputer(strategy="median"), LogisticRegression(max_iter=1000, random_state=42))
    model.fit(train[columns], train[target])
    probabilities = model.predict_proba(test[columns])[:, 1]
    metrics = {
        "brier_score": float(brier_score_loss(test[target], probabilities)),
        "log_loss": float(log_loss(test[target], probabilities, labels=[0, 1])),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise typer.BadParameter("El artefacto ya existe; no se reemplaza silenciosamente")
    dump({"model": model, "features": columns, "target": target, "metrics": metrics}, output)
    _json_output({"artifact": str(output), "metrics": metrics})


@app.command("evaluate-models")
def evaluate_models(
    features: Path | None = typer.Option(None, "--features", exists=True, dir_okay=False),
) -> None:
    if features:
        report = train_evaluate_poisson(_read_records(features))
        _json_output({"temporary_evaluation": True, "metrics": report.metrics})
        return
    _ensure_db()
    with SessionLocal() as db:
        rows = list(db.scalars(select(models.ModelMetric)).all())
    _json_output(
        [
            {"model_version_id": row.model_version_id, "metric": row.metric_name, "value": row.value, "scope": row.scope}
            for row in rows
        ]
    )


@app.command("predict-match")
def predict_match(
    match_id: int = typer.Option(..., "--match-id", min=1),
    use_confirmed_lineups: bool = typer.Option(False, "--use-confirmed-lineups"),
) -> None:
    _ensure_db()
    with SessionLocal() as db:
        response = generate_prediction(
            db,
            PredictionRequest(match_id=match_id, use_confirmed_lineups=use_confirmed_lineups),
        )
    _json_output(response.model_dump(mode="json"))


@app.command("preview-openfootball")
def preview_openfootball_command(
    path: Path = typer.Option(..., "--path", exists=True, readable=True),
    competition: str | None = typer.Option(None, "--competition"),
    season: str | None = typer.Option(None, "--season"),
    limit: int = typer.Option(50, "--limit", min=1, max=200),
) -> None:
    """Detecta y previsualiza un repo/ZIP/JSON/TXT local, sin red ni escrituras DB."""

    try:
        result = import_openfootball_repository(
            path,
            competition=competition,
            season=season,
        )
    except (OpenFootballImportError, OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--path") from exc
    catalogs = discover_openfootball_catalogs(
        path, repository=result.detection.source_repository
    )
    payload = result.to_dict(preview_limit=limit)
    counts = {
        kind: sum(
            len(catalog.records) for catalog in catalogs.catalogs if catalog.kind == kind
        )
        for kind in ("leagues", "clubs", "players")
    }
    payload["metrics"].update(
        {
            "catalog_files_scanned": catalogs.files_scanned,
            "catalog_records_found": catalogs.records_found,
            "catalog_records_imported": 0,
            "leagues_found": counts["leagues"],
            "clubs_found": counts["clubs"],
            "players_found": counts["players"],
        }
    )
    payload["catalogs"] = {
        "files_scanned": catalogs.files_scanned,
        "records_found": catalogs.records_found,
        "leagues_found": counts["leagues"],
        "clubs_found": counts["clubs"],
        "players_found": counts["players"],
        "identity_only": True,
    }
    payload["warnings"] = list(
        dict.fromkeys([*payload["warnings"], *catalogs.warnings])
    )
    _json_output({"status": "previewed", **payload, "offline": True})


@app.command("validate-openfootball")
def validate_openfootball_command(
    path: Path = typer.Option(..., "--path", exists=True, readable=True),
) -> None:
    """Valida estructura y partidos OpenFootball sin descargar contenido."""

    try:
        report = validate_openfootball_path(path)
    except (OpenFootballImportError, OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--path") from exc
    repository = str(report.get("detection", {}).get("source_repository") or "")
    catalogs = discover_openfootball_catalogs(path, repository=repository)
    report["valid"] = bool(
        not report.get("errors")
        and (report.get("metrics", {}).get("matches_found", 0) or catalogs.records_found)
    )
    report.setdefault("metrics", {}).update(
        {
            "catalog_files_scanned": catalogs.files_scanned,
            "catalog_records_found": catalogs.records_found,
        }
    )
    report["catalogs"] = {
        "files_scanned": catalogs.files_scanned,
        "records_found": catalogs.records_found,
        "identity_only": True,
    }
    report["warnings"] = list(
        dict.fromkeys([*report.get("warnings", []), *catalogs.warnings])
    )
    _json_output({**report, "offline": True})
    if not report["valid"]:
        raise typer.Exit(code=1)


@app.command("import-openfootball")
def import_openfootball_command(
    path: Path = typer.Option(..., "--path", exists=True, readable=True),
    competition: str | None = typer.Option(None, "--competition"),
    season: str | None = typer.Option(None, "--season"),
    preview_limit: int = typer.Option(50, "--preview-limit", min=1, max=200),
) -> None:
    """Importa partidos desde una ruta local explícita; nunca usa HTTP ni red."""

    _ensure_db()
    try:
        with SessionLocal() as db:
            run = preview_openfootball_path(
                db,
                path,
                competition=competition,
                season=season,
                preview_limit=preview_limit,
            )
            run = confirm_openfootball_import(db, run)
            _json_output({**_run_envelope(run), "offline": True})
    except (OpenFootballImportError, OpenFootballPersistenceError, OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--path") from exc


if __name__ == "__main__":
    app()
