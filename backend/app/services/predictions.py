from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import models, repositories, schemas
from app.config import get_settings
from app.constants import DEMO_WARNING, DISCLAIMER
from app.ml.features import FeatureBuilder, FeatureEngineeringError
from app.ml.poisson import PoissonPredictor
from app.ml.training import DependencyUnavailableError, TrainingError
from app.ml.xgboost_model import TrainedXGBoostGoalsModel, load_xgboost_model
from app.services.model_inputs import database_match_records


MODEL_NAME = "goals-independent-poisson"
MODEL_VERSION = "1.0.0"


class InsufficientPredictionDataError(ValueError):
    pass


def _load_active_xgboost(
    db: Session, match: models.Match
) -> tuple[TrainedXGBoostGoalsModel | None, list[str]]:
    """Load only a registered active artifact from the configured model directory."""

    registered = db.scalar(
        select(models.ModelVersion)
        .where(
            models.ModelVersion.name == "goals-xgboost",
            models.ModelVersion.status == "active",
            models.ModelVersion.is_mock_data == match.is_mock_data,
        )
        .order_by(models.ModelVersion.trained_at.desc())
    )
    if registered is None:
        return None, []
    if not registered.artifact_path:
        return None, ["El modelo XGBoost activo no tiene un artefacto registrado; se usó el baseline."]

    try:
        source = Path(registered.artifact_path).expanduser()
        if not source.is_absolute():
            raise TrainingError("La ruta del artefacto debe ser absoluta")
        resolved = source.resolve(strict=True)
        model_root = get_settings().model_dir.expanduser().resolve()
        if model_root not in (resolved, *resolved.parents):
            raise TrainingError("El artefacto queda fuera de MODEL_ROOT")
        model = load_xgboost_model(resolved)
        if (
            model.metadata.model_name != registered.name
            or model.metadata.model_version != registered.version
            or tuple(registered.features) != model.feature_names
        ):
            raise TrainingError("Los metadatos del artefacto no coinciden con el registro")
        return model, []
    except (DependencyUnavailableError, FileNotFoundError, OSError, TrainingError, ValueError) as exc:
        return None, [f"No se pudo cargar XGBoost activo ({exc}); se usó el baseline."]


def _xgboost_features(
    db: Session, match: models.Match, model: TrainedXGBoostGoalsModel
) -> dict[str, Any]:
    """Rebuild the exact leakage-safe feature schema used during XGBoost training."""

    records = database_match_records(db, is_mock_data=match.is_mock_data)
    features = FeatureBuilder().build_for_match(
        records,
        target_date=match.match_date,
        home_team_id=match.home_team_id,
        away_team_id=match.away_team_id,
        competition=match.competition_id,
    )
    missing = [name for name in model.feature_names if name not in features]
    if missing:
        raise TrainingError(
            "El esquema de variables del modelo no coincide: " + ", ".join(missing[:5])
        )
    return features


def _xgboost_key_factors(model_prediction: Any) -> list[schemas.KeyFactor]:
    factors: list[schemas.KeyFactor] = []
    seen: set[tuple[str, str]] = set()
    for factor in model_prediction.key_factors:
        key = (factor.factor, factor.target)
        if key in seen:
            continue
        seen.add(key)
        factors.append(
            schemas.KeyFactor(
                factor=f"Importancia global XGBoost: {factor.factor}",
                impact=factor.impact,
                value=round(float(factor.value), 4),
                source_feature=factor.factor,
            )
        )
        if len(factors) == 6:
            break
    return factors


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _weighted_mean(values_newest_first: list[float], decay: float = 0.82) -> float | None:
    if not values_newest_first:
        return None
    weights = [decay**index for index in range(len(values_newest_first))]
    return sum(value * weight for value, weight in zip(values_newest_first, weights, strict=True)) / sum(weights)


def _team_goal_history(
    db: Session,
    team_id: int,
    before: datetime,
    *,
    is_mock_data: bool,
    limit: int = 10,
) -> dict[str, Any]:
    matches = repositories.historical_team_matches(
        db, team_id, before, is_mock_data=is_mock_data
    )[:limit]
    goals_for: list[float] = []
    goals_against: list[float] = []
    for match in matches:
        if match.home_team_id == team_id:
            goals_for.append(float(match.home_score or 0))
            goals_against.append(float(match.away_score or 0))
        else:
            goals_for.append(float(match.away_score or 0))
            goals_against.append(float(match.home_score or 0))
    return {
        "count": len(matches),
        "goals_for": goals_for,
        "goals_against": goals_against,
        "weighted_for": _weighted_mean(goals_for),
        "weighted_against": _weighted_mean(goals_against),
        "average_for": fmean(goals_for) if goals_for else None,
        "average_against": fmean(goals_against) if goals_against else None,
        "max_date": matches[0].match_date if matches else None,
        "match_ids": [match.id for match in matches],
        "sources": sorted({match.data_source for match in matches}),
    }


def _league_history(
    db: Session, competition_id: int, before: datetime, *, is_mock_data: bool
) -> dict[str, Any]:
    matches = list(
        db.scalars(
            select(models.Match)
            .where(
                models.Match.competition_id == competition_id,
                models.Match.status == "finished",
                models.Match.match_date < before,
                models.Match.is_mock_data == is_mock_data,
                models.Match.home_score.is_not(None),
                models.Match.away_score.is_not(None),
            )
            .order_by(models.Match.match_date.desc())
            .limit(200)
        ).all()
    )
    return {
        "count": len(matches),
        "home_average": fmean(float(match.home_score or 0) for match in matches) if matches else None,
        "away_average": fmean(float(match.away_score or 0) for match in matches) if matches else None,
    }


def _feature_snapshot(db: Session, match: models.Match) -> tuple[dict[str, Any], list[str]]:
    home = _team_goal_history(
        db, match.home_team_id, match.match_date, is_mock_data=match.is_mock_data
    )
    away = _team_goal_history(
        db, match.away_team_id, match.match_date, is_mock_data=match.is_mock_data
    )
    league = _league_history(
        db, match.competition_id, match.match_date, is_mock_data=match.is_mock_data
    )
    warnings: list[str] = []
    features = {
        "match_id": match.id,
        "feature_cutoff": match.match_date.isoformat(),
        "home_history_matches": home["count"],
        "away_history_matches": away["count"],
        "league_history_matches": league["count"],
        "home_goals_for_recency_weighted_10": home["weighted_for"],
        "home_goals_against_recency_weighted_10": home["weighted_against"],
        "away_goals_for_recency_weighted_10": away["weighted_for"],
        "away_goals_against_recency_weighted_10": away["weighted_against"],
        "home_goals_for_avg_10": home["average_for"],
        "home_goals_against_avg_10": home["average_against"],
        "away_goals_for_avg_10": away["average_for"],
        "away_goals_against_avg_10": away["average_against"],
        "home_history_max_date": home["max_date"].isoformat() if home["max_date"] else None,
        "away_history_max_date": away["max_date"].isoformat() if away["max_date"] else None,
        "league_home_goals_avg": league["home_average"],
        "league_away_goals_avg": league["away_average"],
        "historical_sources": sorted({*home["sources"], *away["sources"]}),
        "historical_match_ids": sorted({*home["match_ids"], *away["match_ids"]}),
        "history_contains_mock_data": bool(match.is_mock_data and (home["count"] or away["count"])),
    }
    return features, warnings


def _lineup_quality(
    db: Session, match: models.Match, *, as_of: datetime
) -> tuple[list[models.Lineup], float, bool]:
    candidates = list(
        db.scalars(
            select(models.Lineup)
            .options(selectinload(models.Lineup.player), selectinload(models.Lineup.team))
            .where(models.Lineup.match_id == match.id)
        ).all()
    )
    cutoff = min(_as_utc(as_of), _as_utc(match.match_date))
    lineups = []
    for lineup in candidates:
        recorded_at = lineup.source_updated_at or lineup.created_at
        if recorded_at is not None and _as_utc(recorded_at) <= cutoff:
            lineups.append(lineup)
    if not lineups:
        return [], 0.0, False
    teams_present = len({lineup.team_id for lineup in lineups})
    completeness = min(len(lineups) / 22.0, 1.0) * min(teams_present / 2.0, 1.0)
    confirmed = bool(lineups) and all(lineup.confirmed for lineup in lineups)
    return lineups, completeness, confirmed


def _statistics_rows(
    db: Session,
    team_id: int,
    before: datetime,
    *,
    is_mock_data: bool,
    limit: int = 10,
) -> list[models.TeamMatchStatistics]:
    stmt = (
        select(models.TeamMatchStatistics)
        .join(models.Match, models.Match.id == models.TeamMatchStatistics.match_id)
        .where(
            models.TeamMatchStatistics.team_id == team_id,
            models.Match.status == "finished",
            models.Match.match_date < before,
            models.Match.is_mock_data == is_mock_data,
        )
        .order_by(models.Match.match_date.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def _average_present(rows: list[Any], field: str) -> float | None:
    values = [float(value) for row in rows if (value := getattr(row, field, None)) is not None]
    return fmean(values) if values else None


def _poisson_over(rate: float, threshold: float) -> float:
    # P(X > n.5) using a stable finite recurrence; card means in this app are small.
    maximum_under = math.floor(threshold)
    probability = math.exp(-rate)
    cumulative = probability
    for count in range(1, maximum_under + 1):
        probability *= rate / count
        cumulative += probability
    return max(0.0, min(1.0, 1.0 - cumulative))


def _league_fallback(
    db: Session, competition_id: int, *, is_mock_data: bool
) -> tuple[float | None, float | None, float | None]:
    """League-wide average cards, corners, shots from last 20 matches."""
    rows = list(
        db.scalars(
            select(models.TeamMatchStatistics)
            .join(models.Match, models.Match.id == models.TeamMatchStatistics.match_id)
            .where(
                models.Match.competition_id == competition_id,
                models.Match.is_mock_data == is_mock_data,
            )
            .order_by(models.Match.match_date.desc())
            .limit(40)
        ).all()
    )
    if not rows:
        return None, None, None
    cards = [
        float((r.yellow_cards or 0) + (r.red_cards or 0))
        for r in rows if r.yellow_cards is not None or r.red_cards is not None
    ]
    corners = [float(r.corners or 0) for r in rows if r.corners is not None]
    shots = [float(r.shots or 0) for r in rows if r.shots is not None]
    return (
        fmean(cards) if cards else None,
        fmean(corners) if corners else None,
        fmean(shots) if shots else None,
    )


def _cards_and_events(
    db: Session, match: models.Match
) -> tuple[schemas.CardsPrediction, schemas.OtherEventsPrediction, float, list[str]]:
    home_rows = _statistics_rows(
        db, match.home_team_id, match.match_date, is_mock_data=match.is_mock_data
    )
    away_rows = _statistics_rows(
        db, match.away_team_id, match.match_date, is_mock_data=match.is_mock_data
    )
    warnings: list[str] = []
    home_cards_values = [
        float((row.yellow_cards or 0) + (row.red_cards or 0))
        for row in home_rows
        if row.yellow_cards is not None or row.red_cards is not None
    ]
    away_cards_values = [
        float((row.yellow_cards or 0) + (row.red_cards or 0))
        for row in away_rows
        if row.yellow_cards is not None or row.red_cards is not None
    ]
    if home_cards_values and away_cards_values:
        expected_home = fmean(home_cards_values)
        expected_away = fmean(away_cards_values)
        expected_total = expected_home + expected_away
        cards = schemas.CardsPrediction(
            expected_total=expected_total,
            expected_home=expected_home,
            expected_away=expected_away,
            over_2_5=_poisson_over(expected_total, 2.5),
            over_3_5=_poisson_over(expected_total, 3.5),
            over_4_5=_poisson_over(expected_total, 4.5),
            over_5_5=_poisson_over(expected_total, 5.5),
            over_6_5=_poisson_over(expected_total, 6.5),
            under_3_5=1.0 - _poisson_over(expected_total, 3.5),
            under_4_5=1.0 - _poisson_over(expected_total, 4.5),
            under_5_5=1.0 - _poisson_over(expected_total, 5.5),
            under_6_5=1.0 - _poisson_over(expected_total, 6.5),
            available=True,
        )
    else:
        league_avg_cards, _, _ = _league_fallback(db, match.competition_id, is_mock_data=match.is_mock_data)
        if league_avg_cards is not None:
            expected_home = league_avg_cards * 0.5
            expected_away = league_avg_cards * 0.5
            expected_total = league_avg_cards
            cards = schemas.CardsPrediction(
                expected_total=expected_total,
                expected_home=expected_home,
                expected_away=expected_away,
                over_2_5=_poisson_over(expected_total, 2.5),
                over_3_5=_poisson_over(expected_total, 3.5),
                over_4_5=_poisson_over(expected_total, 4.5),
                over_5_5=_poisson_over(expected_total, 5.5),
                over_6_5=_poisson_over(expected_total, 6.5),
                under_3_5=1.0 - _poisson_over(expected_total, 3.5),
                under_4_5=1.0 - _poisson_over(expected_total, 4.5),
                under_5_5=1.0 - _poisson_over(expected_total, 5.5),
                under_6_5=1.0 - _poisson_over(expected_total, 6.5),
                available=True,
            )
        else:
            cards = schemas.CardsPrediction(available=False)
    event_values: dict[str, float | None] = {}
    for field, output_name in (
        ("corners", "expected_corners"),
        ("shots", "expected_shots"),
        ("shots_on_target", "expected_shots_on_target"),
    ):
        home_average = _average_present(home_rows, field)
        away_average = _average_present(away_rows, field)
        if home_average is not None and away_average is not None:
            event_values[output_name] = home_average + away_average
        else:
            _, league_avg_corners, league_avg_shots = _league_fallback(
                db, match.competition_id, is_mock_data=match.is_mock_data
            )
            if output_name == "expected_corners" and league_avg_corners is not None:
                event_values[output_name] = league_avg_corners
            elif output_name == "expected_shots" and league_avg_shots is not None:
                event_values[output_name] = league_avg_shots
            else:
                event_values[output_name] = None
    expected_corners = event_values.get("expected_corners")
    if expected_corners is not None:
        for threshold in [6.5, 7.5, 8.5, 9.5, 10.5, 11.5, 12.5]:
            token = str(threshold).replace(".", "_")
            over = _poisson_over(expected_corners, threshold)
            event_values[f"corners_over_{token}"] = over
            event_values[f"corners_under_{token}"] = 1.0 - over
    stats_coverage = min((len(home_rows) + len(away_rows)) / 20.0, 1.0)
    return cards, schemas.OtherEventsPrediction(**event_values), stats_coverage, warnings


def _player_history(
    db: Session, player_id: int, before: datetime, *, is_mock_data: bool
) -> tuple[int, int, int, int]:
    rows = list(
        db.scalars(
            select(models.PlayerMatch)
            .join(models.Match, models.Match.id == models.PlayerMatch.match_id)
            .where(
                models.PlayerMatch.player_id == player_id,
                models.Match.match_date < before,
                models.Match.status == "finished",
                models.Match.is_mock_data == is_mock_data,
            )
            .order_by(models.Match.match_date.desc())
            .limit(10)
        ).all()
    )
    return (
        sum(row.minutes_played or 0 for row in rows),
        sum(row.goals for row in rows),
        sum(row.yellow_cards + row.red_cards for row in rows),
        len(rows),
    )


def _active_unavailable_players(
    db: Session, match: models.Match
) -> tuple[set[int], int, int]:
    kickoff_date = _as_utc(match.match_date).date()
    team_ids = (match.home_team_id, match.away_team_id)
    injuries = list(
        db.scalars(
            select(models.Injury).where(
                models.Injury.team_id.in_(team_ids),
                models.Injury.active.is_(True),
                models.Injury.is_mock_data == match.is_mock_data,
            )
        ).all()
    )
    suspensions = list(
        db.scalars(
            select(models.Suspension).where(
                models.Suspension.team_id.in_(team_ids),
                models.Suspension.active.is_(True),
                models.Suspension.is_mock_data == match.is_mock_data,
            )
        ).all()
    )
    active_injuries = [
        item
        for item in injuries
        if (item.start_date is None or item.start_date <= kickoff_date)
        and (item.expected_return_date is None or item.expected_return_date >= kickoff_date)
    ]
    active_suspensions = [
        item
        for item in suspensions
        if (item.start_date is None or item.start_date <= kickoff_date)
        and (item.end_date is None or item.end_date >= kickoff_date)
    ]
    unavailable = {
        *(item.player_id for item in active_injuries),
        *(item.player_id for item in active_suspensions),
    }
    return unavailable, len(active_injuries), len(active_suspensions)


def _player_predictions(
    db: Session,
    match: models.Match,
    lineups: list[models.Lineup],
    *,
    confirmed_required: bool,
    unavailable_player_ids: set[int],
) -> tuple[list[schemas.ScorerPrediction], list[schemas.CardRiskPrediction], list[str]]:
    warnings: list[str] = []
    usable = [lineup for lineup in lineups if lineup.confirmed] if confirmed_required else lineups
    if confirmed_required and not usable:
        warnings.append(
            "Se solicitó alineación confirmada, pero no existe; no se generan probabilidades por jugador."
        )
        return [], [], warnings
    scorers: list[schemas.ScorerPrediction] = []
    risks: list[schemas.CardRiskPrediction] = []
    skipped_unavailable = 0
    for lineup in usable:
        if lineup.player_id in unavailable_player_ids:
            skipped_unavailable += 1
            continue
        expected_minutes = lineup.expected_minutes
        if expected_minutes is None or expected_minutes <= 0:
            continue
        minutes, goals, cards, observations = _player_history(
            db,
            lineup.player_id,
            match.match_date,
            is_mock_data=match.is_mock_data,
        )
        if minutes <= 0 or observations == 0:
            continue
        if lineup.confirmed:
            play_factor: float | None = 1.0
            participation_source = "confirmed"
        elif lineup.player.probable_start_probability is not None:
            play_factor = lineup.player.probable_start_probability
            participation_source = "manual_input"
        else:
            play_factor = None
            participation_source = "unavailable"
        goal_rate_for_minutes = (goals / minutes) * expected_minutes
        card_rate_for_minutes = (cards / minutes) * expected_minutes
        goal_if_plays = 1.0 - math.exp(-goal_rate_for_minutes)
        card_if_plays = 1.0 - math.exp(-card_rate_for_minutes)
        note = (
            "Alineación confirmada."
            if lineup.confirmed
            else "Probabilidad condicionada a que el jugador participe; no se infiere P(juega) desde los minutos."
        )
        scorers.append(
            schemas.ScorerPrediction(
                player_id=str(lineup.player_id),
                player_name=lineup.player.name,
                team=lineup.team.name,
                probability_if_plays=goal_if_plays,
                probability_to_play=play_factor,
                unconditional_probability=goal_if_plays * play_factor if play_factor is not None else None,
                expected_minutes=expected_minutes,
                conditional_note=note,
                participation_probability_source=participation_source,
            )
        )
        risks.append(
            schemas.CardRiskPrediction(
                player_id=str(lineup.player_id),
                player_name=lineup.player.name,
                team=lineup.team.name,
                probability_if_plays=card_if_plays,
                probability_to_play=play_factor,
                unconditional_probability=card_if_plays * play_factor if play_factor is not None else None,
                participation_probability_source=participation_source,
            )
        )
    if skipped_unavailable:
        warnings.append(
            f"Se excluyeron {skipped_unavailable} jugadores con lesión o suspensión activa."
        )
    scorers.sort(
        key=lambda row: (
            row.unconditional_probability
            if row.unconditional_probability is not None
            else row.probability_if_plays
        ),
        reverse=True,
    )
    risks.sort(
        key=lambda row: (
            row.unconditional_probability
            if row.unconditional_probability is not None
            else row.probability_if_plays
        ),
        reverse=True,
    )
    return scorers[:10], risks[:10], warnings


def _confidence_label(score: float) -> str:
    if score < 0.50:
        return "datos insuficientes"
    if score < 0.60:
        return "confianza baja"
    if score < 0.70:
        return "confianza moderada"
    if score < 0.80:
        return "confianza alta"
    return "confianza muy alta"


def _impact(value: float, baseline: float | None, *, inverse: bool = False) -> str:
    if baseline is None or baseline == 0:
        return "neutral"
    delta = (value - baseline) / baseline
    if inverse:
        delta = -delta
    magnitude = "high" if abs(delta) >= 0.35 else "medium" if abs(delta) >= 0.15 else "low"
    return f"{magnitude}_{'positive' if delta >= 0 else 'negative'}"


def _key_factors(features: dict[str, Any]) -> list[schemas.KeyFactor]:
    league_home = features.get("league_home_goals_avg")
    league_away = features.get("league_away_goals_avg")
    candidates = [
        (
            "Promedio ponderado de goles recientes del local",
            "home_goals_for_recency_weighted_10",
            league_home,
            False,
        ),
        (
            "Promedio ponderado de goles recibidos por el visitante",
            "away_goals_against_recency_weighted_10",
            league_home,
            False,
        ),
        (
            "Promedio ponderado de goles recientes del visitante",
            "away_goals_for_recency_weighted_10",
            league_away,
            True,
        ),
        (
            "Promedio ponderado de goles recibidos por el local",
            "home_goals_against_recency_weighted_10",
            league_away,
            True,
        ),
    ]
    factors: list[schemas.KeyFactor] = []
    for label, key, baseline, inverse in candidates:
        value = features.get(key)
        if value is None:
            continue
        factors.append(
            schemas.KeyFactor(
                factor=label,
                impact=_impact(float(value), float(baseline) if baseline is not None else None, inverse=inverse),
                value=round(float(value), 4),
                source_feature=key,
            )
        )
    return factors


def generate_prediction(
    db: Session, request: schemas.PredictionRequest
) -> schemas.PredictionResponse:
    generated_at = datetime.now(UTC)
    match = repositories.load_match(db, request.match_id)
    if match is None:
        raise LookupError("Partido no encontrado")
    if generated_at >= _as_utc(match.match_date) and match.status != "scheduled":
        pass
    features, warnings = _feature_snapshot(db, match)
    if not features["league_history_matches"]:
        warnings.append("No hay historial de esta competición; se usan promedios por defecto.")
    if not features["home_history_matches"]:
        warnings.append(f"Sin partidos previos de {match.home_team.name} en la competición; se usa estimación por defecto.")
    if not features["away_history_matches"]:
        warnings.append(f"Sin partidos previos de {match.away_team.name} en la competición; se usa estimación por defecto.")
    lineups, lineup_coverage, lineups_confirmed = _lineup_quality(
        db, match, as_of=generated_at
    )
    unavailable_ids, active_injuries, active_suspensions = _active_unavailable_players(
        db, match
    )
    cards, event_estimates, stats_coverage, stats_warnings = _cards_and_events(db, match)
    warnings.extend(stats_warnings)
    historical_dates = [
        value
        for value in (
            features.get("home_history_max_date"),
            features.get("away_history_max_date"),
        )
        if value
    ]
    latest_historical = (
        max(datetime.fromisoformat(value) for value in historical_dates)
        if historical_dates
        else None
    )
    if latest_historical is not None:
        latest_historical = _as_utc(latest_historical)
    match_kickoff = _as_utc(match.match_date)
    data_age_days = (
        (match_kickoff - latest_historical).total_seconds() / 86400.0
        if latest_historical
        else None
    )
    recency_coverage = (
        max(0.0, min(1.0, 1.0 - data_age_days / 365.0))
        if data_age_days is not None
        else 0.0
    )
    history_coverage = min(
        (features["home_history_matches"] + features["away_history_matches"]) / 20.0,
        1.0,
    )
    referee_coverage = 1.0 if match.referee_id else 0.0
    quality = max(
        0.0,
        min(
            1.0,
            0.45 * history_coverage
            + 0.20 * stats_coverage
            + 0.15 * lineup_coverage
            + 0.10 * referee_coverage
            + 0.10 * recency_coverage,
        ),
    )
    analysis_is_mock = bool(match.is_mock_data or features["history_contains_mock_data"])
    if analysis_is_mock:
        quality *= 0.85
    confidence = 0.0
    features["feature_quality_score"] = quality
    features["data_age_days"] = data_age_days
    features["quality_components"] = {
        "history_coverage": history_coverage,
        "statistics_coverage": stats_coverage,
        "lineup_coverage": lineup_coverage,
        "referee_coverage": referee_coverage,
        "recency_coverage": recency_coverage,
    }
    if analysis_is_mock:
        warnings.insert(0, DEMO_WARNING)

    predictor = PoissonPredictor()
    poisson = predictor.predict_from_features(features, top_n=5, confidence_score=confidence)
    xgboost_key_factors: list[schemas.KeyFactor] | None = None
    active_xgboost, xgboost_warnings = _load_active_xgboost(db, match)
    warnings.extend(xgboost_warnings)
    if active_xgboost is not None:
        try:
            trained_features = _xgboost_features(db, match, active_xgboost)
            poisson = active_xgboost.predict(
                trained_features,
                top_n=5,
                data_quality_score=quality,
                confidence_score=confidence,
            )
            xgboost_key_factors = _xgboost_key_factors(poisson)
            features["trained_model_feature_schema"] = list(active_xgboost.feature_names)
            features["trained_model_feature_values"] = {
                name: trained_features.get(name) for name in active_xgboost.feature_names
            }
            warnings.append(
                "XGBoost activo: los factores muestran importancia global, no explicación causal local."
            )
        except (FeatureEngineeringError, TrainingError, ValueError) as exc:
            warnings.append(f"XGBoost no pudo generar esta predicción ({exc}); se usó el baseline.")
    first_half = predictor.predict(poisson.lambda_home * 0.45, poisson.lambda_away * 0.45, top_n=3)
    event_estimates.home_scores_first = poisson.first_goal["home"]
    event_estimates.first_half_goal = 1.0 - first_half.matrix[0][0]
    event_estimates.halftime_result = schemas.MatchResultProbabilities(**first_half.match_result)
    event_estimates.home_clean_sheet = poisson.clean_sheets["home_clean_sheet"]
    event_estimates.away_clean_sheet = poisson.clean_sheets["away_clean_sheet"]
    # No penalty event history is available, so this field deliberately remains null.
    event_estimates.penalty_awarded = None
    event_estimates.assumptions = {
        "first_half_goal": "technical_assumption: 45% de intensidad Poisson; no calibrado",
        "halftime_result": "technical_assumption: 45% de intensidad Poisson; no calibrado",
        "home_scores_first": "derived_independent_poisson: no calibrado",
    }
    scorers, card_risks, player_warnings = _player_predictions(
        db,
        match,
        lineups,
        confirmed_required=request.use_confirmed_lineups,
        unavailable_player_ids=unavailable_ids,
    )
    warnings.extend(player_warnings)
    manual_fields = {
        key: value
        for key, value in {
            "match.venue": match.venue,
            "match.weather": match.weather,
            "match.importance": match.importance,
            "match.notes": match.notes,
            "home_team.coach_name": match.home_team.coach_name,
            "home_team.stadium": match.home_team.stadium,
            "home_team.manual_elo": match.home_team.manual_elo,
            "home_team.recent_form": match.home_team.recent_form,
            "away_team.coach_name": match.away_team.coach_name,
            "away_team.stadium": match.away_team.stadium,
            "away_team.manual_elo": match.away_team.manual_elo,
            "away_team.recent_form": match.away_team.recent_form,
            "lineup_entries": len(lineups) if lineups else None,
            "active_injuries": active_injuries,
            "active_suspensions": active_suspensions,
        }.items()
        if value is not None
    }
    missing_fields = [
        key
        for key, present in {
            "referee": bool(match.referee_id),
            "lineups": bool(lineups),
            "confirmed_lineups": lineups_confirmed,
            "weather": match.weather is not None,
            "importance": match.importance is not None,
            "home_team.coach_name": bool(match.home_team.coach_name),
            "away_team.coach_name": bool(match.away_team.coach_name),
            "injuries": active_injuries > 0,
            "suspensions": active_suspensions > 0,
        }.items()
        if not present
    ]
    response = schemas.PredictionResponse(
        prediction_id="pending",
        match=schemas.PredictionMatch(
            id=str(match.id),
            home_team=match.home_team.name,
            away_team=match.away_team.name,
            competition=match.competition.name,
            kickoff=match.match_date,
            is_mock_data=analysis_is_mock,
        ),
        analysis=schemas.PredictionAnalysis(
            generated_at=generated_at,
            model_version=poisson.model_version,
            data_quality_score=quality,
            confidence_score=confidence,
            confidence_label=_confidence_label(confidence),
            history_cutoff=match.match_date,
            is_mock_data=analysis_is_mock,
            confidence_method="unavailable",
            probability_calibration_status="not_calibrated",
            data_quality_method="coverage_heuristic",
            historical_sources=features["historical_sources"],
            latest_historical_match_at=latest_historical,
            matches_used=len(features["historical_match_ids"]),
            data_age_days=data_age_days,
            manual_fields=manual_fields,
            missing_fields=missing_fields,
            history_contains_mock_data=bool(features["history_contains_mock_data"]),
        ),
        match_result=schemas.MatchResultProbabilities(**poisson.match_result),
        goals=schemas.GoalsPrediction(
            expected_home_goals=poisson.lambda_home,
            expected_away_goals=poisson.lambda_away,
            over_1_5=poisson.goals["over_1_5"],
            over_2_5=poisson.goals["over_2_5"],
            over_3_5=poisson.goals["over_3_5"],
            under_4_5=poisson.goals["under_4_5"],
            both_teams_score=poisson.both_teams_score["yes"],
            over_0_5=1.0 - poisson.matrix[0][0],
            over_4_5=poisson.goals["over_4_5"],
            under_0_5=poisson.matrix[0][0],
            under_1_5=poisson.goals["under_1_5"],
            under_2_5=poisson.goals["under_2_5"],
            under_3_5=poisson.goals["under_3_5"],
        ),
        score_matrix=[list(row[:6]) for row in poisson.matrix[:6]],
        likely_scores=[
            schemas.LikelyScore(score=item.score, probability=item.probability)
            for item in poisson.likely_scores
        ],
        cards=cards,
        likely_scorers=scorers,
        card_risks=card_risks,
        other_events=event_estimates,
        key_factors=xgboost_key_factors or _key_factors(features),
        warnings=list(dict.fromkeys(warnings)),
        disclaimer=DISCLAIMER,
    )
    prediction = models.Prediction(
        match_id=match.id,
        model_name=poisson.model_name,
        model_version=poisson.model_version,
        prediction_type="match_analysis",
        probability=None,
        confidence=confidence,
        quality_score=quality,
        generated_at=generated_at,
        features_snapshot=features,
        explanation=[factor.model_dump(mode="json") for factor in response.key_factors],
        response_payload={},
        status="pending",
        is_mock_data=analysis_is_mock,
    )
    db.add(prediction)
    db.flush()
    response.prediction_id = prediction.id
    prediction.response_payload = response.model_dump(mode="json")
    db.commit()
    return response


def response_from_prediction(prediction: models.Prediction) -> schemas.PredictionResponse:
    response = schemas.PredictionResponse.model_validate(prediction.response_payload)
    if prediction.outcomes:
        response.outcomes = [
            schemas.PredictionOutcomeOut(
                event_type=o.event_type,
                predicted_probability=o.predicted_probability,
                predicted_value=o.predicted_value,
                actual_value=o.actual_value,
                status=o.status,
                evaluated_at=o.evaluated_at,
            )
            for o in prediction.outcomes
        ]
    return response


def evaluate_prediction(
    db: Session,
    prediction: models.Prediction,
    *,
    home_score: int,
    away_score: int,
    commit: bool = True,
) -> list[models.PredictionOutcome]:
    match = db.get(models.Match, prediction.match_id)
    if match is None:
        raise LookupError("El partido de la predicción no existe")
    existing = list(
        db.scalars(
            select(models.PredictionOutcome).where(
                models.PredictionOutcome.prediction_id == prediction.id
            )
        ).all()
    )
    existing_by_type = {outcome.event_type: outcome for outcome in existing}
    payload = prediction.response_payload
    result = "home_win" if home_score > away_score else "away_win" if away_score > home_score else "draw"
    predicted_result = max(payload["match_result"], key=payload["match_result"].get)
    total = home_score + away_score
    btts = home_score > 0 and away_score > 0
    evaluated_at = datetime.now(UTC)

    # ── Gather actual match stats for cards/corners evaluation ──
    actual_cards_total: float | None = None
    actual_corners_total: float | None = None
    stats_rows = list(
        db.scalars(
            select(models.TeamMatchStatistics).where(
                models.TeamMatchStatistics.match_id == prediction.match_id
            )
        ).all()
    )
    if stats_rows:
        actual_cards_total = sum(
            (r.yellow_cards or 0) + (r.red_cards or 0) for r in stats_rows
        )
        actual_corners_total = sum(r.corners or 0 for r in stats_rows)

    # ── Scorers evaluation: top 3 predicted ──
    top_scorer_names: list[str] = []
    for scorer in (payload.get("likely_scorers") or [])[:3]:
        if isinstance(scorer, dict) and scorer.get("player_name"):
            top_scorer_names.append(scorer["player_name"])
    actual_scorer_names: set[str] = set()
    scorer_rows = list(
        db.execute(
            select(models.Player.name)
            .join(models.PlayerMatch, models.PlayerMatch.player_id == models.Player.id)
            .where(
                models.PlayerMatch.match_id == prediction.match_id,
                models.PlayerMatch.goals > 0,
            )
        ).all()
    )
    for row in scorer_rows:
        if row[0]:
            actual_scorer_names.add(row[0])
    any_top_scorer_scored = bool(top_scorer_names and actual_scorer_names.intersection(top_scorer_names))

    # ── Helper to build a line outcome ──
    def _line_outcome(event_type: str, predicted_probability: float | None, predicted: bool, actual: bool, actual_total: float | None = None) -> tuple[str, float | None, dict, dict, str]:
        return (
            event_type,
            predicted_probability,
            {"predicted": predicted},
            {"actual": actual, "total": actual_total} if actual_total is not None else {"actual": actual},
            "correct" if predicted == actual else "incorrect",
        )

    specifications = [
        (
            "match_result",
            payload["match_result"][predicted_result],
            {"predicted": predicted_result},
            {"actual": result},
            "correct" if predicted_result == result else "incorrect",
        ),
        (
            "over_2_5_goals",
            payload["goals"]["over_2_5"],
            {"predicted": payload["goals"]["over_2_5"] >= 0.5},
            {"actual": total > 2.5},
            "correct" if (payload["goals"]["over_2_5"] >= 0.5) == (total > 2.5) else "incorrect",
        ),
        (
            "both_teams_score",
            payload["goals"]["both_teams_score"],
            {"predicted": payload["goals"]["both_teams_score"] >= 0.5},
            {"actual": btts},
            "correct" if (payload["goals"]["both_teams_score"] >= 0.5) == btts else "incorrect",
        ),
    ]

    # Goals lines
    goals_lines = [0.5, 1.5, 2.5, 3.5, 4.5]
    for line in goals_lines:
        token = str(line).replace(".", "_")
        over_key = f"over_{token}"
        over_prob = payload.get("goals", {}).get(over_key)
        if over_prob is not None:
            predicted_over = over_prob >= 0.5
            actual_over = total > line
            specifications.append(_line_outcome(f"goals_over_{token}", over_prob, predicted_over, actual_over, total))

    # Cards lines (if we have stats data)
    if actual_cards_total is not None:
        cards_lines = [3.5, 4.5, 5.5, 6.5]
        cards_payload = payload.get("cards") or {}
        for line in cards_lines:
            token = str(line).replace(".", "_")
            over_prob = cards_payload.get(f"over_{token}")
            if over_prob is not None:
                predicted_over = over_prob >= 0.5
                actual_over = actual_cards_total > line
                specifications.append(_line_outcome(f"cards_over_{token}", over_prob, predicted_over, actual_over, actual_cards_total))

    # Corners lines (if we have stats data)
    if actual_corners_total is not None:
        corners_lines = [6.5, 7.5, 8.5, 9.5, 10.5, 11.5, 12.5]
        events_payload = payload.get("other_events") or {}
        for line in corners_lines:
            token = str(line).replace(".", "_")
            over_prob = events_payload.get(f"corners_over_{token}")
            if over_prob is not None:
                predicted_over = over_prob >= 0.5
                actual_over = actual_corners_total > line
                specifications.append(_line_outcome(f"corners_over_{token}", over_prob, predicted_over, actual_over, actual_corners_total))

    # Top 3 scorers outcome
    if top_scorer_names:
        specifications.append((
            "top_3_scorer_any",
            None,
            {"predicted_scorers": top_scorer_names},
            {"actual_scorers": list(actual_scorer_names)},
            "correct" if any_top_scorer_scored else "incorrect",
        ))

    outcomes: list[models.PredictionOutcome] = list(existing)
    for spec in specifications:
        event_type, probability, predicted_value, actual_value, status = spec
        if event_type in existing_by_type:
            continue
        outcome = models.PredictionOutcome(
            prediction_id=prediction.id,
            event_type=event_type,
            predicted_probability=probability,
            predicted_value=predicted_value,
            actual_value=actual_value,
            status=status,
            evaluated_at=evaluated_at,
        )
        db.add(outcome)
        outcomes.append(outcome)
    # Evaluation metadata may change; the original features, probabilities,
    # explanations and response snapshot remain byte-for-byte untouched.
    prediction.status = (
        "incorrect" if any(outcome.status == "incorrect" for outcome in outcomes) else "correct"
    )
    prediction.actual_outcome = {"home_score": home_score, "away_score": away_score}
    if commit:
        db.commit()
    else:
        db.flush()
    return outcomes
