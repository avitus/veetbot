"""The knowledge arm's provider: the memory index's, or OpenAI on its own (ADR-0290)."""

from __future__ import annotations

import logging

import pytest

from agent_core.bootstrap import _knowledge_embedder
from agent_core.config import ConfigurationError, load_settings
from agent_core.domain.credentials import CredentialRef, SecretValue
from agent_core.domain.knowledge import KNOWLEDGE_OPENAI_SPACE
from agent_core.domain.memory import Sensitivity
from tests.unit.test_config import base_environment


class Resolver:
    def __init__(self, key: str | None) -> None:
        self._key = key

    async def resolve(self, reference: CredentialRef) -> SecretValue:
        if reference != CredentialRef("openai") or self._key is None:
            raise PermissionError("no such credential")
        return SecretValue(self._key)


def _settings(**extra: str):  # type: ignore[no-untyped-def]
    return load_settings({**base_environment(), "AGENT_KNOWLEDGE_SEMANTIC_ENABLED": "1", **extra})


def test_settings_parse_the_knowledge_semantic_provider() -> None:
    assert load_settings(base_environment()).knowledge_semantic_provider == "index"
    assert _settings(AGENT_KNOWLEDGE_SEMANTIC_PROVIDER="openai").knowledge_semantic_provider == (
        "openai"
    )
    with pytest.raises(ConfigurationError, match="PROVIDER"):
        _settings(AGENT_KNOWLEDGE_SEMANTIC_PROVIDER="cohere")


async def test_openai_provider_composes_without_the_memory_index() -> None:
    """The arm no longer needs the semantic index: OpenAI is its own remote provider."""

    settings = _settings(AGENT_KNOWLEDGE_SEMANTIC_PROVIDER="openai", VEETBOT_OPENAI_KEY="sk-test")
    embedder = await _knowledge_embedder(settings, None, Resolver("sk-test"))
    assert embedder is not None
    assert embedder.space == KNOWLEDGE_OPENAI_SPACE
    assert embedder.channel == "dense"
    assert embedder.admitted()
    # A remote provider: the memory index's egress rule applies.
    assert embedder.allows(Sensitivity.INTERNAL)
    assert not embedder.allows(Sensitivity.RESTRICTED)


async def test_openai_provider_without_a_key_stays_lexical(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(AGENT_KNOWLEDGE_SEMANTIC_PROVIDER="openai")
    with caplog.at_level(logging.WARNING):
        assert await _knowledge_embedder(settings, None, Resolver(None)) is None
    assert "knowledge_semantic_unavailable_using_lexical" in caplog.text


async def test_index_provider_without_the_index_stays_lexical(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings()
    with caplog.at_level(logging.WARNING):
        assert await _knowledge_embedder(settings, None, Resolver("sk-test")) is None
    assert "knowledge_semantic_unavailable_using_lexical" in caplog.text


async def test_the_arm_off_composes_nothing() -> None:
    settings = load_settings({**base_environment(), "AGENT_KNOWLEDGE_SEMANTIC_PROVIDER": "openai"})
    assert await _knowledge_embedder(settings, None, Resolver("sk-test")) is None


class _Index:
    """The composed semantic index as the knowledge arm sees it."""

    def __init__(self, admitted: bool) -> None:
        self._admitted = admitted
        self.provider = None
        self.space = KNOWLEDGE_OPENAI_SPACE
        self.local_provider = None

    def admitted(self) -> bool:
        return self._admitted


async def test_the_openai_arm_reads_the_index_admission_when_one_is_composed() -> None:
    """Withdrawing the standing files stops the OpenAI knowledge arm too."""

    settings = _settings(AGENT_KNOWLEDGE_SEMANTIC_PROVIDER="openai", VEETBOT_OPENAI_KEY="sk-test")
    index = _Index(admitted=True)
    embedder = await _knowledge_embedder(settings, index, Resolver("sk-test"))  # type: ignore[arg-type]
    assert embedder is not None and embedder.admitted()
    index._admitted = False
    assert not embedder.admitted(), "a withdrawn index withdraws the knowledge arm's egress"


async def test_the_openai_arm_stays_lexical_when_the_enabled_index_was_not_admitted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A withdrawal followed by a restart withdraws the knowledge arm's egress as well."""

    settings = _settings(
        AGENT_KNOWLEDGE_SEMANTIC_PROVIDER="openai",
        AGENT_MEMORY_SEMANTIC_INDEX_ENABLED="1",
        VEETBOT_OPENAI_KEY="sk-test",
    )
    with caplog.at_level(logging.WARNING):
        assert await _knowledge_embedder(settings, None, Resolver("sk-test")) is None
    assert "knowledge_semantic_unavailable_using_lexical" in caplog.text


@pytest.mark.parametrize(
    ("reader", "egress"), [("openai", True), ("anthropic", False), (None, False)]
)
async def test_a_question_goes_only_to_the_provider_that_wrote_it(
    reader: str | None, egress: bool
) -> None:
    """The arm's OpenAI provider embeds questions only when the reader is OpenAI too."""

    settings = _settings(AGENT_KNOWLEDGE_SEMANTIC_PROVIDER="openai", VEETBOT_OPENAI_KEY="sk-test")
    embedder = await _knowledge_embedder(
        settings, None, Resolver("sk-test"), reader_provider=reader
    )
    assert embedder is not None
    assert embedder.question_egress is egress
