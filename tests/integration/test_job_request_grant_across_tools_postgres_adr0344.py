"""A job request approval covers the job's other tools, over HTTP and PostgreSQL.

Guards ADR-0344 decision 2 in the composed runtime: one "Allow for this
request" covers, for the offered job, a `job.manage` trial and update, a
`job.run` of any published revision with access that fits a ceiling, and a
`schedule.update` repin. Its context is the radar repair, where the first card
was a trial and the later save, run and repin were meant to ride that one
approval.

`job.manage` is judged by the "<profile>-jobs" policy profile
(`agent_core.policy.jobs.JobPolicyEngine`, ADR-0268) and `job.run` and
`schedule.update` by the base profile, so the card and the later action carry
different policy versions while the policy itself is unchanged. The unit suite
shows the refusal when either profile really changes; this module shows that
the two profiles of one composition are one policy to the grant, in both
directions.
"""

from __future__ import annotations

import pytest

from agent_core.domain.messages import FakeModelScript
from agent_core.domain.runs import RunStatus
from tests.integration.journey_support import api_client, run_worker_once
from tests.integration.test_job_request_grant_postgres_adr0344 import (
    GRANT,
    LATER_REVISION,
    MANIFEST,
    MODEL_COST,
    NEW_SOURCE,
    REVISION,
    SLUG,
    _ask,
    _build,
    _call,
    _card,
    _final,
    _grant_uses,
    _job,
)
from tests.unit.test_job_execution import Runner

pytestmark = pytest.mark.integration

ALLOW = {"decision": "approve_for_request", "request_grant": {"job_slug": SLUG}}


async def test_a_grant_allowed_on_a_trial_card_covers_a_run_that_fits_its_ceiling() -> None:
    """Decision 2: after the trial's card, running a published revision with
    the access the job already holds and other private inputs needs no card."""
    fitting = {"inputs": {"items": [1, 2, 3]}, "hosts": {}, "model_cost": MODEL_COST}
    model = FakeModelScript(
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
            _call("job.run", "run", slug=SLUG, revision_id=str(LATER_REVISION), access=fitting),
            _final("Repaired and ran."),
        ]
    )
    runner = Runner()
    async with _build(model, runner) as composition, api_client(composition) as client:
        async with composition.uow_factory() as uow:
            await uow.jobs.put(_job(composition.principal), expected_version=None)
        run_id = await _ask(client, "Repair the counter job and run it.", "job-grant-cross-tool")
        assert await run_worker_once(composition, "cross-tool-parker")
        card = await _card(client, run_id)
        assert card["tool_name"] == "job.manage"
        assert card["job_request_grant_offer"]["slug"] == SLUG
        allowed = await client.post(f"/v1/approvals/{card['id']}/resolve", json=ALLOW)
        assert allowed.status_code == 200, allowed.text
        assert await run_worker_once(composition, "cross-tool-resumer")

        finished = await client.get(f"/v1/runs/{run_id}")
        events = await composition.runs.events(run_id)
        assert finished.json()["status"] == RunStatus.COMPLETED.value, finished.text

    assert _grant_uses(events) == [("run", card["id"])]
    assert runner.inputs == {"items": [1, 2, 3]}


async def test_a_grant_allowed_on_a_run_card_covers_the_save_of_the_same_job() -> None:
    """Decision 2: after a run's card, saving a new revision of that job needs no card."""
    access = {"inputs": {"items": [1]}, "hosts": {"api.github.com": "github-reader"}}
    model = FakeModelScript(
        turns=[
            _call("job.manage", "read", operation="read", slug=SLUG),
            _call("job.run", "run", slug=SLUG, revision_id=str(REVISION), access=access),
            _call(
                "job.manage",
                "update",
                operation="update",
                slug=SLUG,
                expected_revision=str(REVISION),
                source=NEW_SOURCE,
                manifest=MANIFEST,
            ),
            _final("Ran and saved."),
        ]
    )
    async with _build(model, Runner()) as composition, api_client(composition) as client:
        async with composition.uow_factory() as uow:
            await uow.jobs.put(_job(composition.principal), expected_version=None)
        run_id = await _ask(client, "Run the counter job, then save the fix.", "job-grant-reverse")
        assert await run_worker_once(composition, "reverse-parker")
        card = await _card(client, run_id)
        assert card["tool_name"] == "job.run"
        allowed = await client.post(f"/v1/approvals/{card['id']}/resolve", json=ALLOW)
        assert allowed.status_code == 200, allowed.text
        assert await run_worker_once(composition, "reverse-resumer")

        finished = await client.get(f"/v1/runs/{run_id}")
        events = await composition.runs.events(run_id)
        assert finished.json()["status"] == RunStatus.COMPLETED.value, finished.text

    assert _grant_uses(events) == [("update", card["id"])]
