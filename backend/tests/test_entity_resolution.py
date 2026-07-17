from app.services.entity_resolution import normalize_entity_name, resolve_name


def test_name_normalization_removes_accents_and_spaces() -> None:
    assert normalize_entity_name("  Atlético   Unión  ") == "atletico union"


def test_ambiguous_fuzzy_match_requires_review() -> None:
    result = resolve_name("Manchester", [(1, "Manchester United"), (2, "Manchester City")])
    assert result.entity_id is None
    assert result.status in {"manual_review", "unmatched"}


def test_exact_alias_candidate_is_safe() -> None:
    result = resolve_name("Man Utd", [(1, "Man Utd"), (2, "Manchester City")])
    assert result.status == "exact"
    assert result.entity_id == 1
