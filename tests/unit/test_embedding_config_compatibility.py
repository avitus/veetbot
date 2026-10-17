"""Current-only indexing retains its persisted pre-history configuration identity."""

import pytest

from agent_core.domain.semantic_work import EmbeddingConfig
from tests.contract.semantic_work_cases import CONFIG

# Serialized by the delivered current-only configuration before ADR-0200.
LEGACY_JSON = (
    '{"space":{"provider":"fixture","model":"semantic","revision":"v1",'
    '"dimensions":3,"text_version":"memory-text@1"},"usd_per_million_tokens":"1",'
    '"pricing_revision":"test@1","egress_revision":"test@1",'
    '"residency_provider":"fixture","request_usd":"0.1","daily_usd":"0.15"}'
)
LEGACY_KEY = "92ed35aa3e222cbda9f3e68f342e70c75e65161a5255b698326ffa654fc91764"


@pytest.mark.parametrize("explicit_default", [False, True])
def test_current_only_configuration_keeps_the_legacy_identity(explicit_default: bool) -> None:
    config = EmbeddingConfig.model_validate_json(LEGACY_JSON)
    if explicit_default:
        config = config.model_copy(update={"historical_indexing": False})
    assert config == CONFIG
    assert config.key == LEGACY_KEY


def test_historical_indexing_has_a_distinct_explicit_identity() -> None:
    historical = CONFIG.model_copy(update={"historical_indexing": True})
    assert historical.key != LEGACY_KEY
    assert historical.key != CONFIG.key
    assert historical.model_dump()["historical_indexing"] is True
    assert EmbeddingConfig.model_validate_json(historical.model_dump_json()).key == historical.key
