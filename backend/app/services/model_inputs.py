"""Shared, database-backed inputs for feature engineering and inference."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def database_match_records(
    db: Session, *, is_mock_data: bool | None = None
) -> list[dict[str, Any]]:
    """Return normalized match rows without mixing demo and real data by default."""

    matches_query = select(models.Match).order_by(models.Match.match_date)
    statistics_query = select(models.TeamMatchStatistics)
    if is_mock_data is not None:
        matches_query = matches_query.where(models.Match.is_mock_data == is_mock_data)
        statistics_query = statistics_query.join(models.Match).where(
            models.Match.is_mock_data == is_mock_data
        )

    matches = list(db.scalars(matches_query).all())
    statistics = list(db.scalars(statistics_query).all())
    stats_by_key = {(row.match_id, row.team_id): row for row in statistics}
    records: list[dict[str, Any]] = []
    for match in matches:
        home_stats = stats_by_key.get((match.id, match.home_team_id))
        away_stats = stats_by_key.get((match.id, match.away_team_id))
        record: dict[str, Any] = {
            "id": match.id,
            "external_id": match.external_id,
            "match_date": match.match_date.isoformat(),
            "competition_id": match.competition_id,
            "home_team_id": match.home_team_id,
            "away_team_id": match.away_team_id,
            "status": match.status.upper(),
            "home_goals": match.home_score,
            "away_goals": match.away_score,
            "data_source": match.data_source,
            "is_mock_data": match.is_mock_data,
        }
        for prefix, row in (("home", home_stats), ("away", away_stats)):
            if row is None:
                continue
            for field in (
                "shots",
                "shots_on_target",
                "corners",
                "fouls",
                "yellow_cards",
                "red_cards",
            ):
                record[f"{prefix}_{field}"] = getattr(row, field)
        records.append(record)
    return records


__all__ = ["database_match_records"]
