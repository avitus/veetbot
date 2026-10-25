"""The Cockpit snapshot the Apple client decodes (docs/plan/cockpit.md, the routes).

`tests/fixtures/api/cockpit_snapshot_contract.json` is the `GET /v1/cockpit`
answer for one board, rendered by the real route over a fixed clock: a live
widget with private settings, a declared feed with history, a layout and an
exported runtime. The Apple client decodes the same file, copied into its test
bundle with its checksum asserted, so a change to the wire shape fails both
sides. Set `VEETBOT_REGENERATE_COCKPIT_FIXTURE=1` to rewrite it after an
intended change, then copy it and update the Swift checksum.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx

from agent_core.api import create_app
from agent_core.bootstrap import build
from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import (
    DeviceClass,
    Feed,
    FeedSnapshot,
    Layout,
    Placement,
    RevisionSource,
    RevisionState,
    TileSize,
    Widget,
    WidgetRevision,
    WidgetSettings,
    WidgetState,
)
from tests.gates.cockpit_publication_support import package
from tests.integration.m2_support import memory_settings

FIXTURE = Path(__file__).parents[1] / "fixtures" / "api" / "cockpit_snapshot_contract.json"
OWNER = Principal(
    tenant_id="local",
    principal_id="local-user",
    roles={"user"},
    scopes={"cockpit.read", "cockpit.write"},
)
NOW = datetime(2026, 10, 24, 17, 0, tzinfo=UTC)
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
        "properties": {"place": {"type": "string", "maxLength": 40}},
    },
}
BUNDLE = "(()=>{})();//weather"


def _runtime(root: Path) -> dict[str, str]:
    directory = root / "cockpit-runtime"
    directory.mkdir(parents=True)
    assets: dict[str, dict[str, object]] = {}
    for name, content in (("host.html", "<host>"), ("realm.html", "<realm>"), ("kit.css", "/*k*/")):
        digest = hashlib.sha256(content.encode()).hexdigest()
        (directory / f"{digest}.{name.rsplit('.', 1)[1]}").write_text(content, encoding="utf-8")
        assets[name] = {"name": f"{digest}.{name.rsplit('.', 1)[1]}", "sha256": digest, "bytes": 1}
    entry = str(assets["host.html"]["name"])
    (directory / "manifest.json").write_text(
        json.dumps({"kit_api": 1, "entry": entry, "assets": assets}), encoding="utf-8"
    )
    return {name: str(asset["sha256"]) for name, asset in assets.items()}


async def _rendered(tmp_path: Path) -> dict[str, object]:
    _runtime(tmp_path)
    settings = replace(memory_settings(), cockpit_enabled=True, artifact_root=tmp_path)
    async with build(
        settings=settings, storage="memory", principal=OWNER, fixed_clock_at=NOW
    ) as composition:
        revision = WidgetRevision(
            tenant_id=OWNER.tenant_id,
            principal_id=OWNER.principal_id,
            id=UUID(int=7),
            slug="weather",
            source=RevisionSource.ASSISTANT,
            state=RevisionState.COMMITTED,
            files={**package(), "widget.json": json.dumps(MANIFEST)},
            bundle=BUNDLE,
            bundle_sha256=hashlib.sha256(BUNDLE.encode()).hexdigest(),
            kit_api=1,
            commit_sha="c" * 40,
            created_at=NOW,
            updated_at=NOW,
        )
        async with composition.uow_factory() as uow:
            widgets = uow.cockpit_widgets
            await widgets.create_widget(
                Widget(
                    tenant_id=OWNER.tenant_id,
                    principal_id=OWNER.principal_id,
                    slug="weather",
                    state=WidgetState.ACTIVE,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            await widgets.add_revision(revision)
            await widgets.activate(OWNER, "weather", revision.id, updated_at=NOW)
            await widgets.put_settings(
                WidgetSettings(
                    tenant_id=OWNER.tenant_id,
                    principal_id=OWNER.principal_id,
                    slug="weather",
                    values={"place": "Example City"},
                    updated_at=NOW,
                )
            )
            await widgets.put_layout(
                Layout(
                    tenant_id=OWNER.tenant_id,
                    principal_id=OWNER.principal_id,
                    device_class=DeviceClass.WIDE,
                    placements=(Placement(slug="weather", size=TileSize.MD),),
                    revision=1,
                    updated_at=NOW,
                ),
                expected_revision=None,
            )
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
            for minute in range(3):
                await uow.cockpit_feeds.publish(
                    FeedSnapshot(
                        tenant_id=OWNER.tenant_id,
                        principal_id=OWNER.principal_id,
                        feed_id="agent.weather",
                        sequence=1,
                        observed_at=NOW.replace(minute=minute),
                        expected_interval_seconds=900,
                        data={"temperature": 60 + minute},
                        recorded_at=NOW.replace(minute=minute),
                    ),
                    history_max=10,
                )
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
            response = await client.get("/v1/cockpit")
    assert response.status_code == 200
    body: dict[str, object] = response.json()
    return body


async def test_the_snapshot_fixture_is_what_the_route_renders(tmp_path: Path) -> None:
    rendered = await _rendered(tmp_path)
    if os.environ.get("VEETBOT_REGENERATE_COCKPIT_FIXTURE") == "1":
        FIXTURE.write_text(json.dumps(rendered, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == rendered
