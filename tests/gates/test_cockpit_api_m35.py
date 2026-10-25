"""Cockpit routes and the resumable stream (Milestone 35, gates 14 and 15).

docs/plan/cockpit.md, the routes and the stream. Every route is absent unless
`AGENT_COCKPIT_ENABLED=1` and requires its scope; every answer but an asset is
private and unstored; the stream resumes from `Last-Event-ID`, resets past
retention, heartbeats, closes at its lifetime and never crosses principals.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import (
    CockpitEventKind,
    Feed,
    FeedSnapshot,
    RevisionSource,
    RevisionState,
    Widget,
    WidgetRevision,
    WidgetSettings,
    WidgetState,
)
from agent_core.policy.scopes import PLATFORM_SCOPES
from tests.gates.cockpit_publication_support import ScriptedBuilder, package
from tests.integration.m2_support import memory_settings

OWNER = Principal(
    tenant_id="local",
    principal_id="local-user",
    roles={"user"},
    scopes={"cockpit.read", "cockpit.write"},
)
OTHER = OWNER.model_copy(update={"principal_id": "someone-else"})
NOW = datetime(2026, 10, 23, 18, 0, tzinfo=UTC)
MANIFEST = {
    "schema": 1,
    "slug": "weather",
    "title": "Weather",
    "purpose": "Shows the weather for one place.",
    "kit_api": 1,
    "sizes": ["sm", "md"],
    "feeds": [{"id": "agent.weather", "history": 2}],
    "settings": {
        "type": "object",
        "additionalProperties": False,
        "required": ["place"],
        "properties": {"place": {"type": "string", "maxLength": 40}},
    },
}
ROUTES = {
    ("GET", "/v1/cockpit"): "cockpit.read",
    ("GET", "/v1/cockpit/stream"): "cockpit.read",
    ("GET", "/v1/cockpit/assets/{sha256}"): "cockpit.read",
    ("GET", "/v1/cockpit/widgets/{slug}"): "cockpit.read",
    ("PUT", "/v1/cockpit/widgets/{slug}/settings"): "cockpit.write",
    ("PUT", "/v1/cockpit/layouts/{device_class}"): "cockpit.write",
    ("POST", "/v1/cockpit/widgets/{slug}/revert"): "cockpit.write",
    ("POST", "/v1/cockpit/widgets/{slug}/delete"): "cockpit.write",
    ("POST", "/v1/cockpit/widgets/{slug}/restore"): "cockpit.write",
}


def _overlay(tmp_path: Path, **stream: int) -> Path:
    (tmp_path / "cockpit").mkdir(parents=True, exist_ok=True)
    lines = ["schema_version: 1", "stream:"] + [
        f"  {key}: {value}" for key, value in stream.items()
    ]
    (tmp_path / "cockpit/profiles.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


@asynccontextmanager
async def cockpit_api(
    *,
    enabled: bool = True,
    principal: Principal = OWNER,
    config_dir: Path | None = None,
    artifact_root: Path | None = None,
) -> AsyncIterator[tuple[Composition, httpx.AsyncClient]]:
    settings = replace(memory_settings(), cockpit_enabled=enabled, config_dir=config_dir)
    if artifact_root is not None:
        settings = replace(settings, artifact_root=artifact_root)
    async with build(settings=settings, storage="memory", principal=principal) as composition:
        api = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api, client=("127.0.0.1", 1234)),
            base_url="http://localhost",
        ) as client:
            yield composition, client


async def _live_widget(
    composition: Composition, owner: Principal = OWNER, slug: str = "weather"
) -> WidgetRevision:
    bundle = f"(()=>{{}})();//{slug}"
    files = {**package(), "widget.json": json.dumps({**MANIFEST, "slug": slug})}
    async with composition.uow_factory() as uow:
        await uow.cockpit_widgets.create_widget(
            Widget(
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                slug=slug,
                state=WidgetState.ACTIVE,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        revision = await uow.cockpit_widgets.add_revision(
            WidgetRevision(
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                id=UUID(int=hash(slug) % 10**9 + (7 if owner is OTHER else 0)),
                slug=slug,
                source=RevisionSource.ASSISTANT,
                state=RevisionState.COMMITTED,
                files=files,
                bundle=bundle,
                bundle_sha256=hashlib.sha256(bundle.encode()).hexdigest(),
                kit_api=1,
                commit_sha="c" * 40,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await uow.cockpit_widgets.activate(owner, slug, revision.id, updated_at=NOW)
        await uow.cockpit_widgets.put_settings(
            WidgetSettings(
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                slug=slug,
                values={"place": "Example City"},
                updated_at=NOW,
            )
        )
    return revision


async def _event(composition: Composition, owner: Principal, subject: str) -> int:
    async with composition.uow_factory() as uow:
        event = await uow.cockpit_events.append(
            owner, kind=CockpitEventKind.FEED_UPDATED, subject=subject, occurred_at=NOW
        )
    return event.sequence


def _routes(client: httpx.AsyncClient) -> list[Any]:
    app = client._transport.app  # type: ignore[attr-defined]
    return [
        nested
        for route in app.routes
        for nested in (
            route.original_router.routes if hasattr(route, "original_router") else (route,)
        )
    ]


def _frames(body: str) -> list[dict[str, Any]]:
    frames = []
    for block in body.split("\n\n"):
        fields: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith(":"):
                fields["comment"] = line[1:].strip()
            elif ": " in line:
                key, _, value = line.partition(": ")
                fields[key] = json.loads(value) if key == "data" else value
        if fields:
            frames.append(fields)
    return frames


def test_the_cockpit_scopes_join_the_closed_vocabulary() -> None:
    assert {"cockpit.read", "cockpit.write"} <= PLATFORM_SCOPES


async def test_off_means_every_cockpit_route_is_absent() -> None:
    async with cockpit_api(enabled=False) as (composition, client):
        paths = [getattr(route, "path", "") for route in _routes(client)]
        assert not [path for path in paths if path.startswith("/v1/cockpit")]
        for (method, path), _scope in ROUTES.items():
            concrete = path.replace("{sha256}", "a" * 64).replace("{slug}", "weather")
            concrete = concrete.replace("{device_class}", "wide")
            response = await client.request(method, concrete, json={})
            assert response.status_code == 404, (method, path)
        assert composition.services.cockpit is None
        assert composition.cockpit_publication is None
        assert composition.cockpit_builder is None
        assert composition.cockpit_sandbox is None
        worker = composition.maintenance_factory()
        assert worker._sweep_cockpit_outbox is None  # type: ignore[attr-defined]
        assert worker._sweep_cockpit_import is None  # type: ignore[attr-defined]
        assert worker._sweep_cockpit_events is None  # type: ignore[attr-defined]
        assert worker._sweep_cockpit_feeds is None  # type: ignore[attr-defined]
        assert worker._sweep_cockpit_feed_history is None  # type: ignore[attr-defined]
        registry: Any = composition.tool_pipeline._registry
        assert not [name for name in registry._latest if name.startswith("cockpit.")]


async def test_on_mounts_exactly_the_nine_routes_under_their_scopes() -> None:
    async with cockpit_api() as (_composition, client):
        mounted = {
            (method, route.path): (route.openapi_extra or {}).get("required_scope")
            for route in _routes(client)
            if getattr(route, "path", "").startswith("/v1/cockpit")
            for method in route.methods - {"HEAD"}
        }
        assert mounted == ROUTES


async def test_each_route_refuses_a_principal_without_its_scope() -> None:
    for missing in ("cockpit.read", "cockpit.write"):
        owner = OWNER.model_copy(update={"scopes": {"cockpit.read", "cockpit.write"} - {missing}})
        async with cockpit_api(principal=owner) as (_composition, client):
            for (method, path), scope in ROUTES.items():
                if scope != missing:
                    continue
                concrete = path.replace("{sha256}", "a" * 64).replace("{slug}", "weather")
                concrete = concrete.replace("{device_class}", "wide")
                response = await client.request(method, concrete, json={})
                assert response.status_code == 403, (method, path)


async def test_the_snapshot_carries_live_widgets_settings_layouts_feeds_and_the_cursor() -> None:
    async with cockpit_api() as (composition, client):
        revision = await _live_widget(composition)
        await _live_widget(composition, OTHER, "markets")
        async with composition.uow_factory() as uow:
            await uow.cockpit_feeds.register(
                Feed(
                    tenant_id=OWNER.tenant_id,
                    principal_id=OWNER.principal_id,
                    id="agent.weather",
                    schema={"type": "object"},
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            for minute in range(4):
                await uow.cockpit_feeds.publish(
                    FeedSnapshot(
                        tenant_id=OWNER.tenant_id,
                        principal_id=OWNER.principal_id,
                        feed_id="agent.weather",
                        sequence=1,
                        observed_at=NOW.replace(minute=minute),
                        expected_interval_seconds=300,
                        data={"temperature": 60 + minute},
                        recorded_at=NOW.replace(minute=minute),
                    ),
                    history_max=10,
                )
        cursor = await _event(composition, OWNER, "agent.weather")
        response = await client.get("/v1/cockpit")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        snapshot = response.json()
        assert snapshot["cursor"] == cursor
        [widget] = snapshot["widgets"]
        assert widget["slug"] == "weather"
        assert widget["revision_id"] == str(revision.id)
        assert widget["bundle_sha256"] == revision.bundle_sha256
        assert widget["manifest"]["sizes"] == ["sm", "md"]
        assert widget["settings"] == {"place": "Example City"}
        # The client renders with server-corrected time and quarantines the newest change first.
        assert datetime.fromisoformat(widget["changed_at"]) == NOW
        assert (
            abs(datetime.fromisoformat(snapshot["server_time"]) - datetime.now(UTC)).total_seconds()
            < 60
        )
        feed = snapshot["feeds"]["agent.weather"]
        assert feed["trust"] == "untrusted"
        assert feed["current"]["data"] == {"temperature": 63}
        assert [item["data"] for item in feed["history"]] == [
            {"temperature": 61},
            {"temperature": 62},
        ]
        assert snapshot["layouts"] == {}


async def test_assets_are_served_by_digest_with_immutable_private_caching() -> None:
    async with cockpit_api() as (composition, client):
        revision = await _live_widget(composition)
        other = await _live_widget(composition, OTHER, "markets")
        response = await client.get(f"/v1/cockpit/assets/{revision.bundle_sha256}")
        assert response.status_code == 200
        assert response.text == revision.bundle
        assert response.headers["content-type"].startswith("text/javascript")
        assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
        assert response.headers["x-content-type-options"] == "nosniff"
        for missing in (other.bundle_sha256, "0" * 64, "not-a-digest"):
            refused = await client.get(f"/v1/cockpit/assets/{missing}")
            assert refused.status_code == 404, missing


async def test_runtime_assets_come_from_the_exported_manifest(tmp_path: Path) -> None:
    async with cockpit_api(artifact_root=tmp_path / "artifacts") as (composition, client):
        runtime = composition.settings.artifact_root / "cockpit-runtime"
        runtime.mkdir(parents=True)
        realm = b"<!doctype html><title>realm</title>"
        digest = hashlib.sha256(realm).hexdigest()
        (runtime / f"{digest}.html").write_bytes(realm)
        (runtime / "manifest.json").write_text(
            json.dumps(
                {
                    "kit_api": 1,
                    "entry": f"{digest}.html",
                    "assets": {"realm.html": {"name": f"{digest}.html", "sha256": digest}},
                }
            ),
            encoding="utf-8",
        )
        snapshot = (await client.get("/v1/cockpit")).json()
        assert snapshot["runtime"] == {
            "kit_api": 1,
            "entry": digest,
            "assets": {"realm.html": digest},
        }
        served = await client.get(f"/v1/cockpit/assets/{digest}")
        assert served.status_code == 200
        assert served.content == realm
        assert served.headers["content-type"].startswith("text/html")


async def test_widget_detail_is_the_owners_and_a_foreign_widget_is_absent() -> None:
    async with cockpit_api() as (composition, client):
        revision = await _live_widget(composition)
        await _live_widget(composition, OTHER, "markets")
        detail = await client.get("/v1/cockpit/widgets/weather")
        assert detail.status_code == 200
        assert detail.headers["cache-control"] == "private, no-store"
        body = detail.json()
        assert body["state"] == "active"
        assert body["live_revision_id"] == str(revision.id)
        assert [item["id"] for item in body["revisions"]] == [str(revision.id)]
        assert body["revisions"][0]["state"] == "live"
        assert body["settings_schema"] == MANIFEST["settings"]
        assert body["import_failures"] == []
        for absent in ("markets", "nothing"):
            assert (await client.get(f"/v1/cockpit/widgets/{absent}")).status_code == 404


async def test_settings_writes_follow_the_live_manifest_and_emit_an_event() -> None:
    async with cockpit_api() as (composition, client):
        await _live_widget(composition)
        written = await client.put(
            "/v1/cockpit/widgets/weather/settings", json={"values": {"place": "Harbor"}}
        )
        assert written.status_code == 200, written.text
        assert written.json()["values"] == {"place": "Harbor"}
        bad_values: tuple[dict[str, Any], ...] = (
            {"values": {}},
            {"values": {"place": "x" * 41}},
            {"values": {"other": "x"}},
        )
        for bad in bad_values:
            refused = await client.put("/v1/cockpit/widgets/weather/settings", json=bad)
            assert refused.status_code == 400, bad
        assert (
            await client.put("/v1/cockpit/widgets/markets/settings", json={"values": {}})
        ).status_code == 404
        async with composition.uow_factory() as uow:
            events = await uow.cockpit_events.after(OWNER, sequence=0, limit=10)
            stored = await uow.cockpit_widgets.get_settings(OWNER, "weather")
        assert [(event.kind, event.subject) for event in events] == [
            (CockpitEventKind.SETTINGS_UPDATED, "weather")
        ]
        assert stored is not None and stored.values == {"place": "Harbor"}


async def test_layout_writes_place_live_widgets_by_revision() -> None:
    async with cockpit_api() as (composition, client):
        await _live_widget(composition)
        first = await client.put(
            "/v1/cockpit/layouts/wide",
            json={"placements": [{"slug": "weather", "size": "md"}], "expected_revision": None},
        )
        assert first.status_code == 200, first.text
        assert first.json()["revision"] == 1
        stale = await client.put(
            "/v1/cockpit/layouts/wide", json={"placements": [], "expected_revision": None}
        )
        assert stale.status_code == 409
        for bad in (
            [{"slug": "weather", "size": "wide"}],
            [{"slug": "markets", "size": "sm"}],
        ):
            refused = await client.put(
                "/v1/cockpit/layouts/wide", json={"placements": bad, "expected_revision": 1}
            )
            assert refused.status_code == 400, bad
        second = await client.put(
            "/v1/cockpit/layouts/wide", json={"placements": [], "expected_revision": 1}
        )
        assert second.json()["revision"] == 2
        snapshot = (await client.get("/v1/cockpit")).json()
        assert snapshot["layouts"]["wide"] == {"revision": 2, "placements": []}
        async with composition.uow_factory() as uow:
            kinds = [e.kind for e in await uow.cockpit_events.after(OWNER, sequence=0, limit=9)]
        assert kinds == [CockpitEventKind.LAYOUT_UPDATED, CockpitEventKind.LAYOUT_UPDATED]


async def test_owner_revert_delete_and_restore_go_through_the_outbox() -> None:
    async with cockpit_api() as (composition, client):
        assert composition.cockpit_publication is not None
        composition.cockpit_publication._builder = ScriptedBuilder()
        revision = await _live_widget(composition)
        reverted = await client.post(
            "/v1/cockpit/widgets/weather/revert", json={"revision_id": str(revision.id)}
        )
        assert reverted.status_code == 202, reverted.text
        assert reverted.json()["state"] == "pending_commit"
        missing = await client.post(
            "/v1/cockpit/widgets/weather/revert", json={"revision_id": str(UUID(int=4))}
        )
        assert missing.status_code == 404
        deleted = await client.post("/v1/cockpit/widgets/weather/delete", json={})
        assert deleted.status_code == 202
        assert {key: deleted.json()[key] for key in ("slug", "operation", "state")} == {
            "slug": "weather",
            "operation": "delete",
            "state": "queued",
        }
        early = await client.post("/v1/cockpit/widgets/weather/restore", json={})
        assert early.status_code == 409
        async with composition.uow_factory() as uow:
            queued = await uow.cockpit_outbox.due("local", now=datetime.now(UTC), limit=10)
        assert [entry.operation.value for entry in queued] == ["write", "remove"]


async def _resumes_without_loss_or_duplication_and_closes_at_its_lifetime(
    tmp_path: Path,
) -> None:
    async with cockpit_api(config_dir=_overlay(tmp_path, max_seconds=1)) as (composition, client):
        first = await _event(composition, OWNER, "a")
        second = await _event(composition, OWNER, "b")
        await _event(composition, OTHER, "foreign")
        third = await _event(composition, OWNER, "c")
        response = await client.get("/v1/cockpit/stream", headers={"Last-Event-ID": str(first)})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-accel-buffering"] == "no"
        frames = [frame for frame in _frames(response.text) if "event" in frame]
        assert [(frame["id"], frame["event"], frame["data"]) for frame in frames] == [
            (str(second), "feed.updated", {"subject": "b"}),
            (str(third), "feed.updated", {"subject": "c"}),
        ]


async def _a_resume_point_past_retention_is_told_to_refetch(tmp_path: Path) -> None:
    async with cockpit_api(config_dir=_overlay(tmp_path, max_seconds=5)) as (composition, client):
        old = await _event(composition, OWNER, "old")
        await _event(composition, OWNER, "kept")
        async with composition.uow_factory() as uow:
            pruned = await uow.cockpit_events.prune_before("local", before=NOW.replace(year=2027))
        assert pruned == 2
        response = await client.get("/v1/cockpit/stream", headers={"Last-Event-ID": str(old)})
        assert [frame.get("event") for frame in _frames(response.text)] == ["stream.reset"]


async def test_a_stream_without_a_resume_point_starts_live_and_new_events_arrive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agent_core.application.cockpit_views._IDLE_POLL_SECONDS", 0.05)
    async with cockpit_api(config_dir=_overlay(tmp_path, max_seconds=1)) as (composition, client):
        await _event(composition, OWNER, "before")

        async def later() -> int:
            await asyncio.sleep(0.3)
            return await _event(composition, OWNER, "during")

        appended = asyncio.create_task(later())
        response = await client.get("/v1/cockpit/stream")
        frames = [frame for frame in _frames(response.text) if "event" in frame]
        assert [(frame["id"], frame["data"]) for frame in frames] == [
            (str(await appended), {"subject": "during"})
        ]


async def test_an_event_committed_while_the_stream_subscribes_is_still_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a resume point the stream takes its cursor before it subscribes,
    so an event that commits while it subscribes is after the cursor and is
    replayed rather than folded into it and lost."""

    monkeypatch.setattr("agent_core.application.cockpit_views._IDLE_POLL_SECONDS", 0.05)
    async with cockpit_api(config_dir=_overlay(tmp_path, max_seconds=1)) as (composition, client):
        board = composition.services.cockpit
        assert board is not None
        live_events = board._live_events
        sequences: list[int] = []

        class AppendsWhileSubscribing:
            @asynccontextmanager
            async def listen(self, channel: str) -> AsyncIterator[Any]:
                async with live_events.listen(channel) as subscription:
                    sequences.append(await _event(composition, OWNER, "while subscribing"))
                    yield subscription

        monkeypatch.setattr(board, "_live_events", AppendsWhileSubscribing())
        response = await client.get("/v1/cockpit/stream")
        frames = [frame for frame in _frames(response.text) if "event" in frame]
        assert [(frame["id"], frame["data"]) for frame in frames] == [
            (str(sequences[0]), {"subject": "while subscribing"})
        ]


async def test_a_sweep_while_the_stream_subscribes_still_resets_a_pruned_resume_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pruned-cursor check rides in the first replay's unit of work, after
    the fetch, so a retention sweep between connecting and the first page
    yields `stream.reset`, never a silently shortened replay."""

    async with cockpit_api(config_dir=_overlay(tmp_path, max_seconds=1)) as (composition, client):
        old = await _event(composition, OWNER, "old")
        await _event(composition, OWNER, "kept")
        board = composition.services.cockpit
        assert board is not None
        live_events = board._live_events

        class SweepsWhileSubscribing:
            @asynccontextmanager
            async def listen(self, channel: str) -> AsyncIterator[Any]:
                async with live_events.listen(channel) as subscription:
                    async with composition.uow_factory() as uow:
                        await uow.cockpit_events.prune_before(
                            "local", before=NOW.replace(year=2027)
                        )
                    yield subscription

        monkeypatch.setattr(board, "_live_events", SweepsWhileSubscribing())
        response = await client.get("/v1/cockpit/stream", headers={"Last-Event-ID": str(old)})
        assert [frame.get("event") for frame in _frames(response.text)] == ["stream.reset"]


async def _heartbeats_while_idle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as patched:
        patched.setattr("agent_core.api.cockpit.HEARTBEAT_SECONDS", 0.2)
        async with cockpit_api(config_dir=_overlay(tmp_path, max_seconds=1)) as (_c, client):
            response = await client.get("/v1/cockpit/stream")
    assert any(frame.get("comment") == "heartbeat" for frame in _frames(response.text))


async def test_the_stream_resumes_resets_heartbeats_and_closes_at_its_lifetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate 14: resume without loss or duplication, never across principals, reset
    past retention, heartbeat while idle, and a clean close at the lifetime."""

    await _resumes_without_loss_or_duplication_and_closes_at_its_lifetime(tmp_path / "resume")
    await _a_resume_point_past_retention_is_told_to_refetch(tmp_path / "reset")
    await _heartbeats_while_idle(tmp_path / "heartbeat", monkeypatch)


async def test_a_malformed_resume_point_is_refused_before_the_stream_opens() -> None:
    async with cockpit_api() as (_composition, client):
        for header in ("soon", "-1"):
            response = await client.get("/v1/cockpit/stream", headers={"Last-Event-ID": header})
            assert response.status_code == 400, header
