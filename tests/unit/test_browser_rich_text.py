"""Individually approved rich-text input uses the existing browser action boundary."""

from __future__ import annotations

import pytest

from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserFieldKind,
    BrowserProviderError,
    BrowserSnapshot,
)
from agent_core.domain.browser_act_views import describe_browser_action
from tests.unit.test_browser_playwright import GRANT_NOW, _ref, lesson_constraint, lesson_pages

COMPOSER = """<!doctype html><title>Compose</title>
<style>[role="textbox"] {{ min-height: 30px; }}</style>
<form action="/publish" method="post">
{editor}
<button type="submit">Post</button>
</form>
<script>
window.inputs = [];
document.addEventListener('input', () => window.inputs.push(
    document.querySelector('[contenteditable]').innerText));
</script>"""


@pytest.mark.parametrize("headed", [False, True])
async def test_cancelled_navigation_keeps_the_unsaved_composer(headed: bool) -> None:
    page = (
        COMPOSER.format(
            editor='<div contenteditable="true" role="textbox" aria-label="Post text"></div>'
        )
        + """<script>
    window.addEventListener('beforeunload', event => {
        event.preventDefault(); event.returnValue = '';
    });
    </script>"""
    )
    async with lesson_pages({"/compose/post": page}, headed=headed) as (runtime, visit, left):
        before = await visit("/compose/post")
        after = await runtime.act(
            BrowserAction(
                kind=BrowserActionKind.TYPE,
                expected_revision=before.revision,
                ref=_ref(before, "Post text"),
                value="An unsaved draft",
            )
        )
        with pytest.raises(BrowserProviderError) as cancelled:
            await visit("/intent/post")
        assert cancelled.value.reason_code == "tool.browser.navigation_cancelled"
        assert not cancelled.value.retryable
        current = await runtime.observe()
        assert current.url == after.url
        assert "An unsaved draft" in current.text
        assert any(element.name == "Post" for element in current.elements)
        assert left == []


@pytest.mark.parametrize(
    "editor",
    [
        '<div contenteditable="true" role="textbox" aria-label="Post text">Old draft</div>',
        '<div contenteditable="plaintext-only" role="textbox" aria-label="Post text"></div>',
        '<div contenteditable="true"><div role="textbox" aria-label="Post text">'
        "Old draft</div></div>",
    ],
    ids=["rich-text", "plaintext-only", "inherited-editability"],
)
async def test_approved_rich_text_typing_updates_the_composer_without_submitting(
    editor: str,
) -> None:
    async with lesson_pages({"/compose/post": COMPOSER.format(editor=editor)}) as (
        runtime,
        visit,
        left,
    ):
        before = await visit("/compose/post")
        text = "A new draft with a second line.\nStill a draft."
        action = BrowserAction(
            kind=BrowserActionKind.TYPE,
            expected_revision=before.revision,
            ref=_ref(before, "Post text"),
            value=text,
        )
        view = describe_browser_action(
            action, BrowserSnapshot(observation=before, facts=runtime.facts(before.revision))
        )
        assert view.arguments["field"] == BrowserFieldKind.EDITABLE.value
        assert view.arguments["consequence"] == "publication"
        assert view.arguments["refused"] is False

        # The pipeline dispatches an individually approved action without a grant constraint.
        after = await runtime.act(action)

        assert await runtime._current_page().locator("[contenteditable]").inner_text() == text
        # Chromium emits separate input events for a line break and can split
        # inherited editable descendants. The editor must receive the complete text.
        assert await runtime._current_page().evaluate("window.inputs.at(-1)") == text
        assert after.revision != before.revision
        assert "A new draft" in after.text
        assert left == []
        with pytest.raises(BrowserProviderError) as stale:
            await runtime.act(action)
        assert stale.value.reason_code == "tool.browser.page_changed"


@pytest.mark.parametrize("grant_kind", ["task", "standing"])
async def test_rich_text_typing_remains_refused_under_grants(grant_kind: str) -> None:
    editor = '<div contenteditable="true" role="textbox" aria-label="Answer">Unchanged</div>'
    async with lesson_pages({"/lesson/1": COMPOSER.format(editor=editor)}) as (
        runtime,
        visit,
        left,
    ):
        before = await visit("/lesson/1")
        constraint = lesson_constraint()
        if grant_kind == "standing":
            constraint = lesson_constraint(
                grant_kind="standing",
                path_prefix=None,
                consequence_ceiling="routine",
                max_text_characters=None,
            )
        with pytest.raises(BrowserProviderError) as refused:
            await runtime.act(
                BrowserAction(
                    kind=BrowserActionKind.TYPE,
                    expected_revision=before.revision,
                    ref=_ref(before, "Answer"),
                    value="Changed",
                ),
                constraint=constraint,
                now=GRANT_NOW,
            )
        assert refused.value.reason_code == "tool.browser.action_not_allowed"
        assert await runtime._current_page().locator('[role="textbox"]').inner_text() == "Unchanged"
        assert await runtime._current_page().evaluate("window.inputs") == []
        assert left == []


@pytest.mark.parametrize(
    "attributes",
    [
        'contenteditable="false"',
        'contenteditable="true" autocomplete="current-password"',
        'contenteditable="true" autocomplete="new-password"',
        'contenteditable="true" autocomplete="one-time-code"',
    ],
)
async def test_approval_does_not_allow_noneditable_or_credential_controls(attributes: str) -> None:
    editor = f'<div {attributes} role="textbox" aria-label="Answer">Unchanged</div>'
    async with lesson_pages({"/lesson/1": COMPOSER.format(editor=editor)}) as (
        runtime,
        visit,
        left,
    ):
        before = await visit("/lesson/1")
        with pytest.raises(BrowserProviderError) as refused:
            await runtime.act(
                BrowserAction(
                    kind=BrowserActionKind.TYPE,
                    expected_revision=before.revision,
                    ref=_ref(before, "Answer"),
                    value="Changed",
                )
            )
        assert refused.value.reason_code == "tool.browser.action_not_allowed"
        assert await runtime._current_page().locator('[role="textbox"]').inner_text() == "Unchanged"
        assert await runtime._current_page().evaluate("window.inputs") == []
        assert left == []
