"""Owner recall probe scenarios shared by the in-memory and PostgreSQL suites."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from agent_core.bootstrap import Composition
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import BeliefType, MemoryRecord, MemoryStatus, Sensitivity
from agent_core.domain.messages import FakeModelScript
from agent_core.domain.web import WebPage, WebSearchRequest, WebSearchResult
from agent_core.evals.owner_recall_probe_driver import Finish, _wait, observe_probes
from agent_core.evals.owner_recall_probes import (
    PROBE_REPORT_VERSION,
    ExpectedEvidence,
    Probe,
    ProbeReport,
    WriteProbe,
)

SAVE_QUESTION = (
    "Look up pruning seasons, then remember that pruning schedules for roses, "
    "hydrangeas, hibiscus and azaleas belong on the calendar."
)
VERBATIM_STATEMENT = (
    "pruning schedules for roses, hydrangeas, hibiscus and azaleas belong on the calendar."
)
PARAPHRASED_STATEMENT = (
    "Andy wants pruning schedules for roses, hydrangeas, hibiscus and azaleas added."
)


@dataclass
class FakeWebProvider:
    name: str = "fake-web"
    searches: list[WebSearchRequest] = field(default_factory=list)

    async def search(self, request: WebSearchRequest) -> tuple[WebSearchResult, ...]:
        self.searches.append(request)
        return (
            WebSearchResult(
                title="Pruning seasons",
                url="https://example.org/pruning",
                snippet="Prune roses in late winter and hydrangeas after flowering.",
            ),
        )

    async def fetch(self, url: str) -> WebPage:
        return WebPage(url=url, title="Pruning seasons", content="Prune roses in late winter.")

    async def close(self) -> None:
        return


async def stated(app: Composition, session_id: UUID, statement: str, subject: str) -> MemoryRecord:
    """State one belief the way a user turn would, with real provenance."""

    async with app.uow_factory() as uow:
        event = await uow.events.append(
            NewEvent(
                session_id=session_id,
                run_id=None,
                event_type="user.message.created",
                actor_type="principal",
                actor_id=app.principal.principal_id,
                payload={"content": statement},
            )
        )
    return await app.memory.remember(
        session_id=session_id,
        run_id=None,
        statement=statement,
        subject=subject,
        scope="general",
        belief_type=BeliefType.FACT,
        sensitivity=Sensitivity.INTERNAL,
        source_event_ids=[event.sequence],
        explicit=True,
    )


async def delivery_case(
    app: Composition, script: FakeModelScript, *, finish: Finish = _wait
) -> tuple[ProbeReport, MemoryRecord, MemoryRecord]:
    """A delivered belief, a retired one, and a latency ceiling, through the run loop."""

    session_id = await app.sessions.create()
    car = await stated(app, session_id, "User has a 2026 Aster S8.", "car")
    retired = await stated(app, session_id, "Andy confirmed the CEO list.", "CEO list")
    async with app.uow_factory() as uow:
        record = await uow.memories.get(retired.id, app.principal)
        await uow.memories.reinforce(
            record.model_copy(
                update={"status": MemoryStatus.RETIRED, "valid_to": app.clock.now()}, deep=True
            )
        )
    probes = (
        Probe(
            id="car",
            incident="car",
            family="attributes",
            question="What fuel should I use for my car?",
            search_text="car Aster S8",
            paths=("automatic", "deliberate"),
            expected=(ExpectedEvidence(statement_contains="Aster S8"),),
            latency_ceiling_ms=5_000,
        ),
        Probe(
            id="ceo",
            incident="ceo-list",
            family="decisions",
            question="Who is the CEO of CircleCI?",
            paths=("automatic",),
            expected=(ExpectedEvidence(belief_id=retired.id),),
        ),
    )
    report = await observe_probes(app, script, probes, finish=finish)
    assert_delivery_report(report, car)
    return report, car, retired


def assert_delivery_report(report: ProbeReport, car: MemoryRecord) -> None:
    assert report.version == PROBE_REPORT_VERSION
    assert report.activation_evidence is False and report.release_evidence is False
    assert set(report.implementation) >= {"memory/retrieval.py", "memory/profiles.yaml"}
    by_id = {observation.probe_id: observation for observation in report.probes}
    car_paths = {path.path: path for path in by_id["car"].paths}
    assert set(car_paths) == {"automatic", "deliberate"}
    for path in car_paths.values():
        assert path.run_id is not None and path.trace_ids
        assert path.run_status == "COMPLETED"
        assert path.elapsed_ms >= 0 and path.within_latency_ceiling is True
        (evidence,) = path.evidence
        assert evidence.belief_id == car.id
        assert evidence.stage == "delivered"
        assert evidence.rank is not None and evidence.rank >= 1
    assert car_paths["deliberate"].delivered_trace_ids, "the search block was not delivered"
    (ceo_path,) = by_id["ceo"].paths
    (ceo_evidence,) = ceo_path.evidence
    assert ceo_evidence.stage == "stored_but_retired"
    assert report.summary.required_evidence == 3
    assert report.summary.required_delivered == 2
    assert report.summary.facts_missed == 1
    assert report.summary.wrong_or_outdated == 0
    assert "Aster S8" not in report.model_dump_json()


def write_probes() -> tuple[Probe, Probe]:
    verbatim = Probe(
        id="verbatim",
        incident="garden-calendar-save",
        family="temporal",
        question=SAVE_QUESTION,
        paths=(),
        write=WriteProbe(
            statement=VERBATIM_STATEMENT, subject="gardening calendar", web_results=True
        ),
    )
    assert verbatim.write is not None
    paraphrase = verbatim.model_copy(
        update={
            "id": "paraphrase",
            "write": verbatim.write.model_copy(update={"statement": PARAPHRASED_STATEMENT}),
        }
    )
    return verbatim, paraphrase


async def save_case(
    app: Composition, script: FakeModelScript, web: FakeWebProvider, *, finish: Finish = _wait
) -> ProbeReport:
    """The Saved stage after web results.

    A verbatim owner span commits; a paraphrase reports its refusal reason.
    """

    report = await observe_probes(app, script, write_probes(), finish=finish)
    assert len(web.searches) == 2
    saves = {observation.probe_id: observation.save for observation in report.probes}
    assert saves["verbatim"] is not None and saves["verbatim"].status == "committed"
    assert saves["verbatim"].belief_id is not None
    assert saves["paraphrase"] is not None
    assert saves["paraphrase"].status == "save_refused:tool.memory.manual_trust_rejected"
    assert report.summary.saves_refused == 1 and report.summary.saves_committed == 1
    return report
