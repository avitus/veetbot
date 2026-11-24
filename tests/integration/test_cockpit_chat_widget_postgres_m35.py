"""A widget asked for in Chat reaches the owner's Cockpit, on PostgreSQL.

Guards docs/plan/cockpit.md "The tools", "Writing", "Delete, restore and
revert" and "The routes and the stream", and its 2026-10-24 implementation
checkpoint (until the exact-name policy entry lands, the publication asks the
owner), as one chain over the public API and PostgreSQL:

- the owner asks Chat for a widget; ``cockpit.widget.write`` parks the run on
  an approval that is listed and approved over HTTP;
- the resumed run records the accepted package and answers; nothing is on the
  board until the commit outbox, as the maintenance role runs it, commits the
  package to the branch;
- the snapshot, the widget detail and the bundle asset then serve exactly what
  the model wrote, after a restart too;
- the owner places, deletes and restores the widget through the same outbox,
  each change answering ``202`` and reaching the stream's event log;
- a principal without ``cockpit.write`` is refused, and an unknown widget is
  ``404``.

The builder is the evaluation's manifest-checking one and the branch is the
composition's in-memory repository, as in the deterministic evaluation; the
package is the ``approvals-waiting`` reference widget.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_core.bootstrap import Composition, build
from agent_core.config import Settings
from agent_core.domain.agents import Principal
from agent_core.domain.runs import RunStatus
from agent_core.evals.cockpit import ManifestWidgetBuilder
from agent_core.evals.fixtures import resolve_model_fixture
from tests.integration.journey_support import api_client, run_worker_once
from tests.integration.m2_support import database_settings

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 24, 17, tzinfo=UTC)
SLUG = "approvals-waiting"
REFERENCE = ROOT / "cockpit/examples/widgets" / SLUG
REQUEST = "Make me a Cockpit widget that shows how many approvals are waiting on me."
PUBLISHED = "The Approvals widget is published; it shows on your Cockpit once its commit lands."


def _reference() -> dict[str, str]:
    return {
        path.relative_to(REFERENCE).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(REFERENCE.rglob("*"))
        if path.is_file()
    }


def _settings(tmp_path: Path) -> Settings:
    return replace(database_settings(), cockpit_enabled=True, artifact_root=tmp_path / "artifacts")


def _build(settings: Settings, *, scripted: bool) -> Any:
    return build(
        settings=settings,
        storage="postgres",
        script=(
            resolve_model_fixture(ROOT / "evals/fixtures/models", "cockpit_widget_create")
            if scripted
            else None
        ),
        fixed_clock_at=NOW,
        enabled_tools=["cockpit.read", "cockpit.widget.write"] if scripted else None,
        cockpit_builder_override=ManifestWidgetBuilder(ROOT),
        cockpit_local_branch=True,
    )


async def _commit_queued_changes(app: Composition) -> int:
    """One pass of the commit outbox, which the maintenance role runs on a timer."""
    assert app.cockpit_local_branch is not None and app.cockpit_local_outbox is not None
    await app.cockpit_local_branch.ensure_branch()
    return await app.cockpit_local_outbox.run_once()


async def _event_kinds(app: Composition) -> list[str]:
    async with app.uow_factory() as uow:
        events = await uow.cockpit_events.after(app.principal, sequence=0, limit=50)
    return [event.kind.value for event in events]


def _viewer() -> Principal:
    return Principal(
        tenant_id="local", principal_id="local-user", roles={"user"}, scopes={"cockpit.read"}
    )


async def test_a_widget_asked_for_in_chat_is_approved_committed_and_served(
    tmp_path: Path,
) -> None:
    """Chat writes the package, the owner approves, the outbox commits, the board serves it."""
    settings = _settings(tmp_path)
    reference = _reference()

    async with _build(settings, scripted=True) as app, api_client(app) as client:
        empty = await client.get("/v1/cockpit")
        assert empty.status_code == 200, empty.text
        assert empty.headers["Cache-Control"] == "private, no-store"
        assert empty.json()["widgets"] == []

        created = await client.post("/v1/sessions", json={"agent_id": "general", "metadata": {}})
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]
        submitted = await client.post(
            f"/v1/sessions/{session_id}/messages",
            headers={"Idempotency-Key": "ask-for-a-widget"},
            json={"content": [{"type": "text", "text": REQUEST}]},
        )
        assert submitted.status_code == 202, submitted.text
        run_id = submitted.json()["run_id"]

        assert await run_worker_once(app, "cockpit-chat")
        parked = await client.get(f"/v1/runs/{run_id}")
        assert parked.json()["status"] == RunStatus.WAITING_FOR_APPROVAL.value, parked.text
        listing = await client.get("/v1/approvals", params={"run_id": run_id})
        assert listing.status_code == 200, listing.text
        [approval] = listing.json()["items"]
        assert approval["tool_name"] == "cockpit.widget.write"
        assert approval["status"] == "PENDING"
        assert approval["arguments"]["operation"] == "create"
        assert approval["arguments"]["slug"] == SLUG
        # The card shows the whole change: a create compares against nothing.
        pages = approval["arguments"]["diff"]
        shown = [line for page in sorted(pages) for line in pages[page]]
        assert [line[6:] for line in shown if line.startswith("+++ b/")] == sorted(reference)
        assert {line for line in shown if line.startswith("--- ")} == {"--- /dev/null"}
        for text in reference.values():
            for line in text.splitlines():
                assert "+" + line in shown
        # Nothing is accepted, queued or shown before the owner decides.
        assert (await client.get("/v1/cockpit")).json()["widgets"] == []
        assert (await client.get(f"/v1/cockpit/widgets/{SLUG}")).status_code == 404
        assert await _commit_queued_changes(app) == 0

        resolved = await client.post(
            f"/v1/approvals/{approval['id']}/resolve", json={"decision": "approve_once"}
        )
        assert resolved.status_code == 200, resolved.text
        assert await run_worker_once(app, "cockpit-chat-resumed")
        completed = await client.get(f"/v1/runs/{run_id}")
        assert completed.json()["status"] == RunStatus.COMPLETED.value, completed.text
        messages = await client.get(f"/v1/sessions/{session_id}/messages")
        assert PUBLISHED in messages.text

        # Accepted but not committed: the package waits for the outbox and the
        # board still shows nothing.
        waiting = await client.get(f"/v1/cockpit/widgets/{SLUG}")
        assert waiting.status_code == 200, waiting.text
        assert waiting.json()["live_revision_id"] is None
        [pending] = waiting.json()["revisions"]
        assert pending["state"] == "pending_commit"
        assert (await client.get("/v1/cockpit")).json()["widgets"] == []

        assert await _commit_queued_changes(app) == 1
        assert await _commit_queued_changes(app) == 0
        assert app.cockpit_local_branch is not None
        head = await app.cockpit_local_branch.head()
        assert head is not None

        snapshot = (await client.get("/v1/cockpit")).json()
        [widget] = snapshot["widgets"]
        assert widget["slug"] == SLUG
        assert widget["revision_id"] == pending["id"]
        assert widget["manifest"] == json.loads(reference["widget.json"])
        assert widget["settings"] == {}
        assert snapshot["layouts"] == {}
        detail = (await client.get(f"/v1/cockpit/widgets/{SLUG}")).json()
        assert detail["state"] == "active"
        assert detail["live_revision_id"] == pending["id"]
        [live] = detail["revisions"]
        assert live["state"] == "live"
        assert detail["settings_schema"] == json.loads(reference["widget.json"])["settings"]
        assert detail["import_failures"] == []
        async with app.uow_factory() as uow:
            stored = await uow.cockpit_widgets.get_revision(app.principal, UUID(pending["id"]))
        assert dict(stored.files) == reference
        assert stored.commit_sha == head

        asset = await client.get(f"/v1/cockpit/assets/{widget['bundle_sha256']}")
        assert asset.status_code == 200, asset.text
        assert hashlib.sha256(asset.content).hexdigest() == widget["bundle_sha256"]
        assert asset.headers["Cache-Control"] == "private, max-age=31536000, immutable"
        assert asset.headers["X-Content-Type-Options"] == "nosniff"
        assert await _event_kinds(app) == ["widget.revised"]

        # The owner's own changes go through the same outbox with no approval.
        placed = await client.put(
            "/v1/cockpit/layouts/wide",
            json={"placements": [{"slug": SLUG, "size": "md"}], "expected_revision": None},
        )
        assert placed.status_code == 200, placed.text
        unoffered = await client.put(
            "/v1/cockpit/layouts/wide",
            json={"placements": [{"slug": SLUG, "size": "lg"}], "expected_revision": 1},
        )
        assert unoffered.status_code == 400, unoffered.text
        assert unoffered.json()["error"]["code"] == "malformed_request"

        deleted = await client.post(f"/v1/cockpit/widgets/{SLUG}/delete", json={})
        assert deleted.status_code == 202, deleted.text
        assert deleted.headers["Cache-Control"] == "private, no-store"
        assert {key: deleted.json()[key] for key in ("slug", "operation", "state")} == {
            "slug": SLUG,
            "operation": "delete",
            "state": "queued",
        }
        early = await client.post(f"/v1/cockpit/widgets/{SLUG}/restore", json={})
        assert early.status_code == 409, early.text
        # Still live until the removal commits.
        still = (await client.get("/v1/cockpit")).json()
        assert [item["slug"] for item in still["widgets"]] == [SLUG]
        assert await _commit_queued_changes(app) == 1
        gone = (await client.get("/v1/cockpit")).json()
        assert gone["widgets"] == []
        assert gone["layouts"]["wide"]["placements"] == []
        assert (await client.get(f"/v1/cockpit/widgets/{SLUG}")).json()["state"] == "deleted"

        restored = await client.post(f"/v1/cockpit/widgets/{SLUG}/restore", json={})
        assert restored.status_code == 202, restored.text
        assert restored.json()["state"] == "pending_commit"
        assert await _commit_queued_changes(app) == 1
        board = (await client.get("/v1/cockpit")).json()
        [widget] = board["widgets"]
        assert widget["slug"] == SLUG
        assert widget["revision_id"] != pending["id"]
        assert widget["manifest"] == json.loads(reference["widget.json"])
        assert board["cursor"] > snapshot["cursor"]
        kinds = await _event_kinds(app)
        assert [kind for kind in kinds if kind != "layout.updated"] == [
            "widget.revised",
            "widget.deleted",
            "widget.revised",
        ]
        assert "layout.updated" in kinds

    # A restart serves the same board from PostgreSQL.
    async with _build(settings, scripted=False) as app, api_client(app) as client:
        again = (await client.get("/v1/cockpit")).json()
        assert again["widgets"] == board["widgets"]
        assert again["layouts"] == board["layouts"]
        assert again["cursor"] == board["cursor"]
        asset = await client.get(f"/v1/cockpit/assets/{widget['bundle_sha256']}")
        assert asset.status_code == 200, asset.text

        undeclared = await client.put(
            f"/v1/cockpit/widgets/{SLUG}/settings", json={"values": {"place": "Example City"}}
        )
        assert undeclared.status_code == 400, undeclared.text
        assert undeclared.json()["error"]["code"] == "malformed_request"
        async with api_client(app, principal=_viewer()) as viewer:
            assert (await viewer.get("/v1/cockpit")).status_code == 200
            for path in ("delete", "restore"):
                refused = await viewer.post(f"/v1/cockpit/widgets/{SLUG}/{path}", json={})
                assert refused.status_code == 403, (path, refused.text)
        missing = await client.post("/v1/cockpit/widgets/nothing/delete", json={})
        assert missing.status_code == 404, missing.text
        assert (await client.get("/v1/cockpit")).json()["widgets"] == board["widgets"]
