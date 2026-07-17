from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class SourceMixin:
    data_source: Mapped[str] = mapped_column(String(50), default="internal", index=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_mock_data: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class Competition(TimestampMixin, SourceMixin, Base):
    __tablename__ = "competitions"
    __table_args__ = (UniqueConstraint("data_source", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(150), index=True)
    country: Mapped[str | None] = mapped_column(String(100))
    gender: Mapped[str | None] = mapped_column(String(20))
    catalog_code: Mapped[str | None] = mapped_column(String(50), index=True)
    competition_level: Mapped[int | None] = mapped_column(Integer)
    competition_type: Mapped[str | None] = mapped_column(String(30))
    catalog_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    seasons: Mapped[list[Season]] = relationship(back_populates="competition", cascade="all, delete-orphan")
    aliases: Mapped[list[CompetitionAlias]] = relationship(
        back_populates="competition", cascade="all, delete-orphan"
    )


class Season(TimestampMixin, SourceMixin, Base):
    __tablename__ = "seasons"
    __table_args__ = (UniqueConstraint("competition_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String(100))
    competition_id: Mapped[int] = mapped_column(ForeignKey("competitions.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)

    competition: Mapped[Competition] = relationship(back_populates="seasons")
    matches: Mapped[list[Match]] = relationship(back_populates="season")


class Team(TimestampMixin, SourceMixin, Base):
    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("data_source", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(150), index=True)
    short_name: Mapped[str | None] = mapped_column(String(50))
    country: Mapped[str | None] = mapped_column(String(100))
    city: Mapped[str | None] = mapped_column(String(120))
    founded_year: Mapped[int | None] = mapped_column(Integer)
    coach_name: Mapped[str | None] = mapped_column(String(180))
    stadium: Mapped[str | None] = mapped_column(String(180))
    catalog_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    manual_elo: Mapped[float | None] = mapped_column(Float)
    recent_form: Mapped[list[str] | None] = mapped_column(JSON)
    manual_last_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    aliases: Mapped[list[TeamAlias]] = relationship(back_populates="team", cascade="all, delete-orphan")
    players: Mapped[list[Player]] = relationship(back_populates="current_team")


class Player(TimestampMixin, SourceMixin, Base):
    __tablename__ = "players"
    __table_args__ = (UniqueConstraint("data_source", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String(100))
    current_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), index=True)
    name: Mapped[str] = mapped_column(String(180), index=True)
    birth_date: Mapped[date | None] = mapped_column(Date)
    nationality: Mapped[str | None] = mapped_column(String(100))
    primary_position: Mapped[str | None] = mapped_column(String(50))
    height_m: Mapped[float | None] = mapped_column(Float)
    birthplace: Mapped[str | None] = mapped_column(String(180))
    catalog_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    penalty_taker: Mapped[bool | None] = mapped_column(Boolean)
    free_kick_taker: Mapped[bool | None] = mapped_column(Boolean)
    probable_start_probability: Mapped[float | None] = mapped_column(Float)
    expected_minutes: Mapped[float | None] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    current_team: Mapped[Team | None] = relationship(back_populates="players")
    aliases: Mapped[list[PlayerAlias]] = relationship(back_populates="player", cascade="all, delete-orphan")


class CompetitionAlias(TimestampMixin, Base):
    __tablename__ = "competition_aliases"
    __table_args__ = (UniqueConstraint("provider", "normalized_alias"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    competition_id: Mapped[int] = mapped_column(ForeignKey("competitions.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(50))
    alias: Mapped[str] = mapped_column(String(180))
    normalized_alias: Mapped[str] = mapped_column(String(180), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    review_status: Mapped[str] = mapped_column(String(30), default="approved")

    competition: Mapped[Competition] = relationship(back_populates="aliases")


class TeamAlias(TimestampMixin, Base):
    __tablename__ = "team_aliases"
    __table_args__ = (UniqueConstraint("provider", "normalized_alias"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(50))
    alias: Mapped[str] = mapped_column(String(180))
    normalized_alias: Mapped[str] = mapped_column(String(180), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    review_status: Mapped[str] = mapped_column(String(30), default="approved")

    team: Mapped[Team] = relationship(back_populates="aliases")


class PlayerAlias(TimestampMixin, Base):
    __tablename__ = "player_aliases"
    __table_args__ = (UniqueConstraint("provider", "normalized_alias"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(50))
    alias: Mapped[str] = mapped_column(String(180))
    normalized_alias: Mapped[str] = mapped_column(String(180), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    review_status: Mapped[str] = mapped_column(String(30), default="approved")

    player: Mapped[Player] = relationship(back_populates="aliases")


class EntityResolutionConflict(TimestampMixin, Base):
    __tablename__ = "entity_resolution_conflicts"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(30), index=True)
    provider: Mapped[str] = mapped_column(String(50))
    source_repository: Mapped[str | None] = mapped_column(String(120), index=True)
    source_name: Mapped[str] = mapped_column(String(180))
    normalized_name: Mapped[str] = mapped_column(String(180), index=True)
    candidate_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    best_score: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    resolution_notes: Mapped[str | None] = mapped_column(Text)


class Referee(TimestampMixin, SourceMixin, Base):
    __tablename__ = "referees"
    __table_args__ = (UniqueConstraint("data_source", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(180), index=True)
    nationality: Mapped[str | None] = mapped_column(String(100))


class Injury(TimestampMixin, SourceMixin, Base):
    __tablename__ = "injuries"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    description: Mapped[str | None] = mapped_column(String(255))
    start_date: Mapped[date | None] = mapped_column(Date)
    expected_return_date: Mapped[date | None] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    player: Mapped[Player] = relationship()
    team: Mapped[Team] = relationship()


class Suspension(TimestampMixin, SourceMixin, Base):
    __tablename__ = "suspensions"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    reason: Mapped[str | None] = mapped_column(String(255))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    player: Mapped[Player] = relationship()
    team: Mapped[Team] = relationship()


class Match(TimestampMixin, SourceMixin, Base):
    __tablename__ = "matches"
    __table_args__ = (
        UniqueConstraint("data_source", "external_id"),
        CheckConstraint("home_team_id != away_team_id", name="different_teams"),
        CheckConstraint("home_score IS NULL OR home_score >= 0", name="home_score_nonnegative"),
        CheckConstraint("away_score IS NULL OR away_score >= 0", name="away_score_nonnegative"),
        Index("ix_matches_competition_date", "competition_id", "match_date"),
        Index("ix_matches_teams_date", "home_team_id", "away_team_id", "match_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String(100))
    competition_id: Mapped[int] = mapped_column(ForeignKey("competitions.id"), index=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), index=True)
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    match_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    venue: Mapped[str | None] = mapped_column(String(180))
    status: Mapped[str] = mapped_column(String(30), default="scheduled", index=True)
    home_score: Mapped[int | None] = mapped_column(Integer)
    away_score: Mapped[int | None] = mapped_column(Integer)
    halftime_home_score: Mapped[int | None] = mapped_column(Integer)
    halftime_away_score: Mapped[int | None] = mapped_column(Integer)
    referee_id: Mapped[int | None] = mapped_column(ForeignKey("referees.id"), index=True)
    ingestion_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("data_ingestion_runs.id", ondelete="SET NULL"), index=True
    )
    round_name: Mapped[str | None] = mapped_column(String(100))
    weather: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    importance: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(Text)
    result_details: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    competition: Mapped[Competition] = relationship()
    season: Mapped[Season] = relationship(back_populates="matches")
    home_team: Mapped[Team] = relationship(foreign_keys=[home_team_id])
    away_team: Mapped[Team] = relationship(foreign_keys=[away_team_id])
    referee: Mapped[Referee | None] = relationship()
    lineups: Mapped[list[Lineup]] = relationship(back_populates="match", cascade="all, delete-orphan")
    statistics: Mapped[list[TeamMatchStatistics]] = relationship(
        back_populates="match", cascade="all, delete-orphan"
    )
    predictions: Mapped[list[Prediction]] = relationship(back_populates="match")
    source_records: Mapped[list[MatchSourceRecord]] = relationship(
        back_populates="match", cascade="all, delete-orphan"
    )


class Lineup(TimestampMixin, SourceMixin, Base):
    __tablename__ = "lineups"
    __table_args__ = (UniqueConstraint("match_id", "player_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    started: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[str | None] = mapped_column(String(50))
    shirt_number: Mapped[int | None] = mapped_column(Integer)
    expected_minutes: Mapped[float | None] = mapped_column(Float)

    match: Mapped[Match] = relationship(back_populates="lineups")
    team: Mapped[Team] = relationship()
    player: Mapped[Player] = relationship()


class PlayerMatch(TimestampMixin, SourceMixin, Base):
    __tablename__ = "player_matches"
    __table_args__ = (
        UniqueConstraint("match_id", "player_id"),
        CheckConstraint("minutes_played IS NULL OR (minutes_played >= 0 AND minutes_played <= 130)", name="minutes_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    started: Mapped[bool] = mapped_column(Boolean, default=False)
    minutes_played: Mapped[int | None] = mapped_column(Integer)
    position: Mapped[str | None] = mapped_column(String(50))
    goals: Mapped[int] = mapped_column(Integer, default=0)
    assists: Mapped[int] = mapped_column(Integer, default=0)
    shots: Mapped[int] = mapped_column(Integer, default=0)
    shots_on_target: Mapped[int] = mapped_column(Integer, default=0)
    xg: Mapped[float | None] = mapped_column(Float)
    yellow_cards: Mapped[int] = mapped_column(Integer, default=0)
    red_cards: Mapped[int] = mapped_column(Integer, default=0)
    fouls: Mapped[int] = mapped_column(Integer, default=0)


class MatchEvent(TimestampMixin, SourceMixin, Base):
    __tablename__ = "match_events"
    __table_args__ = (UniqueConstraint("data_source", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String(100))
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), index=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    minute: Mapped[int | None] = mapped_column(Integer)
    second: Mapped[int | None] = mapped_column(Integer)
    period: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class TeamMatchStatistics(TimestampMixin, SourceMixin, Base):
    __tablename__ = "team_match_statistics"
    __table_args__ = (UniqueConstraint("match_id", "team_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    possession: Mapped[float | None] = mapped_column(Float)
    shots: Mapped[int | None] = mapped_column(Integer)
    shots_on_target: Mapped[int | None] = mapped_column(Integer)
    corners: Mapped[int | None] = mapped_column(Integer)
    fouls: Mapped[int | None] = mapped_column(Integer)
    yellow_cards: Mapped[int | None] = mapped_column(Integer)
    red_cards: Mapped[int | None] = mapped_column(Integer)
    xg: Mapped[float | None] = mapped_column(Float)
    passes: Mapped[int | None] = mapped_column(Integer)

    match: Mapped[Match] = relationship(back_populates="statistics")
    team: Mapped[Team] = relationship()


class Prediction(TimestampMixin, Base):
    __tablename__ = "predictions"
    __table_args__ = (
        CheckConstraint("probability IS NULL OR (probability >= 0 AND probability <= 1)", name="probability_range"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint("quality_score >= 0 AND quality_score <= 1", name="quality_range"),
        Index("ix_predictions_match_generated", "match_id", "generated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    model_name: Mapped[str] = mapped_column(String(100), index=True)
    model_version: Mapped[str] = mapped_column(String(50))
    prediction_type: Mapped[str] = mapped_column(String(60), index=True)
    threshold: Mapped[float | None] = mapped_column(Float)
    probability: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    quality_score: Mapped[float] = mapped_column(Float)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    features_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    explanation: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    response_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    actual_outcome: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    is_mock_data: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    match: Mapped[Match] = relationship(back_populates="predictions")
    outcomes: Mapped[list[PredictionOutcome]] = relationship(
        back_populates="prediction", cascade="all, delete-orphan"
    )


class PredictionOutcome(TimestampMixin, Base):
    __tablename__ = "prediction_outcomes"
    __table_args__ = (UniqueConstraint("prediction_id", "event_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    prediction_id: Mapped[str] = mapped_column(
        ForeignKey("predictions.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    predicted_probability: Mapped[float | None] = mapped_column(Float)
    predicted_value: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    actual_value: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    prediction: Mapped[Prediction] = relationship(back_populates="outcomes")


class ModelVersion(TimestampMixin, Base):
    __tablename__ = "model_versions"
    __table_args__ = (UniqueConstraint("name", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    version: Mapped[str] = mapped_column(String(50))
    trained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    max_data_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    features: Mapped[list[str]] = mapped_column(JSON, default=list)
    hyperparameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    data_sources: Mapped[list[str]] = mapped_column(JSON, default=list)
    dataset_hash: Mapped[str | None] = mapped_column(String(128))
    artifact_path: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(30), default="candidate", index=True)
    is_mock_data: Mapped[bool] = mapped_column(Boolean, default=False)

    metrics: Mapped[list[ModelMetric]] = relationship(
        back_populates="model_version", cascade="all, delete-orphan"
    )


class ModelMetric(TimestampMixin, Base):
    __tablename__ = "model_metrics"
    __table_args__ = (UniqueConstraint("model_version_id", "metric_name", "split", "scope"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    model_version_id: Mapped[int] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), index=True
    )
    metric_name: Mapped[str] = mapped_column(String(100), index=True)
    value: Mapped[float] = mapped_column(Float)
    split: Mapped[str] = mapped_column(String(30), default="test")
    scope: Mapped[str] = mapped_column(String(120), default="global")
    sample_size: Mapped[int | None] = mapped_column(Integer)
    calibration_bins: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)

    model_version: Mapped[ModelVersion] = relationship(back_populates="metrics")


class DataIngestionRun(TimestampMixin, Base):
    __tablename__ = "data_ingestion_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source: Mapped[str] = mapped_column(String(50), index=True)
    original_filename: Mapped[str | None] = mapped_column(String(255))
    stored_path: Mapped[str | None] = mapped_column(String(500))
    file_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    import_options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    import_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    preview_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    downloaded_records: Mapped[int] = mapped_column(Integer, default=0)
    valid_records: Mapped[int] = mapped_column(Integer, default=0)
    rejected_records: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    pipeline_version: Mapped[str] = mapped_column(String(50), default="1.0.0")
    is_mock_data: Mapped[bool] = mapped_column(Boolean, default=False)

    source_records: Mapped[list[MatchSourceRecord]] = relationship(back_populates="ingestion_run")


class MatchSourceRecord(TimestampMixin, Base):
    __tablename__ = "match_source_records"
    __table_args__ = (
        UniqueConstraint(
            "source_name", "source_repository", "source_record_id", name="uq_match_source_identity"
        ),
        Index("ix_match_source_records_match_source", "match_id", "source_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(
        ForeignKey("matches.id", ondelete="CASCADE"), index=True
    )
    ingestion_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("data_ingestion_runs.id", ondelete="SET NULL"), index=True
    )
    source_name: Mapped[str] = mapped_column(String(50), index=True)
    source_repository: Mapped[str] = mapped_column(String(120), index=True)
    source_record_id: Mapped[str] = mapped_column(String(128), index=True)
    source_file: Mapped[str | None] = mapped_column(String(500))
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    field_provenance: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    conflict_status: Mapped[str] = mapped_column(String(30), default="none", index=True)
    conflict_details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    match: Mapped[Match] = relationship(back_populates="source_records")
    ingestion_run: Mapped[DataIngestionRun | None] = relationship(back_populates="source_records")


class OpenFootballEntityMapping(TimestampMixin, Base):
    __tablename__ = "openfootball_entity_mappings"
    __table_args__ = (
        UniqueConstraint(
            "entity_type",
            "source_repository",
            "normalized_name",
            name="uq_openfootball_entity_mapping_identity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(30), index=True)
    original_name: Mapped[str] = mapped_column(String(255))
    normalized_name: Mapped[str] = mapped_column(String(255), index=True)
    internal_entity_id: Mapped[int | None] = mapped_column(Integer, index=True)
    source_repository: Mapped[str] = mapped_column(String(120), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    manually_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    resolution_status: Mapped[str] = mapped_column(String(30), default="resolved", index=True)
    resolution_notes: Mapped[str | None] = mapped_column(Text)


class CompetitionSourceCoverage(TimestampMixin, Base):
    __tablename__ = "competition_source_coverage"
    __table_args__ = (
        UniqueConstraint(
            "competition_id",
            "source_name",
            "source_repository",
            name="uq_competition_source_coverage",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    competition_id: Mapped[int] = mapped_column(
        ForeignKey("competitions.id", ondelete="CASCADE"), index=True
    )
    source_name: Mapped[str] = mapped_column(String(50), index=True)
    source_repository: Mapped[str] = mapped_column(String(120), index=True)
    first_match_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_match_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_matches: Mapped[int] = mapped_column(Integer, default=0)
    finished_matches: Mapped[int] = mapped_column(Integer, default=0)
    scheduled_matches: Mapped[int] = mapped_column(Integer, default=0)
    seasons_available: Mapped[list[str]] = mapped_column(JSON, default=list)
    fields_available: Mapped[list[str]] = mapped_column(JSON, default=list)
    last_imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    competition: Mapped[Competition] = relationship()
