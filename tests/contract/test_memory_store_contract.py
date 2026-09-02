"""Structured belief-store contract."""

from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.memory.in_memory import InMemoryMemoryStore
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.memory import (
    BeliefRejection,
    BeliefType,
    ConsolidationRun,
    MemoryEdit,
    MemoryRecord,
    MemoryStatus,
    RejectionKind,
    Sensitivity,
    lexical_query_terms,
    lexical_term_lexemes,
    lexical_text_matches,
)
from tests.contract.memory_fixtures import browse_query, memory, recall_query
from tests.contract.support import NOW, PRINCIPAL_ID, SESSION_ID, TENANT, principal


def _store() -> InMemoryMemoryStore:
    return InMemoryMemoryStore(FixedClock(NOW))


async def test_memory_store_enforces_scope_and_lifecycle() -> None:
    store = _store()
    value = memory()
    await store.upsert_belief(value)
    assert await store.query(recall_query()) == [value]
    assert await store.query(recall_query(tenant_id="tenant-b")) == []
    with pytest.raises(NotFoundError):
        await store.get(value.id, principal().model_copy(update={"principal_id": "other"}))


async def test_upsert_is_idempotent_but_rejects_content_drift() -> None:
    store = _store()
    value = memory()
    await store.upsert_belief(value)
    await store.upsert_belief(value)
    assert await store.query(recall_query()) == [value]
    with pytest.raises(ConflictError):
        await store.upsert_belief(value.model_copy(update={"statement": "Different content"}))


async def test_reinforce_requires_an_existing_belief() -> None:
    store = _store()
    with pytest.raises(NotFoundError):
        await store.reinforce(memory())
    await store.upsert_belief(memory())
    reinforced = memory().model_copy(update={"corroboration_count": 2, "store_position": 2})
    await store.reinforce(reinforced)
    assert (await store.get(reinforced.id, principal())).corroboration_count == 2


async def test_supersede_retires_the_current_belief_exactly_once() -> None:
    store = _store()
    value = memory()
    await store.upsert_belief(value)
    replacement = memory(belief_id=502, statement="User prefers detailed answers").model_copy(
        update={"store_position": 2}
    )
    superseded = value.model_copy(
        update={
            "status": MemoryStatus.SUPERSEDED,
            "valid_to": NOW,
            "superseded_by": replacement.id,
            "store_position": 3,
        }
    )
    await store.supersede(superseded, replacement)

    stored = await store.get(value.id, principal())
    assert stored.status is MemoryStatus.SUPERSEDED
    assert stored.superseded_by == replacement.id
    assert stored.valid_to is not None
    live = await store.query(recall_query(text="answers"))
    assert [record.id for record in live] == [replacement.id]
    with pytest.raises(ConflictError):
        await store.supersede(superseded, replacement)


async def test_related_matches_subject_casefold_and_live_records_only() -> None:
    store = _store()
    await store.upsert_belief(memory())
    retired = memory(belief_id=502, statement="Old belief about answers").model_copy(
        update={"status": MemoryStatus.RETIRED, "valid_to": NOW, "store_position": 2}
    )
    await store.upsert_belief(retired)
    related = await store.related(TENANT, PRINCIPAL_ID, "ANSWER STYLE", BeliefType.PREFERENCE)
    assert [record.id for record in related] == [memory().id]
    assert await store.related(TENANT, PRINCIPAL_ID, "answer style", BeliefType.FACT) == []


async def test_list_memories_orders_by_recency_and_honors_liveness_and_limit() -> None:
    store = _store()
    first = memory(belief_id=511).model_copy(update={"store_position": 1})
    second = memory(belief_id=512, statement="User prefers tabs").model_copy(
        update={"subject": "indentation", "store_position": 2}
    )
    retired = memory(belief_id=513, statement="Old retired belief").model_copy(
        update={
            "subject": "history",
            "status": MemoryStatus.RETIRED,
            "valid_to": NOW,
            "store_position": 3,
        }
    )
    for record in (first, second, retired):
        await store.upsert_belief(record)

    live = await store.list_memories(principal())
    assert [record.id for record in live] == [second.id, first.id]
    everything = await store.list_memories(principal(), include_inactive=True)
    assert [record.id for record in everything] == [retired.id, second.id, first.id]
    limited = await store.list_memories(principal(), include_inactive=True, limit=1)
    assert [record.id for record in limited] == [retired.id]
    other_session = second.model_copy(
        update={"id": UUID(int=514), "source_session_id": UUID(int=999), "store_position": 4}
    )
    await store.upsert_belief(other_session)
    session_only = await store.list_memories(
        principal(), include_inactive=True, session_id=SESSION_ID
    )
    assert [record.id for record in session_only] == [retired.id, second.id, first.id]


async def test_edit_and_delete_are_principal_scoped() -> None:
    store = _store()
    value = memory()
    await store.upsert_belief(value)
    foreign = principal().model_copy(update={"principal_id": "principal-b"})
    edited = value.model_copy(update={"statement": "Edited statement", "store_position": 2})
    edit = MemoryEdit(statement="Edited statement")
    with pytest.raises(NotFoundError):
        await store.edit(value.id, foreign, edit, edited)
    tombstone = BeliefRejection(
        id=UUID(int=901),
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        belief_id=value.id,
        kind=RejectionKind.DELETED,
        subject=value.subject,
        statement=None,
        statement_sha256="0" * 64,
        belief_type=value.belief_type,
        scope=value.scope,
        created_at=NOW,
    )
    with pytest.raises(NotFoundError):
        await store.delete(value.id, foreign, tombstone)

    await store.edit(value.id, principal(), edit, edited)
    assert (await store.get(value.id, principal())).statement == "Edited statement"
    await store.delete(value.id, principal(), tombstone)
    with pytest.raises(NotFoundError):
        await store.get(value.id, principal())
    rejections = await store.outstanding_rejections(TENANT, PRINCIPAL_ID)
    assert [rejection.id for rejection in rejections] == [tombstone.id]
    assert await store.outstanding_rejections(TENANT, "principal-b") == []


async def test_consolidation_watermark_is_monotonic_and_principal_scoped() -> None:
    store = _store()
    assert await store.consolidation_watermark(SESSION_ID, principal()) == 0
    await store.set_consolidation_watermark(SESSION_ID, principal(), 5)
    await store.set_consolidation_watermark(SESSION_ID, principal(), 3)
    assert await store.consolidation_watermark(SESSION_ID, principal()) == 5
    foreign = principal().model_copy(update={"principal_id": "principal-b"})
    assert await store.consolidation_watermark(SESSION_ID, foreign) == 0


async def test_consolidation_runs_are_inspectable_and_principal_scoped() -> None:
    store = _store()
    older = ConsolidationRun(
        id=UUID(int=801),
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        trigger="session_idle",
        scope="general",
        session_id=SESSION_ID,
        watermark_before=0,
        watermark_after=5,
        model="deterministic-formation-v2",
        policy_version="formation@2",
        candidates_proposed=1,
        committed=1,
        reinforced=0,
        superseded=0,
        rejected=0,
        started_at=NOW - timedelta(minutes=1),
        finished_at=NOW - timedelta(minutes=1),
    )
    newer = older.model_copy(
        update={
            "id": UUID(int=802),
            "watermark_before": 5,
            "watermark_after": 9,
            "started_at": NOW,
            "finished_at": NOW,
        }
    )
    foreign = older.model_copy(
        update={"id": UUID(int=803), "principal_id": "principal-b", "started_at": NOW}
    )
    for run in (older, newer, foreign):
        await store.record_consolidation(run)

    assert await store.list_consolidations(principal(), session_id=SESSION_ID) == [newer, older]
    assert await store.list_consolidations(principal(), limit=1) == [newer]
    assert await store.list_consolidations(
        principal().model_copy(update={"principal_id": "principal-b"})
    ) == [foreign]


async def test_expire_retires_only_live_records_past_their_expiry() -> None:
    store = _store()
    past = memory(belief_id=521).model_copy(
        update={"expires_at": NOW - timedelta(seconds=1), "store_position": 1}
    )
    future = memory(belief_id=522, statement="User prefers tabs").model_copy(
        update={
            "subject": "indentation",
            "expires_at": NOW + timedelta(days=1),
            "store_position": 2,
        }
    )
    already_retired = memory(belief_id=523, statement="Old retired belief").model_copy(
        update={
            "subject": "history",
            "status": MemoryStatus.RETIRED,
            "valid_to": NOW,
            "expires_at": NOW - timedelta(days=1),
            "store_position": 3,
        }
    )
    for record in (past, future, already_retired):
        await store.upsert_belief(record)

    expired = await store.expire(principal())
    assert [record.id for record in expired] == [past.id]
    assert expired[0].status is MemoryStatus.EXPIRED
    assert expired[0].valid_to == NOW
    live = await store.query(recall_query(text=None, subjects=["answer style", "indentation"]))
    assert [record.id for record in live] == [future.id]


async def test_query_applies_ceiling_type_and_bitemporal_filters() -> None:
    store = _store()
    restricted = memory(belief_id=531).model_copy(
        update={"sensitivity": Sensitivity.RESTRICTED, "store_position": 1}
    )
    await store.upsert_belief(restricted)
    assert await store.query(recall_query(sensitivity_ceiling=Sensitivity.INTERNAL)) == []
    assert [
        record.id
        for record in await store.query(recall_query(sensitivity_ceiling=Sensitivity.RESTRICTED))
    ] == [restricted.id]
    assert await store.query(recall_query(belief_types=[BeliefType.FACT])) == []

    superseded = memory(belief_id=532, statement="User preferred terse notes").model_copy(
        update={
            "subject": "notes",
            "status": MemoryStatus.SUPERSEDED,
            "valid_from": NOW - timedelta(days=2),
            "valid_to": NOW - timedelta(days=1),
            "superseded_by": UUID(int=533),
            "store_position": 2,
        }
    )
    await store.upsert_belief(superseded)
    assert superseded.id not in {record.id for record in await store.query(recall_query())}
    # Lexical recall is any-term, so a historical query for a belief that
    # shares no term with the default text names its subject instead.
    historical = await store.query(
        recall_query(as_of=NOW - timedelta(days=1, hours=12), text=None, subjects=["notes"])
    )
    assert [record.id for record in historical] == [superseded.id]
    # Bi-temporal validity outranks include_superseded: a belief whose validity
    # ended is not live even when the caller asks to see superseded records.
    included = await store.query(recall_query(include_superseded=True))
    assert superseded.id not in {record.id for record in included}


async def test_query_excludes_zero_overlap_text_and_caps_candidates_newest_first() -> None:
    """Lexical recall is any-term, newest-first, and bounded before ranking.

    The store answers with candidates, not with a ranking, so a record sharing
    no term with the query is left out unless its subject was named, and the
    candidate set is capped at `max(max_items * 8, 64)` newest records.
    """

    store = _store()
    overlapping = [
        memory(
            belief_id=1_000 + position, statement=f"User prefers concise answers {position}"
        ).model_copy(update={"subject": f"answer style {position}", "store_position": position})
        for position in range(1, 71)
    ]
    unrelated = memory(belief_id=1_900, statement="Deployment runs on Fridays").model_copy(
        update={"subject": "release cadence", "store_position": 71}
    )
    for record in (*overlapping, unrelated):
        await store.upsert_belief(record)

    capped = await store.query(recall_query(text="concise answers", max_items=1))
    assert [record.store_position for record in capped] == list(range(70, 6, -1))
    assert unrelated.id not in {record.id for record in capped}

    named = await store.query(
        recall_query(text="concise answers", subjects=["release cadence"], max_items=1)
    )
    assert named[0].id == unrelated.id
    assert len(named) == 64


async def test_query_matches_whole_words_the_way_full_text_search_does() -> None:
    """The in-memory predicate matches lexemes, not substrings.

    PostgreSQL matches `to_tsvector('simple', ...)` against a per-term
    `plainto_tsquery`, which compares whole lexemes and never stems under the
    `simple` configuration. A substring predicate would make the in-memory tier
    the more permissive of the two stores, and it is the tier the benchmark
    measures, so it compares tokens the same way.
    """

    store = _store()
    plural = memory(belief_id=1_100, statement="Dashboards use the emerald themes").model_copy(
        update={"subject": "dashboard palette", "store_position": 1}
    )
    prefixed = memory(belief_id=1_101, statement="Apple Watch charges overnight").model_copy(
        update={"subject": "wearables", "store_position": 2}
    )
    exact = memory(belief_id=1_102, statement="The theme is emerald").model_copy(
        update={"subject": "editor colours", "store_position": 3}
    )
    hyphenated = memory(belief_id=1_103, statement="The e-mail digest is weekly").model_copy(
        update={"subject": "digest cadence", "store_position": 4}
    )
    for record in (plural, prefixed, exact, hyphenated):
        await store.upsert_belief(record)

    # "theme" is not "themes" and "app" is not "Apple": neither is a lexeme of
    # the record it reads as a substring of.
    assert [record.id for record in await store.query(recall_query(text="theme"))] == [exact.id]
    assert await store.query(recall_query(text="app")) == []
    assert [record.id for record in await store.query(recall_query(text="themes"))] == [plural.id]
    assert [record.id for record in await store.query(recall_query(text="apple"))] == [prefixed.id]
    # A hyphenated word is its own lexeme and each of its parts, as the
    # PostgreSQL parser splits it.
    assert [record.id for record in await store.query(recall_query(text="mail"))] == [hyphenated.id]
    assert [record.id for record in await store.query(recall_query(text="e-mail"))] == [
        hyphenated.id
    ]
    # Query text that reduces to no lexeme matches nothing, as an empty
    # `plainto_tsquery` does, rather than matching everything.
    assert await store.query(recall_query(text="...")) == []


async def test_list_idle_returns_the_least_recently_evidenced_live_beliefs() -> None:
    """The decay sweep's window is ordered by idleness, not by write position.

    A store larger than one sweep's ceiling must still offer the belief that
    has gone without evidence the longest, or a bounded sweep never reaches it.
    """

    store = _store()

    def idle(belief_id: int, days: int, position: int) -> MemoryRecord:
        return memory(belief_id=belief_id, statement=f"Belief {belief_id}").model_copy(
            update={
                "subject": f"subject-{belief_id}",
                "last_evidence_at": NOW - timedelta(days=days),
                "store_position": position,
            }
        )

    # Written newest-first, so write position cannot stand in for idleness.
    oldest = idle(521, 400, 4)
    middle = idle(522, 200, 3)
    recent_enough = idle(523, 100, 2)
    fresh = idle(524, 1, 1)
    retired = idle(525, 500, 5).model_copy(update={"status": MemoryStatus.RETIRED, "valid_to": NOW})
    other = idle(526, 450, 6).model_copy(update={"principal_id": "someone-else"})
    for record in (fresh, recent_enough, middle, oldest, retired, other):
        await store.upsert_belief(record)

    cutoff = NOW - timedelta(days=50)
    window = await store.list_idle(principal(), evidence_before=cutoff, limit=10)
    bounded = await store.list_idle(principal(), evidence_before=cutoff, limit=2)
    cut = await store.list_idle(principal(), evidence_before=NOW - timedelta(days=300), limit=10)
    foreign = await store.list_idle(
        principal().model_copy(update={"tenant_id": "tenant-b"}),
        evidence_before=NOW,
        limit=10,
    )
    eligible = idle(527, 300, 7).model_copy(
        update={"status": MemoryStatus.PROVISIONAL, "confidence": 0.3}
    )
    eligible_high_confidence = idle(528, 250, 8).model_copy(
        update={"status": MemoryStatus.PROVISIONAL, "confidence": 0.8}
    )
    await store.upsert_belief(eligible)
    await store.upsert_belief(eligible_high_confidence)
    decay_window = await store.list_idle(
        principal(),
        evidence_before=cutoff,
        decay_confidence_ceiling=0.55,
        limit=2,
    )

    assert [record.id for record in window] == [oldest.id, middle.id, recent_enough.id]
    assert [record.id for record in bounded] == [oldest.id, middle.id]
    assert [record.id for record in cut] == [oldest.id]
    assert foreign == []
    assert [record.id for record in decay_window] == [
        eligible.id,
        eligible_high_confidence.id,
    ]


async def test_head_position_is_zero_when_empty_and_tracks_the_newest_position() -> None:
    """The head is the principal's newest write, live or not, and zero when empty.

    The recall delta bounds itself by the store head rather than by the highest
    position a recall happened to return, so a belief no query matched at
    snapshot time cannot be reported as new on the next turn.
    """

    store = _store()
    foreign = principal().model_copy(update={"principal_id": "principal-b"})
    assert await store.head_position(principal()) == 0

    first = memory()
    await store.upsert_belief(first)
    assert await store.head_position(principal()) == first.store_position

    newest = memory(belief_id=531, statement="User prefers tabs").model_copy(
        update={"subject": "indentation", "store_position": 7}
    )
    await store.upsert_belief(newest)
    assert await store.head_position(principal()) == 7

    # A retired row is still a write: the head is the store's position, not the
    # newest live belief, or a correction would move the watermark backwards.
    retired = memory(belief_id=532, statement="Old belief about answers").model_copy(
        update={
            "subject": "history",
            "status": MemoryStatus.RETIRED,
            "valid_to": NOW,
            "store_position": 9,
        }
    )
    await store.upsert_belief(retired)
    assert await store.head_position(principal()) == 9

    # Another principal's rows are invisible, however far the store has moved.
    assert await store.head_position(foreign) == 0


async def test_query_min_store_position_returns_only_newer_rows() -> None:
    """The delta is a query bound, not a filter the caller applies afterwards."""

    store = _store()
    older = memory()
    newer = memory(belief_id=533, statement="User prefers concise answers in review").model_copy(
        update={"store_position": 4}
    )
    for record in (older, newer):
        await store.upsert_belief(record)

    assert [record.id for record in await store.query(recall_query(text="answers"))] == [
        newer.id,
        older.id,
    ]
    delta = await store.query(recall_query(text="answers", min_store_position=1))
    assert [record.id for record in delta] == [newer.id]
    assert await store.query(recall_query(text="answers", min_store_position=4)) == []


async def test_conflict_links_and_the_review_flag_survive_a_round_trip() -> None:
    """A conflicted belief stays live, so its link has to be readable back.

    Conflict partners are stored, not derived: retrieval renders the marker
    from the row it read, and a store that dropped the link would silently turn
    a surfaced conflict back into a hidden one.
    """

    store = _store()
    partner = UUID(int=555)
    await store.upsert_belief(memory())
    linked = memory().model_copy(
        update={
            "conflicts_with": [partner],
            "flagged_for_review": True,
            "store_position": 2,
        }
    )
    await store.reinforce(linked)

    stored = await store.get(linked.id, principal())
    assert stored.conflicts_with == [partner]
    assert stored.flagged_for_review is True
    assert stored.status is MemoryStatus.ACTIVE
    assert await store.query(recall_query()) == [linked]


async def test_browse_ceiling_filter_excludes_records_above_it() -> None:
    store = _store()
    visible = memory(belief_id=541).model_copy(
        update={"sensitivity": Sensitivity.INTERNAL, "store_position": 1}
    )
    hidden = memory(belief_id=542, statement="A restricted belief").model_copy(
        update={"subject": "secret", "sensitivity": Sensitivity.RESTRICTED, "store_position": 2}
    )
    await store.upsert_belief(visible)
    await store.upsert_belief(hidden)

    page = await store.browse(browse_query(ceiling=Sensitivity.INTERNAL))
    assert [record.id for record in page] == [visible.id]

    page_all = await store.browse(browse_query(ceiling=Sensitivity.RESTRICTED))
    assert {record.id for record in page_all} == {visible.id, hidden.id}


async def test_browse_enforces_tenant_and_principal_isolation() -> None:
    store = _store()
    mine = memory(belief_id=543)
    foreign_principal = memory(belief_id=544, statement="Another principal's belief").model_copy(
        update={"principal_id": "principal-b", "subject": "other", "store_position": 2}
    )
    foreign_tenant = memory(belief_id=545, statement="Another tenant's belief").model_copy(
        update={"tenant_id": "tenant-b", "subject": "other-tenant", "store_position": 3}
    )
    for record in (mine, foreign_principal, foreign_tenant):
        await store.upsert_belief(record)

    page = await store.browse(browse_query())
    assert [record.id for record in page] == [mine.id]


async def test_browse_status_default_is_the_live_set_and_can_be_overridden() -> None:
    store = _store()
    active = memory(belief_id=551).model_copy(update={"store_position": 1})
    provisional = memory(belief_id=552, statement="A provisional belief").model_copy(
        update={"subject": "draft", "status": MemoryStatus.PROVISIONAL, "store_position": 2}
    )
    superseded = memory(belief_id=553, statement="An old belief").model_copy(
        update={
            "subject": "history",
            "status": MemoryStatus.SUPERSEDED,
            "valid_to": NOW,
            "superseded_by": UUID(int=999),
            "store_position": 3,
        }
    )
    for record in (active, provisional, superseded):
        await store.upsert_belief(record)

    default_page = await store.browse(browse_query())
    assert {record.id for record in default_page} == {active.id, provisional.id}

    with_history = await store.browse(browse_query(statuses=(MemoryStatus.SUPERSEDED,)))
    assert [record.id for record in with_history] == [superseded.id]


async def test_browse_belief_type_filter_composes_with_the_live_default() -> None:
    store = _store()
    preference = memory(belief_id=561)
    fact = memory(belief_id=562, statement="Deployment runs on Fridays").model_copy(
        update={"subject": "release cadence", "belief_type": BeliefType.FACT, "store_position": 2}
    )
    for record in (preference, fact):
        await store.upsert_belief(record)

    page = await store.browse(browse_query(belief_types=(BeliefType.FACT,)))
    assert [record.id for record in page] == [fact.id]

    everything = await store.browse(browse_query())
    assert {record.id for record in everything} == {preference.id, fact.id}


async def test_browse_subject_filter_is_exact_and_lowercased() -> None:
    """Exact, case-insensitive, and lowercased — not casefolded.

    The PostgreSQL adapter compares SQL `lower()`, and `casefold()` disagrees
    with it on non-ASCII text: "Straße" casefolds to "strasse" and lowers to
    "straße". Both adapters lowercase so both select the same set; the
    cross-adapter half of that claim is asserted against a live store in
    `tests/integration/test_memory_postgres_m9.py`.
    """

    store = _store()
    target = memory(belief_id=571)
    other = memory(belief_id=572, statement="User prefers tabs").model_copy(
        update={"subject": "indentation", "store_position": 2}
    )
    non_ascii = memory(belief_id=573, statement="Deliveries go to the flat").model_copy(
        update={"subject": "straße", "store_position": 3}
    )
    for record in (target, other, non_ascii):
        await store.upsert_belief(record)

    page = await store.browse(browse_query(subject="ANSWER STYLE"))
    assert [record.id for record in page] == [target.id]
    assert await store.browse(browse_query(subject="answer styles")) == []

    lowered = await store.browse(browse_query(subject="Straße"))
    assert [record.id for record in lowered] == [non_ascii.id]
    # Casefolding would have matched here; lowercasing must not.
    assert await store.browse(browse_query(subject="STRASSE")) == []


async def test_browse_session_id_filter_matches_the_source_session() -> None:
    store = _store()
    in_session = memory(belief_id=581)
    other_session = memory(belief_id=582, statement="Another session's belief").model_copy(
        update={"subject": "other", "source_session_id": UUID(int=999), "store_position": 2}
    )
    for record in (in_session, other_session):
        await store.upsert_belief(record)

    page = await store.browse(browse_query(session_id=SESSION_ID))
    assert [record.id for record in page] == [in_session.id]


async def test_browse_text_filter_matches_the_shared_lexical_helpers_directly() -> None:
    """Browse's text filter is exactly what the shared lexical helpers say.

    Both belief stores answer a text query by calling `lexical_query_terms`
    and `lexical_text_matches`; this asserts the in-memory store's browse
    results equal those two functions applied directly to the same corpus.
    Cross-adapter parity is asserted separately, against a live PostgreSQL
    store, in `tests/integration/test_memory_postgres_m9.py`.
    """

    store = _store()
    records = [
        memory(belief_id=591, statement="Dashboards use the emerald themes").model_copy(
            update={"subject": "dashboard palette", "store_position": 1}
        ),
        memory(belief_id=592, statement="The theme is emerald").model_copy(
            update={"subject": "editor colours", "store_position": 2}
        ),
        memory(belief_id=593, statement="Apple Watch charges overnight").model_copy(
            update={"subject": "wearables", "store_position": 3}
        ),
    ]
    for record in records:
        await store.upsert_belief(record)

    for text in ("theme", "themes", "apple", "..."):
        term_lexemes = lexical_term_lexemes(lexical_query_terms(text))
        expected = {
            record.id
            for record in records
            if lexical_text_matches(term_lexemes, f"{record.subject} {record.statement}")
        }
        page = await store.browse(browse_query(text=text))
        assert {record.id for record in page} == expected, f"mismatch for text={text!r}"


async def test_browse_orders_newest_first_with_id_tiebreak_and_overfetches_by_one() -> None:
    store = _store()
    tied_low_id = memory(belief_id=620, statement="Tied belief, lower id").model_copy(
        update={"subject": "tie-a", "store_position": 5}
    )
    tied_high_id = memory(belief_id=621, statement="Tied belief, higher id").model_copy(
        update={"subject": "tie-b", "store_position": 5}
    )
    newest = memory(belief_id=622, statement="Newest belief").model_copy(
        update={"subject": "newest", "store_position": 6}
    )
    for record in (tied_low_id, tied_high_id, newest):
        await store.upsert_belief(record)

    page = await store.browse(browse_query(limit=2))
    assert len(page) == 3  # limit + 1 overfetch, so the caller can detect has-more
    assert [record.store_position for record in page] == [6, 5, 5]
    assert [record.id for record in page[1:]] == [tied_low_id.id, tied_high_id.id]


async def test_browse_keyset_tiebreak_includes_the_higher_identifier_sibling() -> None:
    """The `store_position = p AND id > i` branch is an inclusion path, not only a guard.

    Two beliefs tied at one position are the case the tiebreak exists for: a
    cursor at the lower identifier must still return the higher one, or the
    walk silently drops it. Ordering alone never observes this, because a
    page taken without a cursor cannot exercise the predicate.
    """

    store = _store()
    tied_low_id = memory(belief_id=630, statement="Tied belief, lower id").model_copy(
        update={"subject": "tie-a", "store_position": 5}
    )
    tied_high_id = memory(belief_id=631, statement="Tied belief, higher id").model_copy(
        update={"subject": "tie-b", "store_position": 5}
    )
    older = memory(belief_id=632, statement="Older belief").model_copy(
        update={"subject": "older", "store_position": 4}
    )
    for record in (tied_low_id, tied_high_id, older):
        await store.upsert_belief(record)

    assert tied_low_id.id < tied_high_id.id
    page = await store.browse(browse_query(cursor=(5, tied_low_id.id)))
    assert [record.id for record in page] == [tied_high_id.id, older.id]

    # And the guard half still holds: a cursor at the higher identifier
    # excludes both tied rows.
    beyond = await store.browse(browse_query(cursor=(5, tied_high_id.id)))
    assert [record.id for record in beyond] == [older.id]


async def test_browse_keyset_predicate_walks_without_skipping_or_repeating_across_writes() -> None:
    """A page is point-in-time: a write between fetches lands, but is never lost or doubled."""

    store = _store()
    records = {
        position: memory(belief_id=610 + position, statement=f"Belief at {position}").model_copy(
            update={"subject": f"walk-{position}", "store_position": position}
        )
        for position in (10, 8, 6, 4, 2)
    }
    for record in records.values():
        await store.upsert_belief(record)

    first_batch = await store.browse(browse_query(limit=2))
    first_batch_again = await store.browse(browse_query(limit=2))
    assert first_batch_again == first_batch
    assert [record.store_position for record in first_batch] == [10, 8, 6]
    page_one = first_batch[:2]
    cursor = (page_one[-1].store_position, page_one[-1].id)

    # A belief is written between page fetches, landing inside the unwalked range.
    inserted = memory(belief_id=699, statement="Inserted mid-walk").model_copy(
        update={"subject": "inserted", "store_position": 5}
    )
    await store.upsert_belief(inserted)
    records[5] = inserted

    second_batch = await store.browse(browse_query(limit=2, cursor=cursor))
    assert [record.store_position for record in second_batch] == [6, 5, 4]
    page_two = second_batch[:2]
    cursor2 = (page_two[-1].store_position, page_two[-1].id)

    third_batch = await store.browse(browse_query(limit=2, cursor=cursor2))
    assert [record.store_position for record in third_batch] == [4, 2]
    page_three = third_batch

    walked_ids = [record.id for record in (*page_one, *page_two, *page_three)]
    assert set(walked_ids) == {record.id for record in records.values()}
    assert len(walked_ids) == len(set(walked_ids))  # no repeats across the walk
