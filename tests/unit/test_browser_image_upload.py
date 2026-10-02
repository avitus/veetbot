"""Browser image upload is an approved transfer, not a filesystem capability."""

from __future__ import annotations

from typing import Any

import pytest

from agent_core.adapters.browser.playwright import PythonPlaywrightRuntime
from agent_core.domain.browser import BrowserAction, BrowserActionKind, BrowserProviderError
from agent_core.domain.browser_upload import BrowserImageFile
from agent_core.domain.media import MediaImage
from tests.unit.test_browser_playwright import _ref, lesson_pages


def test_the_runtime_has_a_distinct_image_upload_operation() -> None:
    assert callable(getattr(PythonPlaywrightRuntime, "upload", None))


@pytest.mark.parametrize("chooser", [False, True])
async def test_upload_selects_image_bytes_without_publishing(chooser: bool) -> None:
    data = b"\x89PNG\r\n\x1a\nsynthetic-image"
    image = BrowserImageFile(
        "00000000-0000-0000-0000-000000000001.png", MediaImage("image/png", data)
    )
    opener = (
        '<button type="button" onclick="document.getElementById(\'file\').click()">'
        "Add image</button>"
    )
    page_html = (
        '<!doctype html><title>Compose</title><form action="/publish" method="post">'
        f'<input id="file" type="file" aria-label="Image" {"hidden" if chooser else ""}>'
        f"{opener if chooser else ''}<button>Post</button></form>"
        '<script>window.changes = 0;document.getElementById("file").addEventListener('
        '"change", () => window.changes++);</script>'
    )
    async with lesson_pages({"/compose/post": page_html}) as (runtime, visit, left):
        before = await visit("/compose/post")
        action = BrowserAction(
            kind=BrowserActionKind.CLICK,
            expected_revision=before.revision,
            ref=_ref(before, "Add image" if chooser else "Image"),
        )
        uploader: Any = runtime
        after = await uploader.upload(action, image)
        selected = await runtime._current_page().evaluate(
            "async () => { const f = document.getElementById('file').files[0];"
            "return { name: f.name, type: f.type, "
            "bytes: Array.from(new Uint8Array(await f.arrayBuffer())),"
            "changes: window.changes }; }"
        )
        assert selected == {
            "name": image.filename,
            "type": "image/png",
            "bytes": list(data),
            "changes": 1,
        }
        assert after.revision != before.revision
        assert left == []


@pytest.mark.parametrize("case", ["stale", "unknown", "outside", "nonclick", "disabled"])
async def test_upload_refuses_before_selecting_a_file(case: str) -> None:
    image = BrowserImageFile.for_artifact(
        "00000000-0000-0000-0000-000000000001", MediaImage("image/png", b"\x89PNG\r\n\x1a\nimage")
    )
    html = '<!doctype html><input type="file" aria-label="Image">'
    async with lesson_pages({"/compose/post": html}) as (runtime, visit, left):
        page = await visit("/compose/post")
        if case == "outside":
            runtime._allowed_origins = ("https://elsewhere.test",)
        if case == "disabled":
            await runtime._current_page().locator("input").evaluate("node => node.disabled = true")
        action = BrowserAction(
            kind=BrowserActionKind.CHECK if case == "nonclick" else BrowserActionKind.CLICK,
            expected_revision="stale" if case == "stale" else page.revision,
            ref="unknown" if case == "unknown" else _ref(page, "Image"),
        )
        with pytest.raises(BrowserProviderError) as refused:
            await runtime.upload(action, image)
        assert (
            refused.value.reason_code
            == {
                "stale": "tool.browser.page_changed",
                "unknown": "tool.browser.element_not_found",
                "outside": "tool.browser.action_not_allowed",
                "nonclick": "tool.browser.action_not_allowed",
                "disabled": "tool.browser.element_not_found",
            }[case]
        )
        assert (
            await runtime._current_page().locator("input").evaluate("node => node.files.length")
            == 0
        )
        assert left == []


async def test_missing_chooser_after_a_click_is_uncertain(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_core.adapters.browser import playwright as adapter

    monkeypatch.setattr(adapter, "ACTION_TIMEOUT_MILLISECONDS", 200)
    image = BrowserImageFile.for_artifact(
        "00000000-0000-0000-0000-000000000001", MediaImage("image/png", b"\x89PNG\r\n\x1a\nimage")
    )
    html = '<!doctype html><button onclick="window.clicked = true">Add image</button>'
    async with lesson_pages({"/compose/post": html}) as (runtime, visit, left):
        page = await visit("/compose/post")
        action = BrowserAction(
            kind=BrowserActionKind.CLICK,
            expected_revision=page.revision,
            ref=_ref(page, "Add image"),
        )
        with pytest.raises(BrowserProviderError) as uncertain:
            await runtime.upload(action, image)
        assert uncertain.value.reason_code == "tool.browser.outcome_unknown"
        assert await runtime._current_page().evaluate("window.clicked") is True
        assert left == []


async def test_upload_never_releases_bytes_to_an_embedded_document() -> None:
    image = BrowserImageFile.for_artifact(
        "00000000-0000-0000-0000-000000000001", MediaImage("image/png", b"\x89PNG\r\n\x1a\nimage")
    )
    html = (
        '<!doctype html><iframe id="frame" srcdoc="&lt;input type=file&gt;"></iframe>'
        "<button onclick=\"document.getElementById('frame').contentDocument."
        "querySelector('input').click()\">Add image</button>"
    )
    async with lesson_pages({"/compose/post": html}) as (runtime, visit, left):
        page = await visit("/compose/post")
        action = BrowserAction(
            kind=BrowserActionKind.CLICK,
            expected_revision=page.revision,
            ref=_ref(page, "Add image"),
        )
        with pytest.raises(BrowserProviderError) as refused:
            await runtime.upload(action, image)
        assert refused.value.reason_code == "tool.browser.outcome_unknown"
        assert (
            await runtime._current_page()
            .frame_locator("iframe")
            .locator("input")
            .evaluate("node => node.files.length")
            == 0
        )
        assert left == []


def test_nginx_streams_only_the_bounded_image_upload_route() -> None:
    from pathlib import Path

    nginx = (Path(__file__).resolve().parents[2] / "nginx/veetbot.conf").read_text()
    browser = nginx.split("live/browser.veetbot.com/fullchain.pem;", 1)[1].split("\nserver {", 1)[0]
    upload = browser.split("location = /v1/browser-sessions:upload {", 1)[1].split("\n    }", 1)[0]
    assert "client_max_body_size 64k;" in browser
    for setting in (
        "client_max_body_size 7m;",
        "proxy_request_buffering off;",
        "proxy_buffering off;",
        "access_log off;",
        "proxy_pass http://127.0.0.1:8081;",
    ):
        assert setting in upload
