"""The Cockpit tools (docs/plan/cockpit.md, the tools).

Five deferred tools over the board and the publication path: read, check,
widget.write, configure and feed.publish. Writes wait briefly for the outbox
and report the revision's state, replay by their idempotency key, and are
offered only in the owner's interactive chat; feed data is labelled untrusted.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_core.adapters.cockpit.runtime import Draft202012SchemaCheck, FileCockpitRuntime
from agent_core.adapters.live_events import InMemoryLiveEventBroadcaster
from agent_core.application.cockpit_feeds import CockpitFeeds
from agent_core.application.cockpit_views import CockpitBoard
from agent_core.cockpit.catalogue import load_kit_catalogue
from agent_core.domain.cockpit import CockpitEventKind
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, SideEffectClass, TrustLevel
from agent_core.domain.tool_output import content_bytes
from agent_core.domain.tools import ToolExecutionContext, ToolFailureKind, ToolResult, ToolSpec
from agent_core.tools.cockpit import (
    COCKPIT_TOOL_NAMES,
    REASON_CODES,
    WIDGET_WRITE_TOOL,
    CockpitToolset,
    cockpit_tools,
    offers_widget_writes,
)
from tests.contract import support
from tests.contract.support import tool_context
from tests.gates.cockpit_publication_support import PRINCIPAL, Stack, package, publication_stack

OWNER = PRINCIPAL.model_copy(update={"scopes": {"cockpit.read", "cockpit.write"}})
CATALOGUE = {"kit_api": 1, "rules": ["Import only from react."], "primitives": [{"name": "Tile"}]}
CHAT = UUID(int=401)
SCHEDULED = UUID(int=402)


def _specs() -> dict[str, ToolSpec]:
    toolset = CockpitToolset(
        uow_factory=None,  # type: ignore[arg-type]
        clock=None,  # type: ignore[arg-type]
        board=None,  # type: ignore[arg-type]
        publication=None,  # type: ignore[arg-type]
        feeds=None,  # type: ignore[arg-type]
        catalogue=CATALOGUE,
        commit_wait_seconds=30,
    )
    return {tool.spec.name: tool.spec for tool in cockpit_tools(toolset)}


@dataclass
class Harness:
    stack: Stack
    tools: dict[str, Any]
    calls: int = 0

    def context(self, **changes: Any) -> ToolExecutionContext:
        # Each call is its own invocation unless a test replays one by its key.
        self.calls += 1
        changes.setdefault("idempotency_key", f"call-{self.calls}")
        changes.setdefault("session_id", CHAT)
        return replace(tool_context(), principal=OWNER, tenant_id=OWNER.tenant_id, **changes)

    async def call(self, name: str, arguments: dict[str, Any], **changes: Any) -> ToolResult:
        result: ToolResult = await self.tools[name].execute(arguments, self.context(**changes))
        return result


def reference_package(name: str) -> dict[str, str]:
    root = Path(__file__).resolve().parents[2] / "cockpit/examples/widgets" / name
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@asynccontextmanager
async def harness(
    tmp_path: Path, *, commit_wait_seconds: int = 0, catalogue: dict[str, Any] = CATALOGUE
) -> AsyncIterator[Harness]:
    async with publication_stack(principal=OWNER) as stack:
        async with stack.uow_factory() as uow:
            for session_id, metadata in ((CHAT, {}), (SCHEDULED, {"schedule_id": "s"})):
                await uow.sessions.create(
                    support.session().model_copy(
                        update={
                            "id": session_id,
                            "principal_id": OWNER.principal_id,
                            "metadata": metadata,
                        }
                    )
                )
        board = CockpitBoard(
            uow_factory=stack.uow_factory,
            clock=stack.clock,
            live_events=InMemoryLiveEventBroadcaster(),
            publication=stack.publication,
            runtime=FileCockpitRuntime(tmp_path / "runtime"),
            settings_schema=Draft202012SchemaCheck(),
            stream_max_seconds=600,
        )
        feeds = CockpitFeeds(
            uow_factory=stack.uow_factory,
            clock=stack.clock,
            schema_check=Draft202012SchemaCheck(),
            history_max=288,
        )
        toolset = CockpitToolset(
            uow_factory=stack.uow_factory,
            clock=stack.clock,
            board=board,
            publication=stack.publication,
            feeds=feeds,
            catalogue=catalogue,
            commit_wait_seconds=commit_wait_seconds,
        )
        yield Harness(stack, {tool.spec.name: tool for tool in cockpit_tools(toolset)})


def test_the_five_tools_are_classified_as_the_design_says() -> None:
    specs = _specs()
    assert tuple(sorted(specs)) == tuple(sorted(COCKPIT_TOOL_NAMES))
    read, check, write = specs["cockpit.read"], specs["cockpit.check"], specs[WIDGET_WRITE_TOOL]
    assert (read.side_effect, read.idempotency) == (
        SideEffectClass.NONE,
        IdempotencyClass.READ_ONLY,
    )
    assert read.required_scopes == {"cockpit.read"}
    assert read.output_trust is TrustLevel.INTERNAL_TOOL
    # The builder's sandbox is the tool's own, as it is widget.write's: the check
    # runs no command the model chose, and its verdict is the one widget.write
    # returns, so it keeps the internal label a write after it does not escalate on.
    assert (check.side_effect, check.target_kind) == (SideEffectClass.NONE, "in_process")
    assert check.output_trust is TrustLevel.INTERNAL_TOOL
    assert write.side_effect is SideEffectClass.PUBLICATION
    assert write.idempotency is IdempotencyClass.CONDITIONALLY_IDEMPOTENT
    for name in ("cockpit.configure", "cockpit.feed.publish"):
        assert specs[name].side_effect is SideEffectClass.NONE, name
    for name in (
        "cockpit.check",
        "cockpit.widget.write",
        "cockpit.configure",
        "cockpit.feed.publish",
    ):
        assert specs[name].required_scopes == {"cockpit.write"}, name


async def test_a_write_reports_its_commit_once_the_outbox_lands_it(tmp_path: Path) -> None:
    async with harness(tmp_path, commit_wait_seconds=30) as h:

        async def maintenance() -> None:
            for _ in range(200):
                if await h.stack.outbox.run_once():
                    return
                await asyncio.sleep(0)

        landing = asyncio.create_task(maintenance())
        result = await h.call(
            "cockpit.widget.write", {"operation": "create", "slug": "weather", "files": package()}
        )
        await landing
        assert result.ok, result.failure
        assert result.structured is not None
        assert result.structured["state"] == "live"
        assert result.structured["commit_sha"] == await h.stack.repository.head()


async def test_a_write_the_outbox_has_not_reached_reports_pending_commit(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        created = await h.call(
            "cockpit.widget.write", {"operation": "create", "slug": "weather", "files": package()}
        )
        assert created.ok and created.structured is not None
        assert created.structured["state"] == "pending_commit"
        revision = created.structured["revision_id"]
        await h.stack.outbox.run_once()
        updated = await h.call(
            "cockpit.widget.write",
            {
                "operation": "update",
                "slug": "weather",
                "files": package("v2"),
                "expected_revision": revision,
            },
        )
        assert updated.ok and updated.structured is not None
        stale = await h.call(
            "cockpit.widget.write",
            {
                "operation": "update",
                "slug": "weather",
                "files": package("v3"),
                "expected_revision": revision,
            },
        )
        assert not stale.ok and stale.failure is not None
        assert stale.failure.reason_code == "cockpit.revision_changed"


async def test_a_rejected_package_returns_its_diagnostics(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        result = await h.call(
            "cockpit.widget.write",
            {
                "operation": "create",
                "slug": "weather",
                "files": {**package(), "widget.tsx": "REJECT"},
            },
        )
        assert result.ok and result.structured is not None
        assert result.structured["state"] == "rejected"
        assert [item["rule"] for item in result.structured["diagnostics"]] == ["lint.banned"]


async def test_widget_writes_are_interactive_only(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        create = {"operation": "create", "slug": "weather", "files": package()}
        # A direct call by name is refused even where the session filter withholds the tool.
        for changes in (
            {"run_kind": "delegated"},
            {"run_kind": "skill_review"},
            {"session_id": SCHEDULED},
            {"session_id": UUID(int=403)},
        ):
            result = await h.call("cockpit.widget.write", create, **changes)
            assert not result.ok and result.failure is not None, changes
            assert result.failure.kind is ToolFailureKind.PERMISSION
            assert result.failure.reason_code == "cockpit.write_interactive_only"
        async with h.stack.uow_factory() as uow:
            assert await uow.cockpit_widgets.list_widgets(OWNER, include_deleted=True) == []


async def test_delete_and_restore_go_through_the_outbox(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        await h.call(
            "cockpit.widget.write", {"operation": "create", "slug": "weather", "files": package()}
        )
        await h.stack.outbox.run_once()
        deleted = await h.call("cockpit.widget.write", {"operation": "delete", "slug": "weather"})
        assert deleted.ok and deleted.structured is not None
        assert deleted.structured["state"] == "queued"
        await h.stack.outbox.run_once()
        restored = await h.call("cockpit.widget.write", {"operation": "restore", "slug": "weather"})
        assert restored.ok and restored.structured is not None
        assert restored.structured["state"] == "pending_commit"


async def test_check_builds_without_committing(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        head = await h.stack.repository.head()
        valid = await h.call(
            "cockpit.check", {"operation": "validate", "slug": "weather", "files": package()}
        )
        assert valid.ok and valid.structured is not None
        assert valid.structured["ok"] is True
        rejected = await h.call(
            "cockpit.check",
            {
                "operation": "validate",
                "slug": "weather",
                "files": {**package(), "widget.tsx": "REJECT"},
            },
        )
        assert rejected.ok and rejected.structured is not None
        assert rejected.structured["ok"] is False
        preview = await h.call(
            "cockpit.check", {"operation": "preview", "slug": "weather", "files": package()}
        )
        assert not preview.ok and preview.failure is not None
        assert preview.failure.reason_code == "cockpit.preview_unavailable"
        assert await h.stack.repository.head() == head
        async with h.stack.uow_factory() as uow:
            assert await uow.cockpit_widgets.list_widgets(OWNER, include_deleted=True) == []
        assert h.stack.builder.screens[0].names[:1] == ("Avery Quinn",)


async def test_read_lists_and_reads_widgets_layouts_feeds_and_the_kit(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        created = await h.call(
            "cockpit.widget.write", {"operation": "create", "slug": "weather", "files": package()}
        )
        assert created.structured is not None
        await h.stack.outbox.run_once()
        listed = await h.call("cockpit.read", {"operation": "list_widgets"})
        assert listed.structured is not None
        assert [item["slug"] for item in listed.structured["widgets"]] == ["weather"]
        read = await h.call("cockpit.read", {"operation": "read_widget", "slug": "weather"})
        assert read.structured is not None
        assert read.structured["files"] == {
            name: len(text.encode()) for name, text in package().items()
        }
        assert read.structured["head_revision_id"] == created.structured["revision_id"]
        assert read.output_trust is None  # the spec's internal label, not the feeds'
        source = await h.call(
            "cockpit.read", {"operation": "read_file", "slug": "weather", "file": "widget.tsx"}
        )
        assert source.structured is not None
        assert source.structured["content"] == package()["widget.tsx"]
        assert (source.structured["start_line"], source.structured["end_line"]) == (1, 1)
        assert (await h.call("cockpit.read", {"operation": "get_layouts"})).structured == {
            "layouts": {}
        }
        missing = await h.call("cockpit.read", {"operation": "read_widget", "slug": "markets"})
        assert not missing.ok and missing.failure is not None
        assert missing.failure.kind is ToolFailureKind.NOT_FOUND


async def test_the_kit_is_an_index_with_details_by_name(tmp_path: Path) -> None:
    async with harness(tmp_path, catalogue=load_kit_catalogue()) as h:
        index = await h.call("cockpit.read", {"operation": "describe_kit"})
        assert index.structured is not None
        names = [item["name"] for item in index.structured["primitives"]]
        assert {"Tile", "Metric", "Sparkline"} <= set(names)
        assert index.structured["rules"] and "props" not in index.structured["primitives"][0]
        detail = await h.call(
            "cockpit.read", {"operation": "describe_kit", "primitives": ["Tile", "Metric"]}
        )
        assert detail.structured is not None
        tile, metric = detail.structured["primitives"]
        assert (tile["name"], metric["name"]) == ("Tile", "Metric")
        assert tile["props"] and tile["example"]
        helpers = await h.call("cockpit.read", {"operation": "describe_kit", "section": "helpers"})
        assert helpers.structured is not None and helpers.structured["helpers"]
        unknown = await h.call(
            "cockpit.read", {"operation": "describe_kit", "primitives": ["Marquee"]}
        )
        assert unknown.failure is not None
        assert unknown.failure.reason_code == "cockpit.kit_unknown"


async def test_every_read_of_a_reference_widget_fits_the_inline_budget(tmp_path: Path) -> None:
    """Tool answers reach the model whole only up to 4 KiB (tools/limits.yaml)."""

    files = reference_package("flight-tracker")
    slug = json.loads(files["widget.json"])["slug"]
    long_source = "\n".join(f"// line {n:04d} {'x' * 60}" for n in range(1, 200)) + "\n"
    async with harness(tmp_path, catalogue=load_kit_catalogue()) as h:
        await h.call("cockpit.widget.write", {"operation": "create", "slug": slug, "files": files})
        await h.stack.outbox.run_once()
        big = {**package(), "widget.json": package()["widget.json"], "widget.tsx": long_source}
        await h.call(
            "cockpit.widget.write", {"operation": "create", "slug": "weather", "files": big}
        )
        reads: list[dict[str, Any]] = [
            {"operation": "describe_kit"},
            {"operation": "describe_kit", "section": "helpers"},
            {"operation": "describe_kit", "section": "types"},
            {"operation": "list_widgets"},
            {"operation": "read_widget", "slug": slug},
            {"operation": "read_file", "slug": slug, "file": "widget.tsx"},
            {"operation": "read_file", "slug": "weather", "file": "widget.tsx"},
        ]
        names = [item["name"] for item in load_kit_catalogue()["primitives"]]
        reads += [
            {"operation": "describe_kit", "primitives": names[index : index + 4]}
            for index in range(0, len(names), 4)
        ]
        for arguments in reads:
            result = await h.call("cockpit.read", arguments)
            assert result.ok, (arguments, result.failure)
            assert len(content_bytes(result.content)) <= 4096, arguments
        pages: list[str] = []
        start = 1
        while True:
            page = await h.call(
                "cockpit.read",
                {
                    "operation": "read_file",
                    "slug": "weather",
                    "file": "widget.tsx",
                    "start_line": start,
                },
            )
            assert page.structured is not None
            pages.append(page.structured["content"])
            if page.structured["end_line"] >= page.structured["total_lines"]:
                break
            start = page.structured["end_line"] + 1
        assert "".join(pages) == long_source


async def test_a_line_longer_than_a_page_is_paged_by_byte_offset(tmp_path: Path) -> None:
    """A long line (minified data, a long expression) is read whole, a page at a time."""

    long_line = "const data = [" + "é→x," * 1_500 + "];\n"
    source = "// head\n" + long_line + "// tail\n"
    assert len(long_line.encode("utf-8")) > 3 * 3_000
    async with harness(tmp_path) as h:
        files = {**package(), "widget.tsx": source}
        await h.call(
            "cockpit.widget.write", {"operation": "create", "slug": "weather", "files": files}
        )
        pages: list[str] = []
        position: dict[str, int] = {"start_line": 1}
        while True:
            page = await h.call(
                "cockpit.read",
                {"operation": "read_file", "slug": "weather", "file": "widget.tsx", **position},
            )
            assert page.ok and page.structured is not None, page.failure
            assert len(content_bytes(page.content)) <= 4096
            assert len(page.structured["content"].encode("utf-8")) <= 3_000
            pages.append(page.structured["content"])
            following = page.structured["next"]
            if following is None:
                break
            if "start_byte" in following:
                sentence = page.content[0]
                assert isinstance(sentence, TextPart)
                assert f"start_byte {following['start_byte']}" in sentence.text
            position = following
        assert "".join(pages) == source
        assert len(pages) >= 4
        inside_a_character = await h.call(
            "cockpit.read",
            {
                "operation": "read_file",
                "slug": "weather",
                "file": "widget.tsx",
                "start_line": 2,
                "start_byte": len("const data = [é".encode()) - 1,
            },
        )
        assert inside_a_character.failure is not None
        assert inside_a_character.failure.reason_code == "cockpit.invalid_arguments"


async def test_feed_publish_registers_checks_and_labels_the_feed_untrusted(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        schema = {
            "type": "object",
            "properties": {"temperature": {"type": "number"}},
            "required": ["temperature"],
        }
        first = await h.call(
            "cockpit.feed.publish",
            {
                "feed": "weather",
                "schema": schema,
                "expected_interval_seconds": 900,
                "data": {"temperature": 64},
            },
        )
        assert first.ok, first.failure
        assert first.structured is not None
        assert first.structured["feed"] == "agent.weather"
        for bad in (
            {"feed": "weather", "expected_interval_seconds": 900, "data": {"temperature": "hot"}},
            {"feed": "markets", "expected_interval_seconds": 900, "data": {}},
            {
                "feed": "weather",
                "expected_interval_seconds": 900,
                "data": {},
                "schema": {"type": 7},
            },
        ):
            refused = await h.call("cockpit.feed.publish", bad)
            assert not refused.ok and refused.failure is not None, bad
            assert refused.failure.kind is ToolFailureKind.INVALID_ARGUMENTS
        read = await h.call("cockpit.read", {"operation": "read_feed", "feed": "agent.weather"})
        assert read.structured is not None
        assert read.structured["current"]["data"] == {"temperature": 64}
        assert read.output_trust is TrustLevel.EXTERNAL_UNTRUSTED
        async with h.stack.uow_factory() as uow:
            events = await uow.cockpit_events.after(OWNER, sequence=0, limit=10)
        assert [(e.kind, e.subject) for e in events] == [
            (CockpitEventKind.FEED_UPDATED, "agent.weather")
        ]


async def test_configure_sets_private_settings_and_a_layout(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        files = {
            **package(),
            "widget.json": json.dumps(
                {
                    "schema": 1,
                    "slug": "weather",
                    "sizes": ["md"],
                    "settings": {
                        "type": "object",
                        "properties": {"place": {"type": "string"}},
                        "additionalProperties": False,
                    },
                }
            ),
        }
        await h.call(
            "cockpit.widget.write", {"operation": "create", "slug": "weather", "files": files}
        )
        await h.stack.outbox.run_once()
        settings = await h.call(
            "cockpit.configure",
            {"operation": "settings", "slug": "weather", "values": {"place": "Harbor"}},
        )
        assert settings.ok, settings.failure
        layout = await h.call(
            "cockpit.configure",
            {
                "operation": "layout",
                "device_class": "wide",
                "placements": [{"slug": "weather", "size": "md"}],
                "expected_revision": None,
            },
        )
        assert layout.ok and layout.structured is not None
        assert layout.structured["revision"] == 1
        bad = await h.call(
            "cockpit.configure",
            {"operation": "settings", "slug": "weather", "values": {"other": "x"}},
        )
        assert not bad.ok and bad.failure is not None
        assert bad.failure.kind is ToolFailureKind.INVALID_ARGUMENTS


async def test_the_write_approval_shows_the_change_as_a_diff(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        created = await h.call(
            "cockpit.widget.write", {"operation": "create", "slug": "weather", "files": package()}
        )
        assert created.structured is not None
        await h.stack.outbox.run_once()
        presentation = await h.tools["cockpit.widget.write"].approval_view_in_session(
            {
                "operation": "update",
                "slug": "weather",
                "files": package("v2"),
                "expected_revision": created.structured["revision_id"],
            },
            run=None,
            principal=OWNER,
            turn=None,
            not_covered=None,
        )
        assert presentation.summary == "Update the Cockpit widget weather on the public branch"
        assert presentation.arguments["file_changes"] == ["widget.tsx"]
        diff = "\n".join(presentation.arguments["diff"])
        assert "-export default function Weather() { return null; } // v1" in diff
        assert "+export default function Weather() { return null; } // v2" in diff


def test_the_stored_history_bound_is_the_manifest_schema_ceiling() -> None:
    from agent_core.domain.cockpit import FEED_HISTORY_MAX

    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "cockpit/schema/widget.schema.json").read_text()
    )
    feeds = schema["properties"]["feeds"]["items"]["properties"]
    assert feeds["history"]["maximum"] == FEED_HISTORY_MAX


async def test_a_replayed_write_returns_the_first_outcome(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        arguments = {"operation": "create", "slug": "weather", "files": package()}
        first = await h.call("cockpit.widget.write", arguments, idempotency_key="replayed-create")
        again = await h.call("cockpit.widget.write", arguments, idempotency_key="replayed-create")
        assert first.ok and again.ok and first.structured and again.structured
        assert again.structured["revision_id"] == first.structured["revision_id"]
        async with h.stack.uow_factory() as uow:
            assert len(await uow.cockpit_widgets.list_revisions(OWNER, "weather", limit=10)) == 1
        await h.stack.outbox.run_once()
        deleted = await h.call(
            "cockpit.widget.write", {"operation": "delete", "slug": "weather"}, idempotency_key="d"
        )
        replayed = await h.call(
            "cockpit.widget.write", {"operation": "delete", "slug": "weather"}, idempotency_key="d"
        )
        assert deleted.ok and replayed.ok, replayed.failure
        assert await h.stack.outbox.run_once() == 1
        assert await h.stack.outbox.run_once() == 0


@pytest.mark.parametrize(
    "metadata",
    [
        {"schedule_id": "00000000-0000-0000-0000-000000000001"},
        {"run_kind": "email_task"},
        {"email_operational": True},
        {"purpose": "memory_formation"},
        {"device_triage": {"device_id": "00000000-0000-0000-0000-000000000002", "channel": "sms"}},
        # A marker key excludes the session by its presence, whatever its value.
        {"device_triage": False},
        {"purpose": ""},
    ],
)
def test_widget_writes_are_withheld_outside_interactive_chat(metadata: dict[str, Any]) -> None:
    assert offers_widget_writes(support.session().metadata)
    assert not offers_widget_writes(metadata)


def test_widget_writes_follow_the_composed_session_filter() -> None:
    from agent_core.bootstrap import _session_tool_filter

    specs = list(_specs().values())
    session_filter = _session_tool_filter(None)
    assert session_filter is not None
    chat = support.session()
    scheduled = chat.model_copy(update={"metadata": {"schedule_id": "s"}})
    assert {spec.name for spec in session_filter(chat, specs)} == set(COCKPIT_TOOL_NAMES)
    assert {spec.name for spec in session_filter(scheduled, specs)} == (
        set(COCKPIT_TOOL_NAMES) - {WIDGET_WRITE_TOOL}
    )


def test_every_refusal_reads_in_platform_words() -> None:
    from agent_core.tools.messages import TOOL_MESSAGES

    assert all(code in TOOL_MESSAGES for code in REASON_CODES)
    assert all("{" not in TOOL_MESSAGES[code] for code in REASON_CODES)
