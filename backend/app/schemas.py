from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CompetitionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    country: str | None = Field(default=None, max_length=100)
    gender: str | None = Field(default=None, max_length=20)
    external_id: str | None = Field(default=None, max_length=100)
    data_source: str = Field(default="manual", max_length=50)
    is_mock_data: bool = False


class CompetitionOut(ORMModel):
    id: int
    name: str
    country: str | None
    gender: str | None
    data_source: str
    is_mock_data: bool


class SeasonCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    start_date: date | None = None
    end_date: date | None = None
    external_id: str | None = Field(default=None, max_length=100)
    data_source: str = Field(default="manual", max_length=50)
    is_mock_data: bool = False

    @model_validator(mode="after")
    def date_order(self) -> SeasonCreate:
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date no puede ser anterior a start_date")
        return self


class SeasonOut(ORMModel):
    id: int
    competition_id: int
    name: str
    start_date: date | None
    end_date: date | None
    data_source: str
    is_mock_data: bool


class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    short_name: str | None = Field(default=None, max_length=50)
    country: str | None = Field(default=None, max_length=100)
    coach_name: str | None = Field(default=None, max_length=180)
    stadium: str | None = Field(default=None, max_length=180)
    manual_elo: float | None = Field(default=None, ge=500, le=3000)
    recent_form: list[Literal["W", "D", "L"]] | None = Field(default=None, max_length=10)
    external_id: str | None = Field(default=None, max_length=100)
    data_source: str = Field(default="manual", max_length=50)
    is_mock_data: bool = False
    aliases: list[str] = Field(default_factory=list, max_length=20)


class TeamUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    short_name: str | None = Field(default=None, max_length=50)
    country: str | None = Field(default=None, max_length=100)
    coach_name: str | None = Field(default=None, max_length=180)
    stadium: str | None = Field(default=None, max_length=180)
    manual_elo: float | None = Field(default=None, ge=500, le=3000)
    recent_form: list[Literal["W", "D", "L"]] | None = Field(default=None, max_length=10)
    active_aliases: list[str] | None = Field(default=None, max_length=20)


class StandingsEntry(BaseModel):
    position: int
    team_id: int
    team_name: str
    played: int
    won: int
    drawn: int
    lost: int
    goals_for: int
    goals_against: int
    goal_difference: int
    points: int
    form: list[str]

class TeamOut(ORMModel):
    id: int
    name: str
    short_name: str | None
    country: str | None
    coach_name: str | None
    stadium: str | None
    manual_elo: float | None
    recent_form: list[str] | None
    manual_last_updated_at: datetime | None
    data_source: str
    is_mock_data: bool


class PlayerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    current_team_id: int | None = None
    birth_date: date | None = None
    nationality: str | None = Field(default=None, max_length=100)
    primary_position: str | None = Field(default=None, max_length=50)
    penalty_taker: bool | None = None
    free_kick_taker: bool | None = None
    probable_start_probability: float | None = Field(default=None, ge=0, le=1)
    expected_minutes: float | None = Field(default=None, ge=0, le=130)
    external_id: str | None = Field(default=None, max_length=100)
    data_source: str = Field(default="manual", max_length=50)
    is_mock_data: bool = False
    aliases: list[str] = Field(default_factory=list, max_length=20)


class PlayerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=180)
    current_team_id: int | None = None
    birth_date: date | None = None
    nationality: str | None = Field(default=None, max_length=100)
    primary_position: str | None = Field(default=None, max_length=50)
    penalty_taker: bool | None = None
    free_kick_taker: bool | None = None
    probable_start_probability: float | None = Field(default=None, ge=0, le=1)
    expected_minutes: float | None = Field(default=None, ge=0, le=130)
    active: bool | None = None


class PlayerOut(ORMModel):
    id: int
    name: str
    current_team_id: int | None
    birth_date: date | None
    nationality: str | None
    primary_position: str | None
    penalty_taker: bool | None
    free_kick_taker: bool | None
    probable_start_probability: float | None
    expected_minutes: float | None
    active: bool
    data_source: str
    is_mock_data: bool


class MatchCreate(BaseModel):
    competition_id: int
    season_id: int
    home_team_id: int
    away_team_id: int
    match_date: datetime
    venue: str | None = Field(default=None, max_length=180)
    round_name: str | None = Field(default=None, max_length=100)
    weather: dict[str, Any] | None = None
    importance: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=2000)
    external_id: str | None = Field(default=None, max_length=100)
    data_source: str = Field(default="manual", max_length=50)
    is_mock_data: bool = False

    @model_validator(mode="after")
    def teams_are_distinct(self) -> MatchCreate:
        if self.home_team_id == self.away_team_id:
            raise ValueError("Los equipos local y visitante deben ser distintos")
        return self


class ManualLineupEntry(BaseModel):
    player_name: str = Field(min_length=1, max_length=180)
    team: str = Field(min_length=1, max_length=150)
    started: bool = False
    confirmed: bool = False
    position: str | None = Field(default=None, max_length=50)
    shirt_number: int | None = Field(default=None, ge=1, le=99)
    expected_minutes: float | None = Field(default=None, ge=0, le=130)


class ManualAvailabilityEntry(BaseModel):
    player_name: str = Field(min_length=1, max_length=180)
    team: str = Field(min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=255)
    reason: str | None = Field(default=None, max_length=255)
    start_date: date | None = None
    end_date: date | None = None


class ManualMatchCreate(BaseModel):
    competition: str = Field(min_length=1, max_length=150)
    season: str = Field(min_length=1, max_length=100)
    home_team: str = Field(min_length=1, max_length=150)
    away_team: str = Field(min_length=1, max_length=150)
    match_date: datetime = Field(validation_alias=AliasChoices("match_date", "kickoff"))
    venue: str | None = Field(
        default=None, max_length=180, validation_alias=AliasChoices("venue", "stadium")
    )
    referee: str | None = Field(default=None, max_length=180)
    weather: dict[str, Any] | None = None
    importance: str | None = Field(default=None, max_length=100)
    round_name: str | None = Field(
        default=None, max_length=100, validation_alias=AliasChoices("round_name", "round")
    )
    notes: str | None = Field(default=None, max_length=2000)
    lineups: list[ManualLineupEntry] = Field(default_factory=list, max_length=60)
    injuries: list[ManualAvailabilityEntry] = Field(default_factory=list, max_length=30)
    suspensions: list[ManualAvailabilityEntry] = Field(default_factory=list, max_length=30)
    is_mock_data: bool = False

    @model_validator(mode="after")
    def different_team_names(self) -> ManualMatchCreate:
        if self.home_team.casefold().strip() == self.away_team.casefold().strip():
            raise ValueError("Los equipos local y visitante deben ser distintos")
        return self


class MatchUpdate(BaseModel):
    competition_id: int | None = None
    season_id: int | None = None
    home_team_id: int | None = None
    away_team_id: int | None = None
    match_date: datetime | None = None
    venue: str | None = Field(default=None, max_length=180)
    round_name: str | None = Field(default=None, max_length=100)
    weather: dict[str, Any] | None = None
    importance: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=2000)
    status: Literal["scheduled", "postponed", "cancelled"] | None = None


class NamedEntity(BaseModel):
    id: int
    name: str


class MatchOut(BaseModel):
    id: int
    external_id: str | None
    competition: NamedEntity
    season: NamedEntity
    home_team: NamedEntity
    away_team: NamedEntity
    match_date: datetime
    venue: str | None
    round_name: str | None
    weather: dict[str, Any] | None
    importance: str | None
    notes: str | None
    result_details: dict[str, Any] | None
    status: str
    home_score: int | None
    away_score: int | None
    halftime_home_score: int | None
    halftime_away_score: int | None
    data_source: str
    source_updated_at: datetime | None
    is_mock_data: bool


class MatchResultCreate(BaseModel):
    home_score: int = Field(ge=0, le=30)
    away_score: int = Field(ge=0, le=30)
    halftime_home_score: int | None = Field(default=None, ge=0, le=30)
    halftime_away_score: int | None = Field(default=None, ge=0, le=30)
    status: Literal["finished", "abandoned", "void"] = "finished"
    details: dict[str, Any] | None = None

    @model_validator(mode="after")
    def halftime_not_above_fulltime(self) -> MatchResultCreate:
        if self.halftime_home_score is not None and self.halftime_home_score > self.home_score:
            raise ValueError("El marcador local al descanso supera el marcador final")
        if self.halftime_away_score is not None and self.halftime_away_score > self.away_score:
            raise ValueError("El marcador visitante al descanso supera el marcador final")
        return self


class ScorerInput(BaseModel):
    player_id: int | None = None
    player_name: str | None = Field(default=None, min_length=1, max_length=150)
    team: Literal["home", "away"]
    goals: int = Field(ge=0, le=10)


class CardInput(BaseModel):
    player_id: int | None = None
    player_name: str | None = Field(default=None, min_length=1, max_length=150)
    team: Literal["home", "away"]
    yellow: bool = False
    red: bool = False


class ManualStatsCreate(BaseModel):
    home_corners: int | None = Field(default=None, ge=0, le=50)
    away_corners: int | None = Field(default=None, ge=0, le=50)
    home_yellow_cards: int | None = Field(default=None, ge=0, le=20)
    away_yellow_cards: int | None = Field(default=None, ge=0, le=20)
    home_red_cards: int | None = Field(default=None, ge=0, le=10)
    away_red_cards: int | None = Field(default=None, ge=0, le=10)
    home_shots: int | None = Field(default=None, ge=0, le=50)
    away_shots: int | None = Field(default=None, ge=0, le=50)
    scorers: list[ScorerInput] = Field(default_factory=list)
    cards: list[CardInput] = Field(default_factory=list)


class LineupEntry(BaseModel):
    player_id: int
    team_id: int
    started: bool = False
    confirmed: bool = False
    position: str | None = Field(default=None, max_length=50)
    shirt_number: int | None = Field(default=None, ge=1, le=99)
    expected_minutes: float | None = Field(default=None, ge=0, le=130)


class LineupUpsert(BaseModel):
    entries: list[LineupEntry] = Field(min_length=1, max_length=60)

    @field_validator("entries")
    @classmethod
    def players_unique(cls, entries: list[LineupEntry]) -> list[LineupEntry]:
        ids = [entry.player_id for entry in entries]
        if len(ids) != len(set(ids)):
            raise ValueError("No se puede repetir un jugador en la alineación")
        return entries


class LineupOut(ORMModel):
    id: int
    match_id: int
    team_id: int
    player_id: int
    player_name: str | None
    started: bool
    confirmed: bool
    position: str | None
    shirt_number: int | None
    expected_minutes: float | None
    data_source: str
    is_mock_data: bool


class AvailabilityCreate(BaseModel):
    player_id: int
    team_id: int
    description: str | None = Field(default=None, max_length=255)
    reason: str | None = Field(default=None, max_length=255)
    start_date: date | None = None
    end_date: date | None = None
    active: bool = True
    is_mock_data: bool = False


class InjuryOut(ORMModel):
    id: int
    player_id: int
    team_id: int
    description: str | None
    start_date: date | None
    expected_return_date: date | None
    active: bool
    is_mock_data: bool


class SuspensionOut(ORMModel):
    id: int
    player_id: int
    team_id: int
    reason: str | None
    start_date: date | None
    end_date: date | None
    active: bool
    is_mock_data: bool


class TeamStatisticsOut(ORMModel):
    team_id: int
    possession: float | None
    shots: int | None
    shots_on_target: int | None
    corners: int | None
    fouls: int | None
    yellow_cards: int | None
    red_cards: int | None
    xg: float | None
    passes: int | None
    is_mock_data: bool


PredictionType = Literal[
    "match_result",
    "total_goals",
    "both_teams_score",
    "total_cards",
    "likely_scorers",
    "card_risks",
    "other_events",
]


class PredictionRequest(BaseModel):
    match_id: int
    prediction_types: list[PredictionType] | None = None
    use_confirmed_lineups: bool = False


class PredictionMatch(BaseModel):
    id: str
    home_team: str
    away_team: str
    competition: str
    kickoff: datetime
    is_mock_data: bool


class PredictionAnalysis(BaseModel):
    generated_at: datetime
    model_version: str
    data_quality_score: float = Field(ge=0, le=1)
    confidence_score: float = Field(ge=0, le=1)
    confidence_label: str
    confidence_method: Literal["unavailable"]
    probability_calibration_status: Literal["not_calibrated"]
    data_quality_method: Literal["coverage_heuristic"]
    history_cutoff: datetime
    is_mock_data: bool
    historical_sources: list[str]
    latest_historical_match_at: datetime | None
    matches_used: int
    data_age_days: float | None
    manual_fields: dict[str, Any]
    missing_fields: list[str]
    history_contains_mock_data: bool


class MatchResultProbabilities(BaseModel):
    home_win: float = Field(ge=0, le=1)
    draw: float = Field(ge=0, le=1)
    away_win: float = Field(ge=0, le=1)


class GoalsPrediction(BaseModel):
    expected_home_goals: float = Field(ge=0)
    expected_away_goals: float = Field(ge=0)
    over_1_5: float = Field(ge=0, le=1)
    over_2_5: float = Field(ge=0, le=1)
    over_3_5: float = Field(ge=0, le=1)
    under_4_5: float = Field(ge=0, le=1)
    both_teams_score: float = Field(ge=0, le=1)
    over_0_5: float | None = Field(default=None, ge=0, le=1)
    over_4_5: float | None = Field(default=None, ge=0, le=1)
    under_0_5: float | None = Field(default=None, ge=0, le=1)
    under_1_5: float | None = Field(default=None, ge=0, le=1)
    under_2_5: float | None = Field(default=None, ge=0, le=1)
    under_3_5: float | None = Field(default=None, ge=0, le=1)


class LikelyScore(BaseModel):
    score: str
    probability: float = Field(ge=0, le=1)


class CardsPrediction(BaseModel):
    expected_total: float | None = Field(default=None, ge=0)
    expected_home: float | None = Field(default=None, ge=0)
    expected_away: float | None = Field(default=None, ge=0)
    over_2_5: float | None = Field(default=None, ge=0, le=1)
    over_3_5: float | None = Field(default=None, ge=0, le=1)
    over_4_5: float | None = Field(default=None, ge=0, le=1)
    over_5_5: float | None = Field(default=None, ge=0, le=1)
    over_6_5: float | None = Field(default=None, ge=0, le=1)
    under_3_5: float | None = Field(default=None, ge=0, le=1)
    under_4_5: float | None = Field(default=None, ge=0, le=1)
    under_5_5: float | None = Field(default=None, ge=0, le=1)
    under_6_5: float | None = Field(default=None, ge=0, le=1)
    available: bool


class ScorerPrediction(BaseModel):
    player_id: str
    player_name: str
    team: str
    probability_if_plays: float = Field(ge=0, le=1)
    probability_to_play: float | None = Field(default=None, ge=0, le=1)
    unconditional_probability: float | None = Field(default=None, ge=0, le=1)
    expected_minutes: float = Field(ge=0, le=130)
    conditional_note: str
    participation_probability_source: Literal["confirmed", "manual_input", "unavailable"]


class CardRiskPrediction(BaseModel):
    player_id: str
    player_name: str
    team: str
    probability_if_plays: float = Field(ge=0, le=1)
    probability_to_play: float | None = Field(default=None, ge=0, le=1)
    unconditional_probability: float | None = Field(default=None, ge=0, le=1)
    participation_probability_source: Literal["confirmed", "manual_input", "unavailable"]


class OtherEventsPrediction(BaseModel):
    expected_corners: float | None = Field(default=None, ge=0)
    expected_shots: float | None = Field(default=None, ge=0)
    expected_shots_on_target: float | None = Field(default=None, ge=0)
    home_scores_first: float | None = Field(default=None, ge=0, le=1)
    first_half_goal: float | None = Field(default=None, ge=0, le=1)
    halftime_result: MatchResultProbabilities | None = None
    home_clean_sheet: float | None = Field(default=None, ge=0, le=1)
    away_clean_sheet: float | None = Field(default=None, ge=0, le=1)
    penalty_awarded: float | None = Field(default=None, ge=0, le=1)
    corners_over_6_5: float | None = Field(default=None, ge=0, le=1)
    corners_over_7_5: float | None = Field(default=None, ge=0, le=1)
    corners_over_8_5: float | None = Field(default=None, ge=0, le=1)
    corners_over_9_5: float | None = Field(default=None, ge=0, le=1)
    corners_over_10_5: float | None = Field(default=None, ge=0, le=1)
    corners_over_11_5: float | None = Field(default=None, ge=0, le=1)
    corners_over_12_5: float | None = Field(default=None, ge=0, le=1)
    corners_under_6_5: float | None = Field(default=None, ge=0, le=1)
    corners_under_7_5: float | None = Field(default=None, ge=0, le=1)
    corners_under_8_5: float | None = Field(default=None, ge=0, le=1)
    corners_under_9_5: float | None = Field(default=None, ge=0, le=1)
    corners_under_10_5: float | None = Field(default=None, ge=0, le=1)
    corners_under_11_5: float | None = Field(default=None, ge=0, le=1)
    corners_under_12_5: float | None = Field(default=None, ge=0, le=1)
    assumptions: dict[str, str] = Field(default_factory=dict)


class KeyFactor(BaseModel):
    factor: str
    impact: Literal[
        "high_positive",
        "medium_positive",
        "low_positive",
        "low_negative",
        "medium_negative",
        "high_negative",
        "neutral",
    ]
    value: float | int | str
    source_feature: str


class PredictionResponse(BaseModel):
    prediction_id: str
    match: PredictionMatch
    analysis: PredictionAnalysis
    match_result: MatchResultProbabilities
    goals: GoalsPrediction
    score_matrix: list[list[float]] | None = None
    likely_scores: list[LikelyScore]
    cards: CardsPrediction
    likely_scorers: list[ScorerPrediction]
    card_risks: list[CardRiskPrediction]
    other_events: OtherEventsPrediction
    key_factors: list[KeyFactor]
    warnings: list[str]
    disclaimer: str
    outcomes: list[PredictionOutcomeOut] | None = None


class PredictionSummary(BaseModel):
    id: str
    match_id: int
    model_name: str
    model_version: str
    confidence: float
    quality_score: float
    generated_at: datetime
    status: str
    is_mock_data: bool


class PredictionOutcomeOut(BaseModel):
    event_type: str
    predicted_probability: float | None = None
    predicted_value: dict[str, Any] | None = None
    actual_value: dict[str, Any] | None = None
    status: str
    evaluated_at: datetime | None = None


class PredictionEvaluationRequest(BaseModel):
    home_score: int = Field(ge=0, le=30)
    away_score: int = Field(ge=0, le=30)


class ModelMetricOut(ORMModel):
    metric_name: str
    value: float
    split: str
    scope: str
    sample_size: int | None
    calibration_bins: list[dict[str, Any]] | None


class ModelVersionOut(ORMModel):
    name: str
    version: str
    trained_at: datetime
    max_data_date: datetime | None
    features: list[str]
    hyperparameters: dict[str, Any]
    data_sources: list[str]
    status: str
    is_mock_data: bool


class PlayerMatchImportItem(BaseModel):
    match_id: int
    player_name: str = Field(..., min_length=1, max_length=150)
    team: str = Field(..., min_length=1, max_length=150)
    started: bool = True
    minutes_played: int | None = Field(default=None, ge=0, le=130)
    position: str | None = Field(default=None, max_length=50)
    goals: int = 0
    assists: int = 0
    shots: int = 0
    shots_on_target: int = 0
    xg: float | None = None
    yellow_cards: int = 0
    red_cards: int = 0
    fouls: int = 0


class PlayerMatchImportOut(BaseModel):
    imported: int
    errors: list[str]


class MatchEventImportItem(BaseModel):
    match_id: int
    event_type: str = Field(..., max_length=50)
    team: str | None = Field(default=None, max_length=150)
    player_name: str | None = Field(default=None, max_length=150)
    minute: int | None = Field(default=None, ge=0, le=130)
    second: int | None = Field(default=None, ge=0, le=59)
    period: int | None = Field(default=None, ge=1, le=2)
    payload: dict[str, Any] = Field(default_factory=dict)


class MatchEventImportOut(BaseModel):
    imported: int
    errors: list[str]


class ImportRunOut(ORMModel):
    id: str
    source: str
    original_filename: str | None
    file_hash: str | None
    status: str
    started_at: datetime
    completed_at: datetime | None
    downloaded_records: int
    valid_records: int
    rejected_records: int
    errors: list[dict[str, Any]]
    duration_seconds: float | None
    pipeline_version: str
    is_mock_data: bool


class OpenFootballMetrics(BaseModel):
    files_scanned: int = 0
    matches_found: int = 0
    finished_matches: int = 0
    scheduled_matches: int = 0
    teams_found: int = 0
    competitions_found: int = 0
    duplicates: int = 0
    conflicts: int = 0
    errors: int = 0
    catalog_files_scanned: int = 0
    catalog_records_found: int = 0
    catalog_records_imported: int = 0
    leagues_found: int = 0
    clubs_found: int = 0
    players_found: int = 0


class OpenFootballDetection(BaseModel):
    dataset_type: str
    source_repository: str
    country: str | None = None
    root_name: str
    total_bytes: int
    content_hash: str
    files_scanned: int
    sample_files: list[str] = Field(default_factory=list)
    competitions: list[str] = Field(default_factory=list)
    seasons: list[str] = Field(default_factory=list)


class OpenFootballFileErrorOut(BaseModel):
    source_file: str = ""
    code: str
    message: str
    line: int | None = None
    row: int | None = None


class OpenFootballMatchPreview(BaseModel):
    source_match_id: str | None = None
    source_file: str | None = None
    source_repository: str | None = None
    source_line: int | None = None
    competition: str | None = None
    season: str | None = None
    round: str | None = None
    matchday: int | None = None
    date: str | None = None
    kickoff_time: str | None = None
    home_team: str
    away_team: str
    status: str
    fulltime_home_goals: int | None = None
    fulltime_away_goals: int | None = None
    halftime_home_goals: int | None = None
    halftime_away_goals: int | None = None
    extra_time_home_goals: int | None = None
    extra_time_away_goals: int | None = None
    penalty_home_goals: int | None = None
    penalty_away_goals: int | None = None
    aggregate_home_goals: int | None = None
    aggregate_away_goals: int | None = None
    final_home_goals: int | None = None
    final_away_goals: int | None = None
    leg: str | None = None
    group: str | None = None
    venue: str | None = None
    attendance: int | None = None
    notes: str | None = None


class OpenFootballQualityOut(BaseModel):
    competition: str
    competition_id: int | None = None
    source_repository: str
    first_match_date: str | None = None
    last_match_date: str | None = None
    total_matches: int
    finished_matches: int
    scheduled_matches: int
    seasons_available: list[str] = Field(default_factory=list)
    fields_available: list[str] = Field(default_factory=list)
    last_imported_at: str | None = None
    data_categories: dict[str, bool] = Field(default_factory=dict)
    coverage_status: str = "unknown"
    missing_categories: list[str] = Field(default_factory=list)


class OpenFootballImportOut(BaseModel):
    import_id: str
    status: str
    detection: OpenFootballDetection
    metrics: OpenFootballMetrics
    preview_matches: list[OpenFootballMatchPreview] = Field(default_factory=list)
    quality_by_competition: list[OpenFootballQualityOut] = Field(default_factory=list)
    errors: list[OpenFootballFileErrorOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class OpenFootballConflictCandidateOut(BaseModel):
    id: int
    name: str | None = None
    available: bool


class OpenFootballEntityConflictOut(BaseModel):
    id: int
    entity_type: str
    source_name: str
    normalized_name: str
    candidate_ids: list[int]
    candidates: list[OpenFootballConflictCandidateOut]
    best_score: float | None = None
    status: str
    source_repository: str | None = None
    scope_status: Literal[
        "exact", "legacy_single_mapping", "legacy_ambiguous", "missing_mapping"
    ]
    source_repositories: list[str] = Field(default_factory=list)
    potential_source_repositories: list[str] = Field(default_factory=list)
    mapping_ids: list[int] = Field(default_factory=list)
    resolution_notes: str | None = None
    created_at: datetime
    updated_at: datetime


class OpenFootballMatchConflictFieldOut(BaseModel):
    field: str
    existing: Any
    incoming: Any


class OpenFootballMatchConflictOut(BaseModel):
    id: int
    match_id: int
    source_repository: str
    source_record_id: str
    source_file: str | None = None
    conflict_status: str
    fields: list[OpenFootballMatchConflictFieldOut]
    imported_at: datetime
    created_at: datetime
    updated_at: datetime


class OpenFootballPendingConflictsOut(BaseModel):
    entity_conflicts: list[OpenFootballEntityConflictOut] = Field(default_factory=list)
    match_conflicts: list[OpenFootballMatchConflictOut] = Field(default_factory=list)


class OpenFootballEntityConflictResolutionRequest(BaseModel):
    candidate_id: int = Field(gt=0)
    notes: str | None = Field(default=None, max_length=2_000)


class OpenFootballEntityConflictResolutionOut(BaseModel):
    id: int
    entity_type: str
    status: Literal["resolved"]
    selected_candidate_id: int
    selected_candidate_name: str
    updated_mapping_ids: list[int]
    source_repositories: list[str]
    manually_verified: Literal[True]
    resolution_notes: str
    resolved_at: datetime


class OpenFootballMatchConflictResolutionRequest(BaseModel):
    decisions: dict[str, Literal["existing", "incoming"]] = Field(
        min_length=1, max_length=50
    )
    notes: str | None = Field(default=None, max_length=2_000)


class OpenFootballMatchConflictResolutionOut(BaseModel):
    id: int
    match_id: int
    conflict_status: Literal["resolved"]
    decisions: dict[str, Literal["existing", "incoming"]]
    applied_incoming_fields: list[str]
    kept_existing_fields: list[str]
    resolution: dict[str, Any]
    updated_at: datetime


class HealthOut(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    database: Literal["ok"]
    mode: Literal["offline"]
    timestamp: datetime
