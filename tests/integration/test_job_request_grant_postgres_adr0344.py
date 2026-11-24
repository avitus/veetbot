"""One approval for a request that changes a saved job, over HTTP and PostgreSQL.

Guards ADR-0344 with docs/plan/http-api-and-streaming.md "Approvals", where
the unit suite stops at the authorizer and an in-memory store:

- Decision 1: a covered job card in the owner's chat carries
  `job_request_grant_offer` (the slug, the access ceilings, 7200 seconds and
  the server-authored summary of decision 6). `approve_for_request` with
  `request_grant: {"job_slug": ...}` needs `approval.resolve` and `jobs.run`,
  refuses another slug and leaves the card pending, is safe to retry, and
  records `job.request_grant.created` with the approval.
- Decision 2: later changes to the same job in the same run start without a
  card, each audited as `job_request_grant`; another job still asks.
- Decision 3: a card for new access names that access as a ceiling beside the
  access the job already holds, and private inputs may change under it.
- Decision 4: a run the request authorized binds its grant to that approval,
  and a second run of the same revision with other access asks.

Each run is asked over the public API, parked and resumed by the durable
worker, the first across a composition restart.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

import httpx
import pytest

from agent_core.application.job_grants import revision_grant_id
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import Principal
from agent_core.domain.events import EventEnvelope
from agent_core.domain.jobs import JobRecord
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.runs import RunStatus
from tests.contract.test_job_store_contract import record
from tests.integration.journey_support import api_client, run_worker_once
from tests.integration.m2_support import database_settings
from tests.unit.test_job_execution import Runner
from tests.unit.test_job_publication import Screen

pytestmark = pytest.mark.integration

SLUG = "count-items"
OTHER_SLUG = "count-others"
REVISION = UUID(int=1)
LATER_REVISION = UUID(int=3)
# The approval that first gave the job its model allowance, and the grant that
# approval bound to the revision it was shown (ADR-0268).
EARLIER_APPROVAL = UUID(int=99)
GRANT = revision_grant_id(EARLIER_APPROVAL, REVISION)
MODEL_COST = "0.15"
GRANT_KIND = "job_request_grant"
NEW_SOURCE = "def main(inputs, job): return {'count': len(inputs['items'])}"
NEWER_SOURCE = "def main(inputs, job): return {'count': len(list(inputs['items']))}"
MANIFEST = {"title": "Count items"}
HOSTS = {"api.github.com": "github-reader"}


def _summary(slug: str) -> str:
    return (
        f"Allow for this request: change, test and run the {slug} job and repin its "
        "schedule using access it already has. Veetbot still asks before new websites, "
        "MCP servers, feeds or larger limits, and before schedule timing changes."
    )


def _job(owner: Principal, slug: str = SLUG) -> JobRecord:
    """A saved job with two published revisions and one grant that reaches
    beyond its own computation.

    Such a grant covers only the revision it was approved for, so a trial of
    changed code against it asks, as the first card of ADR-0344's context did.
    """
    saved = record(owner)
    [revision] = saved.revisions.values()
    [grant] = saved.grants.values()
    later = revision.model_copy(update={"id": LATER_REVISION, "commit_sha": "b" * 40})
    bound = grant.model_copy(
        update={
            "id": GRANT,
            "approval_id": EARLIER_APPROVAL,
            "access": replace(grant.access, model_cost=MODEL_COST),
        }
    )
    return saved.model_copy(
        update={
            "slug": slug,
            "revisions": {**saved.revisions, str(LATER_REVISION): later},
            "grants": {str(GRANT): bound},
        }
    )


def _call(name: str, call_id: str, **arguments: Any) -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[ScriptedToolCall(name=name, arguments=arguments, call_id=call_id)],
        stop_reason=StopReason.TOOL_USE,
    )


def _final(text: str) -> ScriptedTurn:
    return ScriptedTurn(text=text, stop_reason=StopReason.END_TURN)


def _build(script: FakeModelScript, runner: Runner) -> Any:
    return build(
        settings=replace(database_settings(), jobs_enabled=True),
        storage="postgres",
        script=script,
        enabled_tools=["job.manage", "job.run"],
        job_runner_override=runner,
        job_screen_override=Screen(),
        cockpit_local_branch=True,
    )


async def _ask(client: httpx.AsyncClient, text: str, key: str) -> UUID:
    created = await client.post("/v1/sessions", json={"agent_id": "general", "metadata": {}})
    assert created.status_code == 201, created.text
    submitted = await client.post(
        f"/v1/sessions/{created.json()['id']}/messages",
        headers={"Idempotency-Key": key},
        json={"content": [{"type": "text", "text": text}]},
    )
    assert submitted.status_code == 202, submitted.text
    return UUID(submitted.json()["run_id"])


async def _card(client: httpx.AsyncClient, run_id: UUID) -> dict[str, Any]:
    """The one card the parked run is waiting on."""
    parked = await client.get(f"/v1/runs/{run_id}")
    assert parked.json()["status"] == RunStatus.WAITING_FOR_APPROVAL.value, parked.text
    listed = await client.get("/v1/approvals", params={"run_id": str(run_id)})
    assert listed.status_code == 200, listed.text
    [card] = listed.json()["items"]
    assert isinstance(card, dict)
    return card


async def _deny_and_finish(
    composition: Composition, client: httpx.AsyncClient, run_id: UUID, card: dict[str, Any]
) -> list[EventEnvelope]:
    denied = await client.post(f"/v1/approvals/{card['id']}/resolve", json={"decision": "deny"})
    assert denied.status_code == 200, denied.text
    assert await run_worker_once(composition, f"finisher-{card['id']}")
    finished = await client.get(f"/v1/runs/{run_id}")
    assert finished.json()["status"] == RunStatus.COMPLETED.value, finished.text
    return await composition.runs.events(run_id)


def _grant_uses(events: list[EventEnvelope]) -> list[tuple[str, str]]:
    return [
        (str(event.payload["call_id"]), str(event.payload["authorization_ref"]))
        for event in events
        if event.event_type == "tool.call.authorized"
        and event.payload.get("authorization_kind") == GRANT_KIND
    ]


async def test_one_approval_on_a_trial_covers_the_save_and_the_next_trial_after_a_restart() -> None:
    """A repair asks once for its trials and its save; another job asks again."""
    first_claim = FakeModelScript(
        turns=[
            _call("job.manage", "read", operation="read", slug=SLUG),
            _call(
                "job.manage",
                "trial",
                operation="trial",
                slug=SLUG,
                grant_id=str(GRANT),
                source=NEW_SOURCE,
                manifest=MANIFEST,
            ),
        ]
    )
    runner = Runner()
    async with _build(first_claim, runner) as composition, api_client(composition) as client:
        async with composition.uow_factory() as uow:
            await uow.jobs.put(_job(composition.principal), expected_version=None)
            await uow.jobs.put(_job(composition.principal, OTHER_SLUG), expected_version=None)
        run_id = await _ask(client, "Repair the counter job.", "job-request-grant")
        assert await run_worker_once(composition, "job-grant-parker")
        card = await _card(client, run_id)
        approval_id = card["id"]
        assert card["tool_name"] == "job.manage"
        offer = card["job_request_grant_offer"]
        assert (offer["slug"], offer["duration_seconds"], offer["summary"]) == (
            SLUG,
            7200,
            _summary(SLUG),
        )
        # The job holds one grant: its access, without its private inputs, is the ceiling.
        [ceiling] = offer["ceilings"]
        assert (ceiling["hosts"], ceiling["mcp_servers"], ceiling["feeds"]) == ({}, [], [])
        assert ceiling["model_cost"] == MODEL_COST and "inputs" not in ceiling
        assert runner.program is None

        resolve = f"/v1/approvals/{approval_id}/resolve"
        allow = {"decision": "approve_for_request", "request_grant": {"job_slug": SLUG}}
        # Validation: the echo is required, and a different job is not the offer.
        missing = await client.post(resolve, json={"decision": "approve_for_request"})
        assert missing.status_code == 400, missing.text
        assert missing.json()["error"]["code"] == "malformed_request"
        other = await client.post(
            resolve,
            json={"decision": "approve_for_request", "request_grant": {"job_slug": OTHER_SLUG}},
        )
        assert other.status_code == 409, other.text
        assert other.json()["error"]["details"]["reason"] == "request_grant_offer_mismatch"
        # Authorization: allowing a job request needs jobs.run beside approval.resolve.
        without_jobs = composition.principal.model_copy(
            update={"scopes": composition.principal.scopes - {"jobs.run"}}
        )
        async with api_client(composition, principal=without_jobs) as unscoped:
            refused = await unscoped.post(resolve, json=allow)
        assert refused.status_code == 403, refused.text
        assert (await _card(client, run_id))["id"] == approval_id

        allowed = await client.post(resolve, json=allow)
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["decision"] == "approve_for_request"
        # A client's retry is safe.
        retried = await client.post(resolve, json=allow)
        assert retried.status_code == 200, retried.text
        assert retried.json()["resolved_at"] == allowed.json()["resolved_at"]

    after_restart = FakeModelScript(
        turns=[
            _call(
                "job.manage",
                "update",
                operation="update",
                slug=SLUG,
                expected_revision=str(REVISION),
                source=NEW_SOURCE,
                manifest=MANIFEST,
            ),
            _call(
                "job.manage",
                "second-trial",
                operation="trial",
                slug=SLUG,
                grant_id=str(GRANT),
                source=NEWER_SOURCE,
                manifest=MANIFEST,
            ),
            _call(
                "job.manage",
                "other-job",
                operation="update",
                slug=OTHER_SLUG,
                expected_revision=str(REVISION),
                source=NEW_SOURCE,
                manifest=MANIFEST,
            ),
            _final("The counter is repaired."),
        ]
    )
    runner = Runner()
    async with _build(after_restart, runner) as composition, api_client(composition) as client:
        assert await run_worker_once(composition, "job-grant-resumer")
        # The first trial ran under the approval; the save and the second trial
        # needed no card. The other job asks, on a card that offers only itself.
        other_card = await _card(client, run_id)
        assert other_card["id"] != approval_id
        assert other_card["arguments"]["slug"] == OTHER_SLUG
        assert other_card["job_request_grant_offer"]["slug"] == OTHER_SLUG
        assert runner.program is not None and runner.program.source == NEWER_SOURCE
        events = await _deny_and_finish(composition, client, run_id, other_card)
        async with composition.uow_factory() as uow:
            saved = await uow.jobs.get(composition.principal, SLUG)
            untouched = await uow.jobs.get(composition.principal, OTHER_SLUG)

    [created] = [event for event in events if event.event_type == "job.request_grant.created"]
    assert created.payload["approval_id"] == approval_id
    assert created.payload["slug"] == SLUG
    assert _grant_uses(events) == [("update", approval_id), ("second-trial", approval_id)]
    # The save published a new live revision; the denied job kept its own.
    assert len(saved.revisions) == 3
    assert saved.live_revision_id not in {REVISION, LATER_REVISION}
    assert saved.revisions[str(saved.live_revision_id)].source == NEW_SOURCE
    assert untouched.live_revision_id == REVISION and len(untouched.revisions) == 2


async def test_a_run_card_for_new_access_covers_another_revision_and_binds_its_grant() -> None:
    """New access asks once; a second run of one revision with other access asks."""

    def access(*items: int) -> dict[str, Any]:
        return {"inputs": {"items": list(items)}, "hosts": HOSTS}

    model = FakeModelScript(
        turns=[
            _call("job.run", "first", slug=SLUG, revision_id=str(REVISION), access=access(1)),
            _call(
                "job.run",
                "later-revision",
                slug=SLUG,
                revision_id=str(LATER_REVISION),
                access=access(1, 2, 3),
            ),
            _call(
                "job.run",
                "other-inputs",
                slug=SLUG,
                revision_id=str(LATER_REVISION),
                access=access(1, 2, 3, 4),
            ),
            _final("Counted."),
        ]
    )
    runner = Runner()
    async with _build(model, runner) as composition, api_client(composition) as client:
        async with composition.uow_factory() as uow:
            await uow.jobs.put(_job(composition.principal), expected_version=None)
        run_id = await _ask(client, "Count the items with the GitHub reader.", "job-run-grant")
        assert await run_worker_once(composition, "job-run-parker")
        card = await _card(client, run_id)
        approval_id = card["id"]
        assert card["tool_name"] == "job.run"
        # The card's own access comes first, then the access the job already holds.
        offer = card["job_request_grant_offer"]
        assert offer["slug"] == SLUG and offer["summary"] == _summary(SLUG)
        assert [item["hosts"] for item in offer["ceilings"]] == [HOSTS, {}]
        assert runner.inputs is None

        allowed = await client.post(
            f"/v1/approvals/{approval_id}/resolve",
            json={"decision": "approve_for_request", "request_grant": {"job_slug": SLUG}},
        )
        assert allowed.status_code == 200, allowed.text
        assert await run_worker_once(composition, "job-run-resumer")
        # The later revision ran with the same host and other private inputs,
        # unasked. A second run of that revision with other access would
        # replace the grant the first bound, so it asks.
        collision = await _card(client, run_id)
        assert collision["id"] != approval_id
        assert collision["arguments"]["access"]["inputs"] == {"items": [1, 2, 3, 4]}
        assert runner.inputs == {"items": [1, 2, 3]}
        events = await _deny_and_finish(composition, client, run_id, collision)
        async with composition.uow_factory() as uow:
            saved = await uow.jobs.get(composition.principal, SLUG)

    assert _grant_uses(events) == [("later-revision", approval_id)]
    assert runner.inputs == {"items": [1, 2, 3]}
    # Each run the approval covered bound one grant to it, by revision.
    first = saved.grants[str(revision_grant_id(UUID(approval_id), REVISION))]
    later = saved.grants[str(revision_grant_id(UUID(approval_id), LATER_REVISION))]
    assert first.approval_id == later.approval_id == UUID(approval_id)
    assert (first.access.inputs, later.access.inputs) == (
        {"items": [1]},
        {"items": [1, 2, 3]},
    )
    assert first.access.hosts == later.access.hosts == HOSTS
    assert set(saved.grants) == {str(GRANT), str(first.id), str(later.id)}
