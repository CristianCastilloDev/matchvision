"""Leakage-safe pre-match feature engineering.

Features for every kickoff timestamp are calculated from an immutable snapshot;
results at that timestamp are appended only after *all* feature rows in the group
have been produced.  This strict ordering also prevents same-time matches from
leaking into one another.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any


WINDOWS = (3, 5, 10)
METRICS = (
    "goals_for",
    "goals_against",
    "shots",
    "shots_on_target",
    "corners",
    "cards",
    "fouls",
    "points",
    "clean_sheet",
    "scored",
    "over_2_5",
    "both_teams_scored",
)
INVALID_STATUSES = {
    "ABANDONED",
    "CANCELLED",
    "CANCELED",
    "POSTPONED",
    "SUSPENDED",
    "VOID",
}
COMPLETED_STATUSES = {"AWARDED", "COMPLETE", "COMPLETED", "FINISHED", "FT"}


class FeatureEngineeringError(ValueError):
    """The match history cannot produce a reliable feature table."""


class TemporalLeakageError(FeatureEngineeringError):
    """An audit timestamp shows information at/after kickoff was used."""


@dataclass(frozen=True, slots=True)
class TeamPerformance:
    kickoff: datetime
    venue: str
    goals_for: float
    goals_against: float
    shots: float | None
    shots_on_target: float | None
    corners: float | None
    cards: float | None
    fouls: float | None
    points: float
    clean_sheet: float
    scored: float
    over_2_5: float
    both_teams_scored: float
    data_source: str
    is_mock_data: bool


@dataclass(slots=True)
class _LeagueHistory:
    matches: int = 0
    home_goals: float = 0.0
    away_goals: float = 0.0
    data_sources: set[str] = field(default_factory=set)
    contains_mock_data: bool = False

    @property
    def average_home_goals(self) -> float | None:
        return self.home_goals / self.matches if self.matches else None

    @property
    def average_away_goals(self) -> float | None:
        return self.away_goals / self.matches if self.matches else None

    @property
    def average_team_goals(self) -> float | None:
        if not self.matches:
            return None
        return (self.home_goals + self.away_goals) / (2 * self.matches)


@dataclass(frozen=True, slots=True)
class FeatureBuildResult:
    rows: tuple[dict[str, Any], ...]
    feature_names: tuple[str, ...]
    generated_matches: int
    completed_matches_used: int

    def to_records(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows]

    def to_dataframe(self) -> Any:
        """Create a pandas DataFrame only when pandas is installed."""

        try:
            import pandas as pd  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("pandas is required for to_dataframe()") from exc
        return pd.DataFrame(self.to_records())


def _as_records(matches: Any) -> list[dict[str, Any]]:
    if hasattr(matches, "to_dict") and not isinstance(matches, Mapping):
        try:
            converted = matches.to_dict(orient="records")
            if isinstance(converted, list):
                return [dict(row) for row in converted]
        except TypeError:
            pass
    if isinstance(matches, Mapping):
        raise FeatureEngineeringError("matches must be an iterable of row mappings")
    try:
        records = [dict(row) for row in matches]
    except (TypeError, ValueError) as exc:
        raise FeatureEngineeringError("matches must contain mapping-like rows") from exc
    return records


def _datetime(value: Any, field: str = "match_date") -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise FeatureEngineeringError(f"Invalid {field}: {value!r}") from exc
    else:
        raise FeatureEngineeringError(f"Missing {field}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value: Any, *, nonnegative: bool = True) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (nonnegative and number < 0):
        return None
    return number


def _first(row: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _team_key(row: Mapping[str, Any], side: str) -> str:
    value = _first(
        row,
        (
            f"{side}_team_id",
            f"{side}_team_external_id",
            f"{side}_team_name",
            f"{side}_team",
        ),
    )
    if value in (None, ""):
        raise FeatureEngineeringError(f"Missing {side} team identity")
    return str(value).strip()


def _competition_key(row: Mapping[str, Any]) -> str:
    value = _first(
        row,
        (
            "competition_id",
            "competition_external_id",
            "competition_name",
            "competition",
        ),
    )
    return str(value).strip() if value not in (None, "") else "__all__"


def _score(row: Mapping[str, Any], side: str) -> float | None:
    return _number(
        _first(row, (f"{side}_goals", f"{side}_score", f"fulltime_{side}_score"))
    )


def _side_metric(row: Mapping[str, Any], side: str, metric: str) -> float | None:
    candidates: dict[str, tuple[str, ...]] = {
        "shots": (f"{side}_shots",),
        "shots_on_target": (f"{side}_shots_on_target",),
        "corners": (f"{side}_corners",),
        "fouls": (f"{side}_fouls",),
        "cards": (f"{side}_cards",),
    }
    direct = _number(_first(row, candidates.get(metric, ())))
    if direct is not None or metric != "cards":
        return direct
    yellow = _number(row.get(f"{side}_yellow_cards"))
    red = _number(row.get(f"{side}_red_cards"))
    if yellow is None and red is None:
        return None
    return (yellow or 0.0) + (red or 0.0)


def _mean(values: Iterable[float | None]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    return sum(usable) / len(usable) if usable else None


def _weighted_mean(values: Sequence[float | None], decay: float = 0.82) -> float | None:
    weighted_sum = 0.0
    total_weight = 0.0
    # values are oldest -> newest, so the newest observation receives weight 1.
    for distance, value in enumerate(reversed(values)):
        if value is None:
            continue
        weight = decay**distance
        weighted_sum += float(value) * weight
        total_weight += weight
    return weighted_sum / total_weight if total_weight else None


def _metric(performance: TeamPerformance, name: str) -> float | None:
    return getattr(performance, name)


def _result_points(goals_for: float, goals_against: float) -> float:
    if goals_for > goals_against:
        return 3.0
    if goals_for == goals_against:
        return 1.0
    return 0.0


def _quality(history_count: int, known_values: int, expected_values: int) -> float:
    volume = min(history_count / 10.0, 1.0)
    completeness = known_values / expected_values if expected_values else 0.0
    return round(max(0.0, min(1.0, 0.65 * volume + 0.35 * completeness)), 6)


def _strength(value: float | None, baseline: float | None) -> float | None:
    if value is None or baseline is None or baseline <= 0:
        return None
    return value / baseline


def _history_features(
    prefix: str,
    history: Sequence[TeamPerformance],
    *,
    venue: str,
    kickoff: datetime,
) -> dict[str, Any]:
    output: dict[str, Any] = {
        f"{prefix}_history_matches": len(history),
        f"{prefix}_days_rest": None,
        f"{prefix}_elo": None,
    }
    if history:
        output[f"{prefix}_days_rest"] = max(
            0.0, (kickoff - history[-1].kickoff).total_seconds() / 86400.0
        )
    for window in WINDOWS:
        recent = history[-window:]
        for metric_name in METRICS:
            output[f"{prefix}_{metric_name}_avg_{window}"] = _mean(
                _metric(item, metric_name) for item in recent
            )
    recent_ten = history[-10:]
    for metric_name in (
        "goals_for",
        "goals_against",
        "shots_on_target",
        "corners",
        "cards",
        "points",
    ):
        output[f"{prefix}_{metric_name}_recency_weighted_10"] = _weighted_mean(
            [_metric(item, metric_name) for item in recent_ten]
        )
    venue_history = [item for item in history if item.venue == venue][-5:]
    output[f"{prefix}_venue_goals_for_avg_5"] = _mean(
        item.goals_for for item in venue_history
    )
    output[f"{prefix}_venue_goals_against_avg_5"] = _mean(
        item.goals_against for item in venue_history
    )
    known = sum(
        value is not None
        for item in recent_ten
        for value in (item.shots, item.shots_on_target, item.corners, item.cards, item.fouls)
    )
    output[f"{prefix}_data_completeness"] = (
        known / (len(recent_ten) * 5) if recent_ten else 0.0
    )
    output[f"{prefix}_history_max_date"] = (
        history[-1].kickoff.isoformat() if history else None
    )
    return output


def _performance(
    row: Mapping[str, Any], kickoff: datetime, side: str, opponent: str
) -> TeamPerformance:
    goals_for = _score(row, side)
    goals_against = _score(row, opponent)
    if goals_for is None or goals_against is None:  # guarded before call
        raise FeatureEngineeringError("Completed result is missing goals")
    total = goals_for + goals_against
    return TeamPerformance(
        kickoff=kickoff,
        venue=side,
        goals_for=goals_for,
        goals_against=goals_against,
        shots=_side_metric(row, side, "shots"),
        shots_on_target=_side_metric(row, side, "shots_on_target"),
        corners=_side_metric(row, side, "corners"),
        cards=_side_metric(row, side, "cards"),
        fouls=_side_metric(row, side, "fouls"),
        points=_result_points(goals_for, goals_against),
        clean_sheet=float(goals_against == 0),
        scored=float(goals_for > 0),
        over_2_5=float(total > 2.5),
        both_teams_scored=float(goals_for > 0 and goals_against > 0),
        data_source=str(row.get("data_source") or "unknown"),
        is_mock_data=bool(row.get("is_mock_data", False)),
    )


def _is_completed(row: Mapping[str, Any]) -> bool:
    status = str(row.get("status") or "").strip().upper()
    if status in INVALID_STATUSES:
        return False
    if status and status not in COMPLETED_STATUSES:
        return False
    if bool(row.get("is_shootout")):
        return False
    return _score(row, "home") is not None and _score(row, "away") is not None


def _elo_expected(rating: float, opponent: float, home_advantage: float = 65.0) -> float:
    return 1.0 / (1.0 + 10 ** ((opponent - (rating + home_advantage)) / 400.0))


def _elo_result(home_goals: float, away_goals: float) -> float:
    if home_goals > away_goals:
        return 1.0
    if home_goals == away_goals:
        return 0.5
    return 0.0


def _feature_names(rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    if not rows:
        return ()
    excluded = {
        "match_id",
        "match_date",
        "home_team_key",
        "away_team_key",
        "competition_key",
        "data_source",
        "historical_sources",
        "history_contains_mock_data",
        "is_mock_data",
        "target_home_goals",
        "target_away_goals",
    }
    return tuple(
        sorted(
            key
            for key in rows[0]
            if key not in excluded
            and not key.startswith("_")
            and not key.endswith("_history_max_date")
            and isinstance(rows[0].get(key), (int, float, type(None)))
        )
    )


class FeatureBuilder:
    """Build deterministic team features using only matches before kickoff."""

    def __init__(self, *, elo_k_factor: float = 20.0, initial_elo: float = 1500.0) -> None:
        if elo_k_factor <= 0:
            raise ValueError("elo_k_factor must be positive")
        self.elo_k_factor = float(elo_k_factor)
        self.initial_elo = float(initial_elo)

    def build_result(self, matches: Any) -> FeatureBuildResult:
        records = _as_records(matches)
        prepared: list[tuple[datetime, int, dict[str, Any]]] = []
        seen_ids: set[str] = set()
        for index, row in enumerate(records):
            kickoff = _datetime(
                _first(row, ("match_date", "kickoff", "date")), "match_date"
            )
            identifier = str(
                _first(row, ("id", "match_id", "external_id")) or f"row-{index}"
            )
            if identifier in seen_ids and not identifier.startswith("row-"):
                raise FeatureEngineeringError(f"Duplicate match id: {identifier}")
            seen_ids.add(identifier)
            copied = dict(row)
            copied["__match_identifier"] = identifier
            prepared.append((kickoff, index, copied))
        prepared.sort(key=lambda item: (item[0], item[1]))

        histories: defaultdict[tuple[str, str], list[TeamPerformance]] = defaultdict(list)
        ratings: defaultdict[tuple[str, str], float] = defaultdict(
            lambda: self.initial_elo
        )
        leagues: defaultdict[str, _LeagueHistory] = defaultdict(_LeagueHistory)
        output: list[dict[str, Any]] = []
        completed_used = 0
        cursor = 0
        while cursor < len(prepared):
            kickoff = prepared[cursor][0]
            end = cursor + 1
            while end < len(prepared) and prepared[end][0] == kickoff:
                end += 1
            group = prepared[cursor:end]

            # Phase 1: all rows see exactly the state from strictly earlier dates.
            for _, _, row in group:
                home = _team_key(row, "home")
                away = _team_key(row, "away")
                competition = _competition_key(row)
                home_history = histories[(competition, home)]
                away_history = histories[(competition, away)]
                league = leagues[competition]
                features: dict[str, Any] = {
                    "match_id": row["__match_identifier"],
                    "match_date": kickoff.isoformat(),
                    "home_team_key": home,
                    "away_team_key": away,
                    "competition_key": competition,
                    "data_source": str(row.get("data_source") or "unknown"),
                    "is_mock_data": bool(row.get("is_mock_data", False)),
                    "_feature_cutoff_at": kickoff.isoformat(),
                    "league_prior_matches": league.matches,
                    "league_home_goals_avg": league.average_home_goals,
                    "league_away_goals_avg": league.average_away_goals,
                    "league_team_goals_avg": league.average_team_goals,
                }
                features.update(
                    _history_features("home", home_history, venue="home", kickoff=kickoff)
                )
                features.update(
                    _history_features("away", away_history, venue="away", kickoff=kickoff)
                )
                home_elo = ratings[(competition, home)]
                away_elo = ratings[(competition, away)]
                features["home_elo"] = home_elo
                features["away_elo"] = away_elo
                features["elo_difference"] = home_elo - away_elo
                features["home_elo_expected_result"] = _elo_expected(home_elo, away_elo)

                home_recent_for = features.get("home_goals_for_avg_5")
                away_recent_for = features.get("away_goals_for_avg_5")
                features["home_attack_strength"] = _strength(
                    home_recent_for, league.average_team_goals
                )
                features["away_attack_strength"] = _strength(
                    away_recent_for, league.average_team_goals
                )
                features["home_defence_weakness"] = _strength(
                    features.get("home_goals_against_avg_5"),
                    league.average_team_goals,
                )
                features["away_defence_weakness"] = _strength(
                    features.get("away_goals_against_avg_5"),
                    league.average_team_goals,
                )
                known = int(features["home_data_completeness"] * min(len(home_history), 10) * 5)
                known += int(features["away_data_completeness"] * min(len(away_history), 10) * 5)
                expected = (min(len(home_history), 10) + min(len(away_history), 10)) * 5
                features["feature_quality_score"] = _quality(
                    min(len(home_history), len(away_history)), known, expected
                )
                historical_sources = {
                    item.data_source for item in (*home_history, *away_history)
                } | league.data_sources
                features["historical_sources"] = sorted(historical_sources)
                features["history_contains_mock_data"] = league.contains_mock_data or any(
                    item.is_mock_data for item in (*home_history, *away_history)
                )
                if _is_completed(row):
                    features["target_home_goals"] = _score(row, "home")
                    features["target_away_goals"] = _score(row, "away")
                output.append(features)

            # Phase 2: update histories after every same-time row was featurized.
            for _, _, row in group:
                if not _is_completed(row):
                    continue
                home = _team_key(row, "home")
                away = _team_key(row, "away")
                competition = _competition_key(row)
                histories[(competition, home)].append(
                    _performance(row, kickoff, "home", "away")
                )
                histories[(competition, away)].append(
                    _performance(row, kickoff, "away", "home")
                )
                home_goals = _score(row, "home")
                away_goals = _score(row, "away")
                if home_goals is None or away_goals is None:  # defensive
                    continue
                league = leagues[competition]
                league.matches += 1
                league.home_goals += home_goals
                league.away_goals += away_goals
                league.data_sources.add(str(row.get("data_source") or "unknown"))
                league.contains_mock_data = league.contains_mock_data or bool(
                    row.get("is_mock_data", False)
                )

                old_home = ratings[(competition, home)]
                old_away = ratings[(competition, away)]
                expected_home = _elo_expected(old_home, old_away)
                result_home = _elo_result(home_goals, away_goals)
                adjustment = self.elo_k_factor * (result_home - expected_home)
                ratings[(competition, home)] = old_home + adjustment
                ratings[(competition, away)] = old_away - adjustment
                completed_used += 1
            cursor = end

        assert_no_temporal_leakage(output)
        return FeatureBuildResult(
            rows=tuple(output),
            feature_names=_feature_names(output),
            generated_matches=len(output),
            completed_matches_used=completed_used,
        )

    def build(self, matches: Any) -> list[dict[str, Any]]:
        """Return feature rows as plain dictionaries (pandas is not required)."""

        return self.build_result(matches).to_records()

    def build_for_match(
        self,
        matches: Any,
        *,
        target_date: str | date | datetime,
        home_team_id: str | int,
        away_team_id: str | int,
        competition: str | int | None = None,
    ) -> dict[str, Any]:
        """Build one future fixture from history strictly before ``target_date``."""

        cutoff = _datetime(target_date, "target_date")
        records = _as_records(matches)
        prior: list[dict[str, Any]] = []
        for row in records:
            row_date = _datetime(
                _first(row, ("match_date", "kickoff", "date")), "match_date"
            )
            if row_date < cutoff:
                prior.append(row)
        selected_competition = competition
        if selected_competition is None:
            candidate_competitions: set[str] = set()
            requested_teams = {str(home_team_id), str(away_team_id)}
            for row in prior:
                try:
                    row_teams = {_team_key(row, "home"), _team_key(row, "away")}
                except FeatureEngineeringError:
                    continue
                if row_teams & requested_teams:
                    candidate_competitions.add(_competition_key(row))
            if len(candidate_competitions) == 1:
                selected_competition = next(iter(candidate_competitions))
        target_id = "__prediction_target__"
        prior.append(
            {
                "id": target_id,
                "match_date": cutoff.isoformat(),
                "home_team_id": home_team_id,
                "away_team_id": away_team_id,
                "competition_id": selected_competition or "__all__",
                "status": "SCHEDULED",
            }
        )
        rows = self.build(prior)
        target = next(row for row in rows if row["match_id"] == target_id)
        assert_no_temporal_leakage([target])
        return target


PrematchFeatureBuilder = FeatureBuilder
TimeAwareFeatureBuilder = FeatureBuilder


def build_prematch_features(matches: Any) -> list[dict[str, Any]]:
    return FeatureBuilder().build(matches)


def assert_no_temporal_leakage(rows: Iterable[Mapping[str, Any]]) -> None:
    """Fail if any feature audit timestamp is not strictly before kickoff."""

    for position, row in enumerate(rows):
        cutoff = _datetime(
            row.get("_feature_cutoff_at") or row.get("match_date"),
            "_feature_cutoff_at",
        )
        for key in ("home_history_max_date", "away_history_max_date"):
            raw = row.get(key)
            if raw in (None, ""):
                continue
            source_date = _datetime(raw, key)
            if source_date >= cutoff:
                raise TemporalLeakageError(
                    f"row {position}: {key}={source_date.isoformat()} is not before "
                    f"kickoff={cutoff.isoformat()}"
                )


__all__ = [
    "FeatureBuildResult",
    "FeatureBuilder",
    "FeatureEngineeringError",
    "PrematchFeatureBuilder",
    "TemporalLeakageError",
    "TimeAwareFeatureBuilder",
    "assert_no_temporal_leakage",
    "build_prematch_features",
]
