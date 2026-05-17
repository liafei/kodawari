from kodawari.patterns import (
    APIEndpointPattern,
    CRUDPattern,
    PatternRegistry,
    RankingRulesPattern,
    SchemaMigrationPattern,
)


def test_pattern_registry_matches_domain_patterns() -> None:
    registry = PatternRegistry(
        [
            CRUDPattern(),
            APIEndpointPattern(),
            RankingRulesPattern(),
            SchemaMigrationPattern(),
        ]
    )

    ranking = registry.analyze(
        task_id="T010",
        task_label="T010: Implement ranking rules",
        task_scope="sort recommendations by weighted score",
        requirements="ranking logic with score normalization",
    )
    assert ranking[0].pattern_id == "ranking-rules"
    assert "Normalize scores before sorting." in ranking[0].checklist

    migration = registry.analyze(
        task_id="T003",
        task_label="T003: Add column via schema migration",
        task_scope="alter table daily_editions add column rank_score",
        requirements="backward compatible migration",
    )
    assert migration[0].pattern_id == "schema-migration"
    assert "test_*migration*.py" in migration[0].verify_hints


def test_api_pattern_matches_endpoint_work() -> None:
    suggestion = APIEndpointPattern().analyze(
        task_id="T004",
        task_label="T004: Add API endpoint",
        task_scope="create route for GET /api/v1/daily",
        requirements="",
    )
    assert suggestion is not None
    assert suggestion.pattern_id == "api-endpoint"
    hint = suggestion.to_hint()
    assert hint["pattern_id"] == "api-endpoint"
    assert "checklist" in hint
