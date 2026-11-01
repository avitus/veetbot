"""The Cockpit tools: read, check and write widgets, configure, publish feeds (Milestone 35).

docs/plan/cockpit.md, the tools. The domain registers only under
`AGENT_COCKPIT_ENABLED`, and its five tools are deferred index entries. A
widget write goes through the whole publication path, waits a bounded time
for the outbox and reports where its revision got to. It is offered only in
the owner's interactive chat, refuses any other run, and a replay with the
same idempotency key returns the first outcome. Feed data is labelled
untrusted wherever it is read.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from agent_core.application.authorization import require_scope
from agent_core.application.cockpit import CockpitPublication
from agent_core.application.cockpit_diagnostics import diagnostics
from agent_core.application.cockpit_feeds import CockpitFeedRefusedError, CockpitFeeds
from agent_core.application.cockpit_views import CockpitBoard
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalPresentation
from agent_core.domain.cockpit import (
    FEED_ERROR_MAX_CHARS,
    WIDGET_FILE_MAX_BYTES,
    WIDGET_FILE_PATTERN,
    WIDGET_PACKAGE_MAX_FILES,
    WIDGET_SLUG_PATTERN,
    DeviceClass,
    OutboxOperation,
    RevisionState,
    WidgetRevision,
    WidgetState,
    feed_trust,
)
from agent_core.domain.cockpit_views import CockpitLayoutWrite
from agent_core.domain.errors import (
    CockpitValidationError,
    ConflictError,
    NotFoundError,
    RateLimitedError,
)
from agent_core.domain.messages import ImageReferencePart, TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.sessions import (
    SESSION_DEVICE_TRIAGE_METADATA_KEY,
    SESSION_EMAIL_OPERATIONAL_METADATA_KEY,
    SESSION_PURPOSE_METADATA_KEY,
    SESSION_RUN_KIND_METADATA_KEY,
    SESSION_SCHEDULE_ID_METADATA_KEY,
)
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolResult,
    ToolSpec,
)
from agent_core.ports.artifacts import ArtifactWriter
from agent_core.ports.cockpit import WidgetBuild, WidgetBuildFailedError
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory

COCKPIT_TOOL_NAMES = (
    "cockpit.read",
    "cockpit.check",
    "cockpit.widget.write",
    "cockpit.configure",
    "cockpit.feed.publish",
)
WIDGET_WRITE_TOOL = "cockpit.widget.write"
REASON_CODES = (
    "cockpit.not_found",
    "cockpit.kit_unknown",
    "cockpit.revision_changed",
    "cockpit.expected_revision_required",
    "cockpit.widget_deleted",
    "cockpit.widget_not_deleted",
    "cockpit.layout_changed",
    "cockpit.conflict",
    "cockpit.write_interactive_only",
    "cockpit.rate_limited",
    "cockpit.build_unavailable",
    "cockpit.preview_unavailable",
    "cockpit.settings_invalid",
    "cockpit.layout_invalid",
    "cockpit.feed_schema_required",
    "cockpit.feed_schema_invalid",
    "cockpit.feed_data_invalid",
    "cockpit.feed_observed_in_future",
    "cockpit.feed_invalid",
    "cockpit.invalid_arguments",
)
# A session carrying any of these is not the owner's interactive chat.
_NOT_CHAT = (
    SESSION_SCHEDULE_ID_METADATA_KEY,
    SESSION_RUN_KIND_METADATA_KEY,
    SESSION_EMAIL_OPERATIONAL_METADATA_KEY,
    SESSION_PURPOSE_METADATA_KEY,
    SESSION_DEVICE_TRIAGE_METADATA_KEY,
)
_INTERACTIVE = "interactive"
_COMMIT_POLL_SECONDS = 1.0
_WAITING = frozenset({RevisionState.PENDING_COMMIT, RevisionState.COMMITTED})
_CONFLICT_CODES = {
    "revision_changed": "cockpit.revision_changed",
    "widget_exists": "cockpit.revision_changed",
    "widget_deleted": "cockpit.widget_deleted",
    "widget_not_deleted": "cockpit.widget_not_deleted",
    "layout_changed": "cockpit.layout_changed",
}
_DIFF_MAX_LINES = 400
# Every tool answer reaches the model inline up to 4 KiB (tools/limits.yaml), so the
# kit, a widget's files and the diagnostics come in pieces that fit.
_KIT_DETAIL_MAX = 4
_KIT_SECTIONS = ("helpers", "types")
_FILE_PAGE_BYTES = 3_000
_RECENT_REVISIONS = 5
_FEED_HISTORY_READ = 10
_DIAGNOSTICS_SHOWN = 10
_DIAGNOSTIC_MAX_CHARS = 300
_DIFF_LINE_MAX_CHARS = 240
_FEED_NAME_PATTERN = r"^(agent\.)?[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*)*$"
_FEED_PREFIX = "agent."
_OUTPUT_SCHEMA: dict[str, Any] = {"type": "object"}
_SLUG = {"type": "string", "pattern": WIDGET_SLUG_PATTERN}
_FILES = {
    "type": "object",
    "minProperties": 1,
    "maxProperties": WIDGET_PACKAGE_MAX_FILES,
    "propertyNames": {"pattern": WIDGET_FILE_PATTERN},
    "additionalProperties": {"type": "string", "maxLength": WIDGET_FILE_MAX_BYTES},
}
_PUBLIC = (
    " Widget code is generic and public: personal values belong in private settings "
    "(cockpit.configure) and feeds (cockpit.feed.publish), never in files or fixtures."
)


def offers_widget_writes(metadata: Mapping[str, object]) -> bool:
    """Whether a session is the owner's interactive chat, the only place widgets are written."""

    return not any(key in metadata for key in _NOT_CHAT)


@dataclass(frozen=True, slots=True)
class CockpitToolset:
    """What the five tools share: the board, the publication path and the feeds."""

    uow_factory: UnitOfWorkFactory
    clock: Clock
    board: CockpitBoard
    publication: CockpitPublication
    feeds: CockpitFeeds
    catalogue: Mapping[str, Any]
    commit_wait_seconds: int


def _failure(
    kind: ToolFailureKind, code: str, detail: str, *, retryable: bool = False
) -> ToolResult:
    return ToolResult(
        ok=False,
        content=[],
        failure=ToolFailure(kind=kind, reason_code=code, detail=detail, retryable=retryable),
    )


def _invalid(detail: str, code: str = "cockpit.invalid_arguments") -> ToolResult:
    return _failure(ToolFailureKind.INVALID_ARGUMENTS, code, detail)


def _ok(sentence: str, body: dict[str, Any], *, trust: TrustLevel | None = None) -> ToolResult:
    rendered = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return ToolResult(
        ok=True,
        content=[TextPart(text=sentence), TextPart(text=rendered)],
        structured=body,
        output_trust=trust,
    )


def _stamp(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _diagnostics(revision_or_build: WidgetRevision | WidgetBuild) -> list[dict[str, Any]]:
    shown = []
    for item in revision_or_build.diagnostics[:_DIAGNOSTICS_SHOWN]:
        body = item.model_dump(mode="json", exclude_none=True)
        body["message"] = item.message[:_DIAGNOSTIC_MAX_CHARS]
        shown.append(body)
    return shown


def _manifest(files: Mapping[str, str]) -> dict[str, Any]:
    try:
        manifest = json.loads(files.get("widget.json", "{}"))
    except ValueError:
        return {}
    return manifest if isinstance(manifest, dict) else {}


def _spec(
    name: str,
    description: str,
    schema: dict[str, Any],
    *,
    side_effect: SideEffectClass,
    risk: RiskLevel,
    idempotency: IdempotencyClass,
    scope: str,
    timeout_seconds: int,
    maximum_output_bytes: int,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        version="1.0.0",
        description=description,
        input_schema={**schema, "additionalProperties": False},
        output_schema=_OUTPUT_SCHEMA,
        side_effect=side_effect,
        risk=risk,
        idempotency=idempotency,
        required_scopes={scope},
        timeout_seconds=timeout_seconds,
        maximum_output_bytes=maximum_output_bytes,
        allow_parallel=False,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )


class _CockpitTool:
    spec: ToolSpec

    def __init__(self, toolset: CockpitToolset) -> None:
        self._toolset = toolset

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        for scope in sorted(self.spec.required_scopes):
            require_scope(context.principal, scope)
        try:
            return await self._run(arguments, context)
        except NotFoundError as exc:
            return _failure(ToolFailureKind.NOT_FOUND, "cockpit.not_found", str(exc))
        except ConflictError as exc:
            code = _CONFLICT_CODES.get(exc.reason or "", "cockpit.conflict")
            return _invalid(str(exc), code)
        except RateLimitedError as exc:
            return _failure(ToolFailureKind.UPSTREAM_ERROR, "cockpit.rate_limited", str(exc))
        except WidgetBuildFailedError:
            return _failure(
                ToolFailureKind.UPSTREAM_ERROR,
                "cockpit.build_unavailable",
                "the builder reached no verdict; nothing was recorded",
                retryable=True,
            )
        except CockpitFeedRefusedError as exc:
            return _invalid(str(exc), f"cockpit.{exc.reason}")
        except ValueError as exc:
            return _invalid(str(exc))

    async def _run(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        raise NotImplementedError


class PreviousCockpitReadTool(_CockpitTool):
    spec = _spec(
        "cockpit.read",
        "Read the Cockpit. describe_kit lists the authoring rules and the kit's primitives; "
        "name up to four in `primitives` for their props and examples, or a `section` "
        "(helpers, types). list_widgets and read_widget give state, settings schema, file "
        "sizes, recent revisions and head_revision_id, which an update names as "
        "expected_revision; read_file pages one file of the head revision from start_line, "
        "and within a line longer than a page from start_byte, as each page's `next` says. "
        "list_feeds, read_feed (with up to ten earlier snapshots) and get_layouts read the "
        "rest. Read the kit before writing a widget.",
        {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "describe_kit",
                        "list_widgets",
                        "read_widget",
                        "read_file",
                        "list_feeds",
                        "read_feed",
                        "get_layouts",
                    ],
                },
                "primitives": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": _KIT_DETAIL_MAX,
                    "items": {"type": "string", "minLength": 1, "maxLength": 40},
                },
                "section": {"type": "string", "enum": list(_KIT_SECTIONS)},
                "slug": _SLUG,
                "file": {"type": "string", "pattern": WIDGET_FILE_PATTERN},
                "start_line": {"type": "integer", "minimum": 1},
                "start_byte": {"type": "integer", "minimum": 0},
                "feed": {"type": "string", "minLength": 1, "maxLength": 64},
                "history": {"type": "integer", "minimum": 0, "maximum": _FEED_HISTORY_READ},
            },
            "required": ["operation"],
        },
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        scope="cockpit.read",
        timeout_seconds=15,
        maximum_output_bytes=65_536,
    )

    async def _run(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        principal = context.principal
        operation = arguments["operation"]
        if operation == "describe_kit":
            return self._describe_kit(arguments)
        if operation == "list_widgets":
            return await self._list_widgets(principal)
        if operation in {"read_widget", "read_file"}:
            if "slug" not in arguments:
                return _invalid(f"{operation} names a slug")
            if operation == "read_widget":
                return await self._read_widget(principal, str(arguments["slug"]))
            if "file" not in arguments:
                return _invalid("read_file names a file")
            return await self._read_file(
                principal,
                str(arguments["slug"]),
                str(arguments["file"]),
                int(arguments.get("start_line", 1)),
                int(arguments.get("start_byte", 0)),
            )
        if operation == "list_feeds":
            feeds = await self._toolset.feeds.list_feeds(principal)
            body = {
                "feeds": [
                    {
                        "id": feed.id,
                        "trust": feed_trust(feed.id).value,
                        "schema_version": feed.schema_version,
                    }
                    for feed in feeds
                ]
            }
            return _ok(f"{len(feeds)} feeds; read_feed gives each one's schema.", body)
        if operation == "read_feed":
            if "feed" not in arguments:
                return _invalid("read_feed names a feed")
            return await self._read_feed(
                principal, str(arguments["feed"]), int(arguments.get("history", 0))
            )
        layouts: dict[str, Any] = {}
        async with self._toolset.uow_factory() as uow:
            for device_class in DeviceClass:
                layout = await uow.cockpit_widgets.get_layout(principal, device_class)
                if layout is not None:
                    layouts[device_class.value] = {
                        "revision": layout.revision,
                        "placements": [p.model_dump(mode="json") for p in layout.placements],
                    }
        return _ok(f"{len(layouts)} layouts.", {"layouts": layouts})

    def _describe_kit(self, arguments: dict[str, Any]) -> ToolResult:
        catalogue = self._toolset.catalogue
        primitives = {
            str(item.get("name")): item
            for item in catalogue.get("primitives", [])
            if isinstance(item, dict)
        }
        if "primitives" in arguments:
            names = [str(name) for name in arguments["primitives"]]
            unknown = [name for name in names if name not in primitives]
            if unknown:
                return _failure(
                    ToolFailureKind.NOT_FOUND, "cockpit.kit_unknown", f"no primitive {unknown[0]}"
                )
            details = [primitives[name] for name in names]
            return _ok(f"{len(details)} kit primitives.", {"primitives": details})
        if "section" in arguments:
            section = str(arguments["section"])
            return _ok(f"The kit's {section}.", {section: catalogue.get(section)})
        index = {
            "kit_api": catalogue.get("kit_api"),
            "rules": catalogue.get("rules", []),
            "primitives": [
                {"name": name, "description": item.get("description", "")}
                for name, item in primitives.items()
            ],
            "sections": list(_KIT_SECTIONS),
        }
        return _ok(
            f"The kit: {len(primitives)} primitives. Name up to {_KIT_DETAIL_MAX} in "
            "`primitives` for their props and examples.",
            index,
        )

    async def _list_widgets(self, principal: Principal) -> ToolResult:
        rows: list[dict[str, Any]] = []
        async with self._toolset.uow_factory() as uow:
            widgets = await uow.cockpit_widgets.list_widgets(principal, include_deleted=True)
            titles: dict[str, Any] = {}
            for widget in widgets:
                if widget.live_revision_id is not None:
                    live = await uow.cockpit_widgets.get_revision(
                        principal, widget.live_revision_id
                    )
                    titles[widget.slug] = _manifest(live.files).get("title")
        for widget in sorted(widgets, key=lambda item: item.slug):
            head = await self._toolset.publication.head_revision(principal, widget.slug)
            rows.append(
                {
                    "slug": widget.slug,
                    "state": widget.state.value,
                    "title": titles.get(widget.slug),
                    "head_revision_id": None if head is None else str(head.id),
                    "head_state": None if head is None else head.state.value,
                }
            )
        return _ok(f"{len(rows)} widgets.", {"widgets": rows})

    async def _shown(
        self, principal: Principal, slug: str, live_revision_id: UUID | None
    ) -> tuple[WidgetRevision | None, WidgetRevision | None]:
        """The head revision, and the revision whose files a reader sees: the head or the live."""

        head = await self._toolset.publication.head_revision(principal, slug)
        if head is not None or live_revision_id is None:
            return head, head
        async with self._toolset.uow_factory() as uow:
            return None, await uow.cockpit_widgets.get_revision(principal, live_revision_id)

    async def _read_widget(self, principal: Principal, slug: str) -> ToolResult:
        detail = await self._toolset.board.widget(principal, slug)
        head, shown = await self._shown(principal, slug, detail.live_revision_id)
        files = {} if shown is None else shown.files
        unseen = [failure for failure in detail.import_failures if not failure.seen]
        body = {
            "slug": detail.slug,
            "state": detail.state.value,
            "live_revision_id": None
            if detail.live_revision_id is None
            else str(detail.live_revision_id),
            "head_revision_id": None if head is None else str(head.id),
            "head_state": None if head is None else head.state.value,
            "settings_schema": detail.settings_schema,
            "settings": detail.settings,
            "files": {name: len(text.encode("utf-8")) for name, text in sorted(files.items())},
            "revisions": [
                {
                    "id": str(revision.id),
                    "state": revision.state.value,
                    "source": revision.source.value,
                    "commit_sha": revision.commit_sha,
                    "created_at": _stamp(revision.created_at),
                }
                for revision in detail.revisions[:_RECENT_REVISIONS]
            ],
            "unseen_import_failures": len(unseen),
        }
        return _ok(
            f"Widget {slug} is {detail.state.value}; read_file pages its files.",
            body,
        )

    async def _read_file(
        self, principal: Principal, slug: str, name: str, start_line: int, start_byte: int
    ) -> ToolResult:
        async with self._toolset.uow_factory() as uow:
            widget = await uow.cockpit_widgets.get_widget(principal, slug)
        _head, shown = await self._shown(principal, slug, widget.live_revision_id)
        if shown is None or name not in shown.files:
            raise NotFoundError(f"{slug} has no file {name}")
        lines = shown.files[name].splitlines(keepends=True)
        first = lines[start_line - 1].encode("utf-8") if start_line <= len(lines) else b""
        if start_byte and not (start_byte < len(first) and first[start_byte] & 0xC0 != 0x80):
            return _invalid(
                f"start_byte {start_byte} does not begin a character of line {start_line}"
            )
        page: list[str] = []
        used = 0
        following: dict[str, int] | None = None
        for number in range(start_line, len(lines) + 1):
            offset = start_byte if number == start_line else 0
            data = lines[number - 1].encode("utf-8")[offset:]
            if used + len(data) <= _FILE_PAGE_BYTES:
                page.append(data.decode("utf-8"))
                used += len(data)
                continue
            if page:
                following = {"start_line": number}
                break
            # A line longer than a whole page is cut at a character boundary and
            # continues from that byte, so every byte of the file can be read.
            cut = _FILE_PAGE_BYTES
            while data[cut] & 0xC0 == 0x80:
                cut -= 1
            page.append(data[:cut].decode("utf-8"))
            following = {"start_line": number, "start_byte": offset + cut}
            break
        end_line = start_line + len(page) - 1
        body = {
            "slug": slug,
            "file": name,
            "revision_id": str(shown.id),
            "start_line": start_line,
            "start_byte": start_byte,
            "end_line": end_line,
            "total_lines": len(lines),
            "content": "".join(page),
            "next": following,
        }
        span = f"{name} lines {start_line}-{end_line} of {len(lines)}"
        if start_byte:
            span += f" (line {start_line} from byte {start_byte})"
        if following is None:
            return _ok(f"{span}.", body)
        if "start_byte" in following:
            return _ok(
                f"{span}; line {end_line} continues past this page: continue at start_line "
                f"{end_line}, start_byte {following['start_byte']}.",
                body,
            )
        return _ok(f"{span}; continue at start_line {following['start_line']}.", body)

    async def _read_feed(self, principal: Principal, feed_id: str, history: int) -> ToolResult:
        reading = await self._toolset.feeds.read(principal, feed_id, history=history)

        def entry(snapshot: Any) -> dict[str, Any]:
            return {
                "observed_at": _stamp(snapshot.observed_at),
                "expected_interval_seconds": snapshot.expected_interval_seconds,
                "error": snapshot.error,
                "data": snapshot.data,
                "schema_version": snapshot.schema_version,
            }

        body = {
            "feed": reading.feed.id,
            "trust": feed_trust(reading.feed.id).value,
            "schema_version": reading.feed.schema_version,
            "schema": reading.feed.schema_document,
            "current": None if reading.current is None else entry(reading.current),
            "history": [entry(item) for item in reading.history],
        }
        # Every feed's content came from a source outside the conversation: an assistant
        # feed from what a run read, a platform feed from mail, calls and people.
        return _ok(f"Feed {feed_id}.", body, trust=TrustLevel.EXTERNAL_UNTRUSTED)


class CockpitReadTool(PreviousCockpitReadTool):
    """Add revision diagnostics while preserving reads pinned to 1.0.0."""

    spec = PreviousCockpitReadTool.spec.model_copy(
        deep=True,
        update={
            "version": "1.1.0",
            "description": PreviousCockpitReadTool.spec.description
            + " Use diagnostics with slug and optional revision_id for build, import, "
            "publication and device failures.",
        },
    )
    spec.input_schema["properties"]["operation"]["enum"].append("diagnostics")
    spec.input_schema["properties"].update({"revision_id": {"type": "string", "format": "uuid"}})

    async def _run(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        if arguments["operation"] == "diagnostics":
            if "slug" not in arguments:
                return _invalid("Diagnostics requires a widget slug.")
            revision = UUID(arguments["revision_id"]) if "revision_id" in arguments else None
            async with self._toolset.uow_factory() as uow:
                details = await diagnostics(uow, context.principal, arguments["slug"], revision)
            return _ok("Widget diagnostics.", details, trust=TrustLevel.EXTERNAL_UNTRUSTED)
        return await super()._run(arguments, context)


class PreviousCockpitCheckTool(_CockpitTool):
    spec = _spec(
        "cockpit.check",
        "Check a widget package without publishing it: validate runs the same build and "
        "privacy screen a write runs, in the builder's offline sandbox, and returns its "
        "diagnostics. preview is not available yet." + _PUBLIC,
        {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["validate", "preview"]},
                "slug": _SLUG,
                "files": _FILES,
            },
            "required": ["operation", "slug", "files"],
        },
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        scope="cockpit.write",
        timeout_seconds=180,
        maximum_output_bytes=65_536,
    )

    async def _run(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        if arguments["operation"] == "preview":
            return _failure(
                ToolFailureKind.UPSTREAM_ERROR,
                "cockpit.preview_unavailable",
                "the preview renderer is not deployed",
            )
        build = await self._toolset.publication.check(
            context.principal, str(arguments["slug"]), dict(arguments["files"])
        )
        body = {"ok": build.ok, "kit_api": build.kit_api, "diagnostics": _diagnostics(build)}
        sentence = (
            "The package builds and passes the screen."
            if build.ok
            else f"The package was rejected with {len(build.diagnostics)} diagnostics."
        )
        return _ok(sentence, body)


class CockpitCheckTool(PreviousCockpitCheckTool):
    """Render a checked fixture through WebKit and return its private artifact."""

    spec = PreviousCockpitCheckTool.spec.model_copy(
        deep=True,
        update={
            "version": "1.1.0",
            "description": "Validate a widget or preview its fixture in WebKit as text and a PNG."
            + _PUBLIC,
        },
    )
    spec.input_schema["properties"].update(
        {
            "fixture": {"type": "string", "pattern": r"^fixtures/[a-z0-9][a-z0-9-]{0,31}\.json$"},
            "size": {"type": "string", "enum": ["sm", "md", "tall", "lg", "wide"]},
        }
    )

    async def _run(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        if arguments["operation"] != "preview":
            return await super()._run(arguments, context)
        selection = {k: str(arguments[k]) for k in ("fixture", "size") if k in arguments}
        if "_live" in arguments:
            selection["live"] = str(arguments["_live"])
        build = await self._toolset.publication.check(
            context.principal,
            str(arguments["slug"]),
            dict(arguments["files"]),
            preview=selection,
        )
        if build.ok:
            rendered = build.preview
            if rendered is None or not callable(getattr(type(context.artifacts), "create", None)):
                return _failure(
                    ToolFailureKind.UPSTREAM_ERROR,
                    "cockpit.preview_unavailable",
                    "The preview renderer or artifact store is unavailable.",
                )

            async def stream() -> AsyncIterator[bytes]:
                yield rendered.png

            case = rendered.fixture.split("/")[-1].removesuffix(".json")
            name = f"{arguments['slug']}-{case}-{rendered.size}.png"
            trust = TrustLevel.EXTERNAL_UNTRUSTED
            ref = await cast(ArtifactWriter, context.artifacts).create(
                stream(), name, "image/png", trust
            )
            result = _ok(
                (
                    "Rendered the current live data in WebKit."
                    if rendered.fixture == "live"
                    else "Rendered the synthetic fixture in WebKit."
                ),
                {
                    "ok": True,
                    "fixture": rendered.fixture,
                    "size": rendered.size,
                    "text": rendered.text,
                    "artifact_id": str(ref.artifact_id),
                },
                trust=trust,
            )
            result.content.append(
                ImageReferencePart(
                    artifact_id=ref.artifact_id, media_type="image/png", filename=name
                )
            )
            result.artifacts.append(
                {
                    "artifact_id": str(ref.artifact_id),
                    "media_type": "image/png",
                    "sha256": ref.sha256,
                    "size_bytes": ref.size_bytes,
                }
            )
            return result
        return _ok(
            "The widget could not be previewed.",
            {
                "ok": False,
                "kit_api": build.kit_api,
                "diagnostics": _diagnostics(build),
            },
            trust=TrustLevel.EXTERNAL_UNTRUSTED,
        )


class CockpitLiveCheckTool(CockpitCheckTool):
    """Preview a widget with the owner's current feed values, in the offline builder (ADR-0281)."""

    spec = CockpitCheckTool.spec.model_copy(
        deep=True,
        update={
            "version": "1.2.0",
            "description": (
                "Validate a widget or preview it in WebKit as text and a PNG, with a fixture or "
                "with live: true for the current feeds and settings, which stay private." + _PUBLIC
            ),
        },
    )
    spec.input_schema["properties"].update({"live": {"type": "boolean"}})

    async def _run(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        if not (arguments["operation"] == "preview" and arguments.get("live")):
            return await super()._run(arguments, context)
        if "fixture" in arguments:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "cockpit.preview_selection",
                "Preview a fixture or live data, not both.",
            )
        return await super()._run(
            {
                **{key: value for key, value in arguments.items() if key != "live"},
                "_live": await self._live_values(arguments, context),
            },
            context,
        )

    async def _live_values(self, arguments: dict[str, Any], context: ToolExecutionContext) -> str:
        """The widget's declared feeds and settings, in the shape of a fixture."""
        principal = context.principal
        try:
            manifest = json.loads(dict(arguments["files"]).get("widget.json", "{}"))
        except ValueError:
            manifest = {}
        feeds: dict[str, Any] = {}
        for declared in manifest.get("feeds", []) if isinstance(manifest, dict) else []:
            try:
                reading = await self._toolset.feeds.read(
                    principal, declared["id"], history=int(declared.get("history", 0))
                )
            except (KeyError, TypeError, ValueError, NotFoundError):
                continue
            if reading.current is None:
                continue
            feeds[declared["id"]] = {
                "observedAt": reading.current.observed_at.isoformat(),
                "expectedIntervalSec": reading.current.expected_interval_seconds,
                "error": reading.current.error,
                "data": reading.current.data,
                "history": [
                    {"observedAt": item.observed_at.isoformat(), "data": item.data}
                    for item in reading.history
                ],
            }
        settings: dict[str, Any] = {}
        async with self._toolset.uow_factory() as uow:
            stored = await uow.cockpit_widgets.get_settings(principal, str(arguments["slug"]))
        if stored is not None:
            settings = dict(stored.values)
        return json.dumps(
            {
                "now": self._toolset.clock.now().isoformat(),
                "settings": settings,
                "feeds": feeds,
            }
        )


class CockpitWidgetWriteTool(_CockpitTool):
    spec = _spec(
        WIDGET_WRITE_TOOL,
        "Publish a Cockpit widget: create, update (naming the widget's head_revision_id "
        "from cockpit.read as expected_revision), delete or restore. Every change is built, "
        "privacy-screened and committed to the public widget branch before it goes live; "
        "the tool waits briefly and reports the revision's state: live, pending_commit "
        "while the commit is on its way, or rejected with diagnostics." + _PUBLIC,
        {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["create", "update", "delete", "restore"],
                },
                "slug": _SLUG,
                "files": _FILES,
                "expected_revision": {"type": "string", "format": "uuid"},
            },
            "required": ["operation", "slug"],
        },
        side_effect=SideEffectClass.PUBLICATION,
        risk=RiskLevel.MEDIUM,
        idempotency=IdempotencyClass.CONDITIONALLY_IDEMPOTENT,
        scope="cockpit.write",
        timeout_seconds=240,
        maximum_output_bytes=65_536,
    )

    async def _run(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        if not await self._in_chat(context):
            return _failure(
                ToolFailureKind.PERMISSION,
                "cockpit.write_interactive_only",
                "widgets are written only in the owner's interactive chat",
            )
        principal = context.principal
        publication = self._toolset.publication
        operation = arguments["operation"]
        slug = str(arguments["slug"])
        replayed = await publication.replayed(principal, context.idempotency_key)
        if replayed is not None:
            if replayed.operation is OutboxOperation.REMOVE:
                return await self._removal(principal, replayed.slug)
            return await self._outcome(principal, replayed.revision_id, operation)
        if operation in {"create", "update"} and "files" not in arguments:
            return _invalid(f"{operation} carries the package's files")
        expected: UUID | None = None
        if operation == "update":
            if "expected_revision" not in arguments:
                return _invalid(
                    "an update names its expected revision", "cockpit.expected_revision_required"
                )
            expected = UUID(str(arguments["expected_revision"]))
        await context.mark_effect_sent()
        if operation == "delete":
            await publication.delete(principal, slug, request_key=context.idempotency_key)
            return await self._removal(principal, slug)
        if operation == "restore":
            revision = await publication.restore(
                principal, slug, request_key=context.idempotency_key
            )
        else:
            revision = await publication.write(
                principal,
                slug,
                dict(arguments["files"]),
                expected_revision=expected,
                origin_run_id=context.run_id,
                request_key=context.idempotency_key,
            )
        return await self._outcome(principal, revision.id, operation)

    async def _in_chat(self, context: ToolExecutionContext) -> bool:
        """The session filter withholds the tool elsewhere; a call by name is refused here."""

        if context.run_kind != _INTERACTIVE:
            return False
        async with self._toolset.uow_factory() as uow:
            try:
                session = await uow.sessions.get(context.session_id, context.principal)
            except NotFoundError:
                return False
        return offers_widget_writes(session.metadata)

    async def _settled(self, principal: Principal, revision_id: UUID) -> WidgetRevision:
        """The revision once the outbox finished with it, or as it is when the wait runs out."""

        clock = self._toolset.clock
        deadline = clock.now() + timedelta(seconds=self._toolset.commit_wait_seconds)
        while True:
            async with self._toolset.uow_factory() as uow:
                revision = await uow.cockpit_widgets.get_revision(principal, revision_id)
            if revision.state not in _WAITING or clock.now() >= deadline:
                return revision
            await clock.sleep(_COMMIT_POLL_SECONDS)

    async def _outcome(self, principal: Principal, revision_id: UUID, operation: str) -> ToolResult:
        revision = await self._settled(principal, revision_id)
        state = revision.state.value
        body = {
            "slug": revision.slug,
            "operation": operation,
            "revision_id": str(revision.id),
            "state": state,
            "commit_sha": revision.commit_sha,
            "diagnostics": _diagnostics(revision),
        }
        if revision.state is RevisionState.LIVE:
            sentence = f"{revision.slug} is live at commit {revision.commit_sha}."
        elif revision.state is RevisionState.REJECTED:
            sentence = (
                f"{revision.slug} was rejected with {len(revision.diagnostics)} diagnostics; "
                "nothing was published."
            )
        elif revision.state in _WAITING:
            sentence = f"{revision.slug} is accepted and its commit is on its way ({state})."
        else:
            sentence = f"{revision.slug} ended {state}."
        return _ok(sentence, body)

    async def _removal(self, principal: Principal, slug: str) -> ToolResult:
        clock = self._toolset.clock
        deadline = clock.now() + timedelta(seconds=self._toolset.commit_wait_seconds)
        while True:
            async with self._toolset.uow_factory() as uow:
                widget = await uow.cockpit_widgets.get_widget(principal, slug)
            if widget.state is WidgetState.DELETED or clock.now() >= deadline:
                break
            await clock.sleep(_COMMIT_POLL_SECONDS)
        deleted = widget.state is WidgetState.DELETED
        return _ok(
            f"{slug} is deleted." if deleted else f"{slug}'s removal is queued.",
            {"slug": slug, "operation": "delete", "state": "deleted" if deleted else "queued"},
        )

    async def approval_view_in_session(
        self,
        arguments: dict[str, Any],
        *,
        run: object,
        principal: Principal,
        turn: object,
        not_covered: object,
    ) -> ApprovalPresentation:
        """The change as a bounded diff against the live files, in server-authored words."""

        del run, turn, not_covered
        operation = str(arguments.get("operation", ""))
        slug = str(arguments.get("slug", ""))
        live: dict[str, str] = {}
        async with self._toolset.uow_factory() as uow:
            try:
                widget = await uow.cockpit_widgets.get_widget(principal, slug)
            except NotFoundError:
                widget = None
            if widget is not None and widget.live_revision_id is not None:
                revision = await uow.cockpit_widgets.get_revision(
                    principal, widget.live_revision_id
                )
                live = dict(revision.files)
        if operation == "delete":
            before, after = live, {}
        elif operation == "restore":
            before, after = {}, live
        else:
            before, after = live, dict(arguments.get("files") or {})
        changes = sorted(name for name in {*before, *after} if before.get(name) != after.get(name))
        lines: list[str] = []
        for name in changes:
            lines.extend(
                line[:_DIFF_LINE_MAX_CHARS]
                for line in difflib.unified_diff(
                    before.get(name, "").splitlines(),
                    after.get(name, "").splitlines(),
                    fromfile=f"a/{name}" if name in before else "/dev/null",
                    tofile=f"b/{name}" if name in after else "/dev/null",
                    lineterm="",
                )
            )
        verb = {
            "create": "Create",
            "update": "Update",
            "delete": "Remove",
            "restore": "Restore",
        }.get(operation, "Change")
        where = "from" if operation == "delete" else "on"
        view: dict[str, Any] = {
            "operation": operation,
            "slug": slug,
            "file_changes": changes,
            "diff": lines[:_DIFF_MAX_LINES],
            "diff_truncated": len(lines) > _DIFF_MAX_LINES,
        }
        if "expected_revision" in arguments:
            view["expected_revision"] = arguments["expected_revision"]
        return ApprovalPresentation(
            summary=f"{verb} the Cockpit widget {slug} {where} the public branch", arguments=view
        )


class CockpitConfigureTool(_CockpitTool):
    spec = _spec(
        "cockpit.configure",
        "Configure the Cockpit privately: settings sets a live widget's private values, "
        "which must fit its manifest's settings schema; layout arranges one device class's "
        "board (compact, regular or wide) from live widgets at sizes they offer, naming the "
        "layout's current revision as expected_revision (null for a first layout).",
        {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["settings", "layout"]},
                "slug": _SLUG,
                "values": {"type": "object", "maxProperties": 16},
                "device_class": {"type": "string", "enum": [item.value for item in DeviceClass]},
                "placements": {
                    "type": "array",
                    "maxItems": 64,
                    "items": {
                        "type": "object",
                        "properties": {"slug": _SLUG, "size": {"type": "string"}},
                        "required": ["slug", "size"],
                        "additionalProperties": False,
                    },
                },
                "expected_revision": {"type": ["integer", "null"], "minimum": 1},
            },
            "required": ["operation"],
        },
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.IDEMPOTENT,
        scope="cockpit.write",
        timeout_seconds=15,
        maximum_output_bytes=16_384,
    )

    async def _run(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        board = self._toolset.board
        principal = context.principal
        if arguments["operation"] == "settings":
            if "slug" not in arguments or "values" not in arguments:
                return _invalid("settings names a slug and its values")
            try:
                settings = await board.put_settings(
                    principal, str(arguments["slug"]), dict(arguments["values"])
                )
            except CockpitValidationError as exc:
                return _invalid(str(exc), "cockpit.settings_invalid")
            return _ok(
                f"{settings.slug}'s settings are saved.",
                {"slug": settings.slug, "updated_at": _stamp(settings.updated_at)},
            )
        if "device_class" not in arguments or "placements" not in arguments:
            return _invalid("layout names a device class and its placements")
        try:
            request = CockpitLayoutWrite.model_validate(
                {
                    "placements": arguments["placements"],
                    "expected_revision": arguments.get("expected_revision"),
                }
            )
            layout = await board.put_layout(
                principal, DeviceClass(arguments["device_class"]), request
            )
        except CockpitValidationError as exc:
            return _invalid(str(exc), "cockpit.layout_invalid")
        except ValueError as exc:
            return _invalid(str(exc), "cockpit.layout_invalid")
        return _ok(
            f"The {arguments['device_class']} layout is at revision {layout.revision}.",
            {
                "device_class": arguments["device_class"],
                "revision": layout.revision,
                "placements": [p.model_dump(mode="json") for p in layout.placements],
            },
        )


class CockpitFeedPublishTool(_CockpitTool):
    spec = _spec(
        "cockpit.feed.publish",
        "Publish a snapshot to the assistant feed agent.<feed> for Cockpit widgets to show. "
        "The first publish carries the feed's JSON Schema (self-contained, draft 2020-12); "
        "later ones are checked against it, or carry a new schema. Give how often the feed "
        "is refreshed as expected_interval_seconds, and report a failed refresh as error.",
        {
            "type": "object",
            "properties": {
                "feed": {"type": "string", "pattern": _FEED_NAME_PATTERN, "maxLength": 58},
                "schema": {"type": "object"},
                "data": {},
                "expected_interval_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 604_800,
                },
                "error": {"type": "string", "minLength": 1, "maxLength": FEED_ERROR_MAX_CHARS},
                "observed_at": {"type": "string", "format": "date-time"},
            },
            "required": ["feed", "expected_interval_seconds"],
        },
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.IDEMPOTENT,
        scope="cockpit.write",
        timeout_seconds=15,
        maximum_output_bytes=4_096,
    )

    async def _run(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        name = str(arguments["feed"]).removeprefix(_FEED_PREFIX)
        if "data" not in arguments and "error" not in arguments:
            return _invalid("a snapshot carries data or reports an error")
        observed = (
            None
            if arguments.get("observed_at") is None
            else datetime.fromisoformat(str(arguments["observed_at"]))
        )
        if observed is not None and observed.utcoffset() is None:
            return _invalid("observed_at carries its UTC offset")
        feed, snapshot = await self._toolset.feeds.publish(
            context.principal,
            _FEED_PREFIX + name,
            data=arguments.get("data"),
            expected_interval_seconds=int(arguments["expected_interval_seconds"]),
            schema=arguments.get("schema"),
            error=arguments.get("error"),
            observed_at=observed,
        )
        return _ok(
            f"Published {feed.id} snapshot {snapshot.sequence}.",
            {
                "feed": feed.id,
                "sequence": snapshot.sequence,
                "schema_version": feed.schema_version,
                "observed_at": _stamp(snapshot.observed_at),
            },
        )


def cockpit_tools(toolset: CockpitToolset) -> tuple[_CockpitTool, ...]:
    return (
        CockpitReadTool(toolset),
        CockpitLiveCheckTool(toolset),
        CockpitWidgetWriteTool(toolset),
        CockpitConfigureTool(toolset),
        CockpitFeedPublishTool(toolset),
    )
