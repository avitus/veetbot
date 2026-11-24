"""The owner's recall question loop, end to end on PostgreSQL.

Guards ADR-0339 decision 8 and ADR-0346 across the layers the other suites
cover one at a time: a question is added over HTTP, the probe tool runs the
stored set through the composed run loop, the report is recorded, and the
page's list shows which questions the latest run passed.

- ADR-0339 decision 8: the stored active questions run as probes; a result is
  stages and counts, moves no revision, and is bound to the content it
  tested. An edit that changes what a question measures clears the result, a
  note leaves it, and a report measured against older content is stale.
- ADR-0346 decisions 1 and 5: an expected text names a fact, which any live
  memory holding the text delivers, and the report names its scoring rule.
- ADR-0339 decision 4: a retired question is left out of a run.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_core.bootstrap import build
from agent_core.domain.memory import BeliefType, Portability, Sensitivity
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.evals.owner_recall_probe_driver import observe_probes
from agent_core.evals.owner_recall_probes import PROBE_REPORT_VERSION, ProbeReport
from agent_core.evals.owner_recall_questions import loaded_from_questions, results_from_report
from agent_core.runtime.worker import DurableWorker
from tests.contract.owner_recall_probe_cases import stated
from tests.integration.journey_support import api_client
from tests.integration.test_memory_postgres_m9 import _settings

pytestmark = pytest.mark.integration

BASE = "/v1/recall-questions"


def _question(key: str, split: str, question: str, fact: str) -> dict[str, Any]:
    return {
        "key": key,
        "split": split,
        "content": {
            "question": question,
            "family": "attributes",
            "expected": [{"statement_contains": fact}],
        },
    }


async def _finish(app: Any, run_id: UUID) -> Any:
    worker = app.worker_factory("recall-question-loop")
    assert isinstance(worker, DurableWorker)
    assert await asyncio.wait_for(worker.run_once(), 60)
    return await asyncio.wait_for(app.runs.wait_terminal(run_id), 60)


async def test_stored_questions_run_are_recorded_and_show_on_the_page(tmp_path: Path) -> None:
    script = FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last")
    settings = replace(_settings(tmp_path), recall_questions_enabled=True)
    async with (
        build(settings=settings, storage="postgres", script=script) as app,
        api_client(app) as client,
    ):
        service = app.services.recall_questions
        assert service is not None
        car = await client.post(
            BASE, json=_question("car", "development", "What fuel does my car take?", "Aster S8")
        )
        boat = await client.post(
            BASE, json=_question("boat", "holdout", "What is my boat called?", "Zephyr Nine")
        )
        bike = await client.post(
            BASE, json=_question("bike", "holdout", "Which bike do I ride?", "Corsa Uno")
        )
        assert [reply.status_code for reply in (car, boat, bike)] == [201, 201, 201]
        retired = await client.post(
            f"{BASE}/{bike.json()['id']}/status",
            json={"expected_revision": 1, "status": "retired"},
        )
        assert retired.status_code == 200, retired.text

        # The owner's statement, and a newer memory that only mentions the car
        # and is local to another project, so this chat can never be given it.
        session_id = await app.sessions.create()
        owned = await stated(app, session_id, "User has a 2026 Aster S8.", "car")
        mention = await app.memory.remember(
            session_id=session_id,
            run_id=None,
            statement="The dealer newsletter advertised the Aster S8.",
            subject="newsletter",
            scope="another-project",
            portability=Portability.LOCAL,
            belief_type=BeliefType.FACT,
            sensitivity=Sensitivity.INTERNAL,
            source_event_ids=list(owned.source_event_ids),
            explicit=True,
        )
        assert mention.store_position > owned.store_position

        # The probe tool's own path: the stored active set, in key order.
        loaded = loaded_from_questions(await service.stored_questions(app.principal), label="all")
        assert [probe.id for probe in loaded.probes] == ["boat", "car"]
        report = await observe_probes(
            app, script, loaded.probes, finish=_finish, probe_digest=loaded.digest
        )
        assert report.version == PROBE_REPORT_VERSION == "owner-recall-probes@2"
        by_key = {observation.probe_id: observation for observation in report.probes}
        for path in by_key["car"].paths:
            (evidence,) = path.evidence
            # Any live memory holding the text delivers the fact: the older
            # statement stands for it although the mention is newer.
            assert (evidence.stage, evidence.belief_id) == ("delivered", owned.id)
        report_bytes = report.model_dump_json().encode("utf-8")
        results = results_from_report(
            ProbeReport.model_validate_json(report_bytes),
            report_digest=hashlib.sha256(report_bytes).hexdigest(),
            recorded_at=app.clock.now(),
        )
        outcome = await service.record_results(app.principal, results)
        assert (outcome.recorded, outcome.stale, outcome.unknown) == (2, 0, 0)

        listed = await client.get(BASE)
        assert listed.status_code == 200, listed.text
        page = {item["key"]: item for item in listed.json()["items"]}
        assert (page["car"]["passed"], page["boat"]["passed"], page["bike"]["passed"]) == (
            True,
            False,
            None,
        )
        # A result is stages and counts for both paths, and it is not an edit.
        recorded = page["car"]["latest_result"]
        assert recorded["report_digest"] == hashlib.sha256(report_bytes).hexdigest()
        assert {path["path"] for path in recorded["paths"]} == {"automatic", "deliberate"}
        assert all(
            (path["stages"], path["required_delivered"], path["required_total"])
            == (["delivered"], 1, 1)
            for path in recorded["paths"]
        )
        assert all(
            path["required_delivered"] == 0 for path in page["boat"]["latest_result"]["paths"]
        )
        assert "Aster" not in str(recorded) and str(owned.id) not in str(recorded)
        assert page["car"]["revision"] == 1 and page["boat"]["revision"] == 1

        # A note keeps the result. Changing what the question measures clears it.
        content = page["car"]["content"]
        noted = await client.put(
            f"{BASE}/{page['car']['id']}",
            json={"expected_revision": 1, "content": {**content, "notes": "Checked in spring."}},
        )
        assert noted.status_code == 200, noted.text
        assert noted.json()["revision"] == 2 and noted.json()["passed"] is True
        changed = await client.put(
            f"{BASE}/{page['car']['id']}",
            json={
                "expected_revision": 2,
                "content": {**content, "expected": [{"statement_contains": "2026 Aster"}]},
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["passed"] is None and changed.json()["latest_result"] is None

        # The same report again is stale for the edited question and never
        # shows a pass beside content that has not run.
        again = await service.record_results(app.principal, results)
        assert (again.recorded, again.stale, again.unknown) == (1, 1, 0)
        after = {item["key"]: item for item in (await client.get(BASE)).json()["items"]}
        assert after["car"]["passed"] is None and after["boat"]["passed"] is False
