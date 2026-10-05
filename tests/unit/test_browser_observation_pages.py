"""Browser controls must remain reachable after generic tool-output admission."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.application.artifact_writer import ArtifactWriterFactory
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserElement,
    BrowserObservation,
    BrowserProviderError,
)
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import TrustLevel
from agent_core.domain.tool_output import content_bytes
from agent_core.tools.browser_observe import BrowserObserveTool
from agent_core.tools.executor import ToolPipeline
from agent_core.tools.registry import StaticToolRegistry
from agent_core.tools.validation import validate_output
from tests.contract.support import principal, run, tool_context
from tests.unit.test_browser_playwright import _ref, lesson_pages
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_history_cache_window import _factory


class ComposerProvider(FakeBrowserProvider):
    """Model a large composer whose publication control lies beyond the first page."""

    def _observation(self, url: str) -> BrowserObservation:
        """Produce fresh references, many controls, and text requiring byte-aware paging."""
        revision = f"revision-{self.observation_count}"
        return BrowserObservation(
            url=url,
            title="Compose",
            revision=revision,
            text='Draft with Unicode 雪 and escaped \\" text.\n' * 300,
            elements=tuple(
                BrowserElement(
                    ref=f"{revision}:{index}",
                    role="button",
                    name="Post" if index == 35 else f"Control {index}",
                )
                for index in range(235)
            ),
        )


async def test_composer_controls_are_readable_after_output_admission(tmp_path: Path) -> None:
    """Production lost the enabled Post control in the middle of a 39 KB result."""
    clock, factory = await _factory()
    pipeline = ToolPipeline(
        StaticToolRegistry(),
        factory,
        clock,
        SequenceIdFactory(),
        artifact_writers=ArtifactWriterFactory(
            factory, FilesystemArtifactStore(tmp_path), clock, SequenceIdFactory()
        ),
    )
    provider = ComposerProvider()
    tool = BrowserObserveTool(provider)
    arguments: dict[str, int] = {}
    for _ in range(256):
        result = await tool.execute(arguments, tool_context())
        assert result.ok
        assert len(content_bytes(result.content)) <= 4096, "browser controls exceed model budget"
        admitted = await pipeline._artifactize_large_output(
            result=result, tool=tool, run=run(), principal=principal()
        )
        assert admitted.context_content is None
        assert not admitted.artifacts
        assert isinstance(admitted.content[0], TextPart)
        page = json.loads(admitted.content[0].text)
        post = next((e for e in page["elements"] if e["name"] == "Post"), None)
        if post is not None:
            assert post["disabled"] is False
            assert post["ref"].startswith(page["revision"] + ":")
            assert provider.navigations == []
            return
        assert page["next_element_offset"] > arguments.get("element_offset", 0)
        arguments = {"element_offset": page["next_element_offset"]}
    pytest.fail("Post must be reachable without leaving the composer")


@pytest.mark.parametrize("budget", [1024, 2048, 4096])
async def test_pages_cover_every_control_and_unicode_character(budget: int) -> None:
    """Traverse every control and Unicode character without exceeding admission limits."""
    provider = ComposerProvider()
    tool = BrowserObserveTool(provider, inline_output_bytes=budget)
    seen: list[str] = []
    parts: list[str] = []
    arguments: dict[str, int] = {}
    for _ in range(512):
        result = await tool.execute(arguments, tool_context())
        assert result.ok
        assert result.output_trust is TrustLevel.EXTERNAL_UNTRUSTED
        assert len(content_bytes(result.content)) <= budget
        page = result.structured
        assert page is not None
        validate_output(page, tool.spec.output_schema)
        seen.extend(element["name"] for element in page["elements"])
        parts.append(page["text"])
        next_element = page["next_element_offset"]
        next_text = page["next_text_offset"]
        if next_element is None and next_text is None:
            break
        arguments = {
            "element_offset": next_element if next_element is not None else page["total_elements"],
            "text_offset": next_text if next_text is not None else page["total_text_characters"],
        }
    else:
        pytest.fail("observation pagination must make progress")
    expected = provider._observation("https://example.org/account")
    assert seen == [element.name for element in expected.elements]
    assert "".join(parts) == expected.text
    assert provider.navigations == []
    assert provider.actions == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"element_offset": -1},
        {"element_offset": 257},
        {"element_offset": True},
        {"element_offset": "35"},
        {"text_offset": -1},
        {"text_offset": 262145},
        {"text_offset": 0.5},
        {"profile_id": "foreign"},
    ],
)
async def test_invalid_page_requests_never_reach_the_provider(arguments: dict[str, object]) -> None:
    """Reject malformed offsets and foreign arguments before binding browser authority."""
    provider = ComposerProvider()
    result = await BrowserObserveTool(provider).execute(arguments, tool_context())
    assert not result.ok
    assert result.failure is not None
    assert result.failure.reason_code == "tool.arguments_invalid"
    assert provider.execution_contexts == []
    assert provider.observation_count == 0


async def test_page_display_truncation_preserves_full_provider_labels() -> None:
    """Shorten presentation labels without changing provider facts or element references."""
    original = BrowserObservation(
        url="https://example.org/account",
        revision="current",
        title="雪" * 1024,
        elements=(BrowserElement(ref="current:0", role="button", name="雪" * 1024),),
    )

    class LongLabelProvider(FakeBrowserProvider):
        """Supply an immutable observation with labels larger than the display limits."""

        def _observation(self, url: str) -> BrowserObservation:
            """Return the original full labels so display truncation can be checked separately."""
            return original

    provider = LongLabelProvider()
    result = await BrowserObserveTool(provider).execute({}, tool_context())
    assert result.ok
    page = result.structured
    assert page is not None
    assert page["title_truncated"]
    assert page["elements"][0]["name_truncated"]
    assert page["elements"][0]["name"].endswith("…")
    assert page["elements"][0]["ref"] == "current:0"
    assert original.elements[0].name == "雪" * 1024
    assert original.title == "雪" * 1024


async def test_an_out_of_scope_page_remains_invalid() -> None:
    """Reject observations outside the provider origin policy before exposing a page."""

    class ForeignProvider(FakeBrowserProvider):
        """Simulate a provider returning a page outside its authorized origin."""

        async def observe(self) -> BrowserObservation:
            """Return a foreign-origin observation for the output-boundary regression."""
            return self._observation("https://foreign.example/account")

    result = await BrowserObserveTool(ForeignProvider()).execute({}, tool_context())
    assert not result.ok
    assert result.failure is not None
    assert result.failure.reason_code == "tool.browser.output_invalid"


async def test_a_page_that_shrank_returns_an_empty_terminal_slice() -> None:
    """Terminate stale offsets safely when the fresh page has fewer elements or text."""
    result = await BrowserObserveTool(FakeBrowserProvider()).execute(
        {"element_offset": 256, "text_offset": 262144}, tool_context()
    )
    assert result.ok
    page = result.structured
    assert page is not None
    assert page["elements"] == [] and page["text"] == ""
    assert page["next_element_offset"] is page["next_text_offset"] is None


async def test_oversized_page_identity_fails_instead_of_losing_references() -> None:
    """Fail explicitly when page identity alone exceeds the configured byte budget."""

    class LongURLProvider(FakeBrowserProvider):
        """Supply an allowed-origin URL too large to fit in the smallest page budget."""

        async def observe(self) -> BrowserObservation:
            """Return oversized identity metadata without changing the allowed origin."""
            return self._observation("https://example.org/" + "x" * 2000)

    result = await BrowserObserveTool(LongURLProvider(), inline_output_bytes=1024).execute(
        {}, tool_context()
    )
    assert not result.ok
    assert result.failure is not None
    assert result.failure.reason_code == "tool.browser.output_invalid"


async def test_real_composer_can_use_a_publication_control_from_a_later_page() -> None:
    """Reach a later-page Post control in Chromium while still rejecting stale refs."""
    controls = "".join(f"<button>Control {index}</button>" for index in range(34))
    timeline = "".join(f"<button>Timeline {index}</button>" for index in range(180))
    html = (
        "<!doctype html><title>Compose</title>"
        + controls
        + '<div contenteditable="true" role="textbox" aria-label="Post text">Draft</div>'
        + '<button onclick="window.published++">Post</button>'
        + timeline
        + "<script>window.published=0;</script>"
    )
    async with lesson_pages({"/compose/post": html}) as (runtime, visit, left):
        before = await visit("/compose/post")
        typed = await runtime.act(
            BrowserAction(
                kind=BrowserActionKind.TYPE,
                expected_revision=before.revision,
                ref=_ref(before, "Post text"),
                value="Ready to publish",
            )
        )

        class RuntimeProvider(FakeBrowserProvider):
            """Adapt the local Chromium fixture to the observation tool contract."""

            async def observe(self) -> BrowserObservation:
                """Refresh the real page so each pagination call receives current references."""
                return await runtime.observe()

        provider = RuntimeProvider(allowed_origins=runtime._allowed_origins)
        tool = BrowserObserveTool(provider)
        offset = 0
        for _ in range(256):
            result = await tool.execute({"element_offset": offset}, tool_context())
            assert result.ok and result.structured is not None
            assert len(content_bytes(result.content)) <= 4096
            page = result.structured
            post = next((e for e in page["elements"] if e["name"] == "Post"), None)
            if post is not None:
                break
            assert page["next_element_offset"] > offset
            offset = page["next_element_offset"]
        else:
            pytest.fail("publication control is unreachable")
        assert offset > 0
        assert await runtime._current_page().evaluate("window.published") == 0
        # Old refs remain invalid; only an explicitly dispatched action on the
        # newly returned revision can publish to this controlled local page.
        with pytest.raises(BrowserProviderError) as stale:
            await runtime.act(
                BrowserAction(
                    kind=BrowserActionKind.CLICK,
                    expected_revision=typed.revision,
                    ref=_ref(typed, "Post"),
                )
            )
        assert stale.value.reason_code == "tool.browser.page_changed"
        await runtime.act(
            BrowserAction(
                kind=BrowserActionKind.CLICK,
                expected_revision=page["revision"],
                ref=post["ref"],
            )
        )
        assert await runtime._current_page().evaluate("window.published") == 1
        assert left == []
