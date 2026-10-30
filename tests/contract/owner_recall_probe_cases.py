"""Owner recall probe scenarios shared by the in-memory and PostgreSQL suites."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.bootstrap import Composition
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import (
    BeliefType,
    MemoryRecord,
    MemoryStatus,
    RecallMoment,
    RecallTrace,
    Sensitivity,
)
from agent_core.domain.messages import FakeModelScript, TextPart, UserMessage
from agent_core.domain.policies import TrustLevel
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


async def home_city_case(
    app: Composition, script: FakeModelScript, *, finish: Finish = _wait
) -> tuple[ProbeReport, MemoryRecord]:
    """The garden-calendar shape (ADR-0248): an affirmed home address beside
    five automatically captured email facts about the same subject. None of
    them may link to the address, and the address must be delivered on both
    paths for a location-dependent task."""

    from agent_core.domain.memory import MemoryAuthority

    session_id = await app.sessions.create()
    address = await stated(
        app, session_id, "Andy Vitus's home address is 12 Oak Lane, Los Altos Hills.", "Andy Vitus"
    )
    noise = (
        "Andy Vitus reported current mouthpieces to the band.",
        "Andy Vitus asked for the rehearsal recording.",
        "Andy Vitus confirmed the dentist appointment for Tuesday.",
        "Andy Vitus renewed the magazine subscription.",
        "Andy Vitus thanked the caterer for the quote.",
    )
    for statement in noise:
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
        await app.memory.remember(
            session_id=session_id,
            run_id=None,
            statement=statement,
            subject="Andy Vitus",
            scope="general",
            belief_type=BeliefType.FACT,
            sensitivity=Sensitivity.INTERNAL,
            source_event_ids=[event.sequence],
            explicit=False,
            authority=MemoryAuthority.INFERRED,
        )
    async with app.uow_factory() as uow:
        stored = await uow.memories.get(address.id, app.principal)
    assert stored.conflicts_with == [] and stored.flagged_for_review is False
    probes = (
        Probe(
            id="home-city-address",
            incident="garden-calendar",
            family="attributes",
            question="Maintain a gardening calendar based on what you know about our garden.",
            search_text="home garden city location climate zone",
            paths=("automatic", "deliberate"),
            expected=(ExpectedEvidence(statement_contains="Los Altos Hills"),),
            min_required_rendered=1,
            answer_must_contain=("Los Altos Hills",),
            answer_must_not_contain=("which city",),
        ),
    )
    report = await observe_probes(app, script, probes, finish=finish)
    (observation,) = report.probes
    paths = {path.path: path for path in observation.paths}
    assert set(paths) == {"automatic", "deliberate"}
    for path in paths.values():
        assert path.run_status == "COMPLETED"
        (evidence,) = path.evidence
        assert evidence.belief_id == address.id
    # The explicit location search reaches the address. A scripted answer is
    # never assessed; the live-model run on a clone checks "Los Altos Hills".
    deliberate = paths["deliberate"]
    assert deliberate.evidence[0].stage == "delivered", deliberate
    assert deliberate.answer_correct is None
    # The automatic path once reproduced production trace d851e572: the task
    # shares no word with the address. "Our garden" now anchors the owner's
    # residence (+home-location@1), so the address reaches the first request.
    assert paths["automatic"].evidence[0].stage == "delivered", paths["automatic"]
    return report, address


async def post_snapshot_home_case(app: Composition, *, finish: Finish = _wait) -> None:
    """A residence stated after the session's snapshot still reaches a garden task.

    The first turn freezes the snapshot. The residence and an unrelated fact
    stated afterwards share no word with the next turn's household-place
    request, so the residence anchor carries the first and only the delta can
    carry the second. The delta is text-free and therefore unfiltered: turning
    the anchor on there would narrow it to residence statements alone.
    """

    provider = app.executor._model_provider
    planner = app.executor._context_planner
    assert isinstance(provider, FakeModelProvider)
    session_id = await app.sessions.create()
    await stated(app, session_id, "User prefers dark themes.", "theme")
    await finish(app, await app.runs.submit("How should you answer?", session_id))
    plan = await planner.current(session_id)
    assert plan is not None and plan.snapshot_id is not None

    residence = await stated(app, session_id, "User lives in Exampleville, CA.", "home location")
    deploys = await stated(app, session_id, "User deploys on Fridays.", "deploys")
    assert min(residence.store_position, deploys.store_position) > plan.snapshot_watermark

    task = await finish(
        app,
        await app.runs.submit(
            "Maintain a gardening calendar based on what you know about our garden.", session_id
        ),
    )
    request = provider.requests[-1]
    body = request.conversation[int(request.metadata["region_a_items"]) :]
    delivered = "\n".join(
        part.text
        for item in body
        if isinstance(item, UserMessage) and item.trust is TrustLevel.MEMORY
        for part in item.content
        if isinstance(part, TextPart)
    )
    assert residence.statement in delivered
    assert deploys.statement in delivered

    async with app.uow_factory() as uow:
        events = await uow.events.list_after(session_id, 0, app.principal)
        traces: list[RecallTrace] = [
            await uow.traces.get(UUID(str(event.payload["trace_id"])), app.principal)
            for event in events
            if event.event_type == "memory.recalled" and event.run_id == task.id
        ]
    in_turn = [trace for trace in traces if trace.moment is RecallMoment.IN_TURN]
    (base,) = [trace for trace in in_turn if trace.query.text is not None]
    (delta,) = [trace for trace in in_turn if trace.query.text is None]
    assert base.query.include_home_location is True
    assert delta.query.include_home_location is False
    assert delta.query.min_store_position == plan.snapshot_watermark
    assert deploys.id in delta.returned
    assert residence.id in base.returned


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


WEB_DERIVED_STATEMENT = (
    "Prune roses in late winter and hydrangeas after flowering; hibiscus and azaleas "
    "follow the calendar."
)


def write_probes() -> tuple[Probe, ...]:
    """Four saves after web results: verbatim, paraphrase, same-step paraphrase, web-derived."""

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

    def variant(probe_id: str, statement: str, *, same_step: bool = False) -> Probe:
        assert verbatim.write is not None
        return verbatim.model_copy(
            update={
                "id": probe_id,
                "write": verbatim.write.model_copy(
                    update={"statement": statement, "same_step": same_step}
                ),
            }
        )

    return (
        verbatim,
        variant("paraphrase", PARAPHRASED_STATEMENT),
        variant("same-step", PARAPHRASED_STATEMENT, same_step=True),
        variant("web-derived", WEB_DERIVED_STATEMENT),
    )


async def save_case(
    app: Composition, script: FakeModelScript, web: FakeWebProvider, *, finish: Finish = _wait
) -> ProbeReport:
    """The Saved stage after web results.

    A verbatim owner span and an owner paraphrase commit, in the step after the
    web read or the same step; a statement carrying the web result's own words
    is refused with the closed owner-grounding reason.
    """

    report = await observe_probes(app, script, write_probes(), finish=finish)
    assert len(web.searches) == 4
    saves = {observation.probe_id: observation.save for observation in report.probes}
    for probe_id in ("verbatim", "paraphrase", "same-step"):
        save = saves[probe_id]
        assert save is not None and save.status == "committed", (probe_id, save)
        assert save.belief_id is not None
    refused = saves["web-derived"]
    assert refused is not None
    assert refused.status == "save_refused:tool.memory.statement_not_owner_grounded"
    assert report.summary.saves_committed == 3 and report.summary.saves_refused == 1
    return report


async def family_names_case(
    app: Composition, script: FakeModelScript, *, finish: Finish = _wait
) -> ProbeReport:
    """An owner overview delivers family as People lines, not as one belief each.

    The private family probe named three memory ids and reported them
    `stored` while the overview showed the same people through other edges.
    It checks the names the delivered recall renders instead.
    """

    from tests.contract.owner_overview_cases import TIERED, seed_closeness

    await seed_closeness(app, TIERED)
    probe = Probe.model_validate(
        {
            "id": "family-names",
            "incident": "owner-family-overview",
            "family": "relationships",
            "question": "Tell me everything you know about me.",
            "paths": ["automatic"],
            "delivered_must_contain": ["Erin", "Riv", "Kyrri", "Cousin", "Cousin2"],
        }
    )
    report = await observe_probes(app, script, (probe,), finish=finish)
    (observation,) = report.probes
    (path,) = observation.paths
    assert path.run_status == "COMPLETED"
    # ADR-0265: page one reaches every tier, so the first cousin is delivered;
    # it stops at eight of nine people, so the second cousin waits for the continuation.
    assert path.delivered_found == (True, True, True, True, False)
    assert report.summary.required_evidence == 5
    assert report.summary.required_delivered == 4
    assert report.summary.facts_missed == 1
    assert "Erin" not in report.model_dump_json()
    return report
