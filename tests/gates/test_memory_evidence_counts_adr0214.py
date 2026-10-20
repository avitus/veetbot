"""ADR-0214 parts B and C: evidence quotes and sidebar counts.

`GET /v1/memories/{id}/evidence` shows at most five cited sources, newest
first. Only the owner's own authenticated words carry an excerpt, and only at
the restricted ceiling to a caller who may read the conversation; email, SMS,
call and tool sources are identifiers; a source that is gone is withheld.

`GET /v1/memories/counts` returns the browser's numbers under exactly the
list's predicates.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from agent_core.adapters.persistence.people import InMemoryPeopleStore
from agent_core.bootstrap import build
from agent_core.domain.agents import Principal
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import BeliefType, MemoryStatus, Sensitivity
from agent_core.domain.people_sources import source_id
from agent_core.domain.policies import TrustLevel
from agent_core.policy.scopes import PLATFORM_SCOPES
from tests.gates.test_memory_read_api_m17 import (
    PRINCIPAL_ID,
    SESSION_A,
    _belief,
    _client,
    _enabled_settings,
    _principal,
    _seed,
)
from tests.gates.test_memory_write_api_adr0117 import _seed_with_session

READER = _principal(scopes=set(PLATFORM_SCOPES) | {"memory.read"})
OWNER_WORDS = "I switched to oat milk in the spring, dairy upsets me now."


async def _event(
    composition: Any,
    event_type: str,
    payload: dict[str, Any],
    *,
    actor_type: str = "principal",
    actor_id: str = PRINCIPAL_ID,
) -> int:
    async with composition.uow_factory() as uow:
        event = await uow.events.append(
            NewEvent(
                session_id=SESSION_A,
                run_id=None,
                event_type=event_type,
                actor_type=actor_type,
                actor_id=actor_id,
                payload=payload,
            )
        )
    return int(event.sequence)


async def _evidence(
    client: Any, belief_id: UUID, ceiling: str = "restricted"
) -> tuple[int, dict[str, Any]]:
    response = await client.get(f"/v1/memories/{belief_id}/evidence", params={"ceiling": ceiling})
    if response.status_code == 200:
        assert response.headers["cache-control"] == "private, no-store"
    return response.status_code, response.json()


async def test_the_owners_own_words_are_quoted_and_everything_else_is_a_reference() -> None:
    async with build(
        settings=_enabled_settings(), storage="memory", sequential_ids=True, principal=READER
    ) as composition:
        await _seed_with_session(composition, [])
        owner = await _event(composition, "user.message.created", {"content": OWNER_WORDS})
        email_body = "Quarterly invoice attached, wire to account 0042."
        email = await _event(
            composition,
            "tool.call.completed",
            {"name": "mcp.gmail.get_thread_page", "result_item": {"text": email_body}},
            actor_type="runtime",
            actor_id=None,  # type: ignore[arg-type]
        )
        relayed = await _event(
            composition,
            "user.message.created",
            {"content": "Running late, start without me", "trust": TrustLevel.EXTERNAL_UNTRUSTED},
        )
        belief = _belief(belief_id=1, position=1).model_copy(
            update={"source_event_ids": [owner, email, relayed]}
        )
        await _seed(composition, [belief])
        async with _client(composition) as client:
            status, body = await _evidence(client, belief.id)
        assert status == 200, body
        items = body["items"]
        assert [item["event_sequence"] for item in items] == [relayed, email, owner]
        by_sequence = {item["event_sequence"]: item for item in items}
        assert by_sequence[owner]["source_kind"] == "owner"
        assert by_sequence[owner]["excerpt"] == OWNER_WORDS
        assert by_sequence[owner]["withheld"] is None
        assert by_sequence[email]["source_kind"] == "email"
        assert by_sequence[relayed]["source_kind"] == "sms"
        for sequence in (email, relayed):
            assert by_sequence[sequence]["excerpt"] is None
            assert by_sequence[sequence]["withheld"] == "not_owner_text"
            assert by_sequence[sequence]["session_id"] == str(SESSION_A)
            assert by_sequence[sequence]["evidence_at"] is not None
        assert email_body not in str(body)
        assert "Running late" not in str(body)


async def test_an_excerpt_needs_the_restricted_ceiling_and_session_read() -> None:
    async with build(
        settings=_enabled_settings(), storage="memory", sequential_ids=True, principal=READER
    ) as composition:
        await _seed_with_session(composition, [])
        owner = await _event(composition, "user.message.created", {"content": OWNER_WORDS})
        belief = _belief(belief_id=1, position=1, sensitivity=Sensitivity.INTERNAL).model_copy(
            update={"source_event_ids": [owner]}
        )
        await _seed(composition, [belief])
        async with _client(composition) as client:
            status, body = await _evidence(client, belief.id, ceiling="sensitive")
        assert status == 200
        assert body["items"][0]["withheld"] == "ceiling"
        assert body["items"][0]["excerpt"] is None
        no_sessions = _principal(scopes=(set(PLATFORM_SCOPES) | {"memory.read"}) - {"session.read"})
        async with _client(composition, principal=no_sessions) as client:
            status, body = await _evidence(client, belief.id)
        assert status == 200
        assert body["items"][0]["withheld"] == "scope"
        assert OWNER_WORDS not in str(body)


async def test_hazardous_owner_text_and_someone_elses_words_are_withheld() -> None:
    async with build(
        settings=_enabled_settings(), storage="memory", sequential_ids=True, principal=READER
    ) as composition:
        await _seed_with_session(composition, [])
        secret = await _event(
            composition, "user.message.created", {"content": "password: hunter2-correct-horse"}
        )
        foreign = await _event(
            composition, "user.message.created", {"content": OWNER_WORDS}, actor_id="someone-else"
        )
        scheduled = await _event(
            composition,
            "user.message.created",
            {"content": "Check the snow report."},
            actor_type="scheduler",
        )
        belief = _belief(belief_id=1, position=1).model_copy(
            update={"source_event_ids": [secret, foreign, scheduled]}
        )
        await _seed(composition, [belief])
        async with _client(composition) as client:
            status, body = await _evidence(client, belief.id)
        assert status == 200
        by_sequence = {item["event_sequence"]: item for item in body["items"]}
        assert by_sequence[secret]["withheld"] == "hazard"
        assert by_sequence[foreign] == {**by_sequence[foreign], "source_kind": "unknown"}
        assert by_sequence[foreign]["withheld"] == "not_owner_text"
        assert by_sequence[scheduled]["source_kind"] == "scheduler"
        assert all(item["excerpt"] is None for item in body["items"])


async def test_long_text_is_cut_at_a_word_and_only_five_sources_are_shown() -> None:
    async with build(
        settings=_enabled_settings(), storage="memory", sequential_ids=True, principal=READER
    ) as composition:
        await _seed_with_session(composition, [])
        long_text = " ".join(f"word{index}" for index in range(120))
        sequences = [
            await _event(composition, "user.message.created", {"content": long_text})
            for _ in range(7)
        ]
        belief = _belief(belief_id=1, position=1).model_copy(update={"source_event_ids": sequences})
        await _seed(composition, [belief])
        async with _client(composition) as client:
            status, body = await _evidence(client, belief.id)
        assert status == 200
        assert [item["event_sequence"] for item in body["items"]] == sorted(sequences)[::-1][:5]
        excerpt = body["items"][0]["excerpt"]
        assert len(excerpt) <= 280
        assert excerpt.endswith("…")
        assert long_text.startswith(excerpt.removesuffix("…"))
        assert excerpt.removesuffix("…").split(" ")[-1] in long_text.split(" ")


async def test_a_source_that_is_gone_is_withheld_with_only_its_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with build(
        settings=_enabled_settings(), storage="memory", sequential_ids=True, principal=READER
    ) as composition:
        await _seed_with_session(composition, [])
        kept = await _event(composition, "user.message.created", {"content": OWNER_WORDS})
        erased = await _event(composition, "user.message.created", {"content": "Erased words."})
        suppressed = source_id(READER, SESSION_A, erased)
        original = InMemoryPeopleStore.source_suppressed

        async def suppress(self: InMemoryPeopleStore, principal: Principal, key: UUID) -> bool:
            return key == suppressed or await original(self, principal, key)

        monkeypatch.setattr(InMemoryPeopleStore, "source_suppressed", suppress)
        belief = _belief(belief_id=1, position=1).model_copy(
            update={"source_event_ids": [kept, erased, 99]}
        )
        orphan = _belief(belief_id=2, position=2, session_id=UUID(int=0xDEAD))
        await _seed(composition, [belief, orphan])
        async with _client(composition) as client:
            status, body = await _evidence(client, belief.id)
            orphan_status, orphan_body = await _evidence(client, orphan.id)
        assert status == 200
        by_sequence = {item["event_sequence"]: item for item in body["items"]}
        for sequence in (erased, 99):
            assert by_sequence[sequence] == {
                "source_kind": "unknown",
                "session_id": str(SESSION_A),
                "event_sequence": sequence,
                "evidence_at": None,
                "excerpt": None,
                "withheld": "erased",
            }
        assert by_sequence[kept]["excerpt"] == OWNER_WORDS
        assert "Erased words" not in str(body)
        assert orphan_status == 200
        assert [item["withheld"] for item in orphan_body["items"]] == ["erased"]


async def test_evidence_follows_the_read_rules() -> None:
    async with build(
        settings=_enabled_settings(), storage="memory", sequential_ids=True, principal=READER
    ) as composition:
        restricted = _belief(belief_id=1, position=1, sensitivity=Sensitivity.RESTRICTED)
        foreign = _belief(belief_id=2, position=2, principal_id="someone-else")
        await _seed_with_session(composition, [restricted, foreign])
        async with _client(composition) as client:
            assert (await _evidence(client, restricted.id, ceiling="sensitive"))[0] == 404
            assert (await _evidence(client, foreign.id))[0] == 404
            assert (await _evidence(client, UUID(int=0xBEEF)))[0] == 404
            missing_ceiling = await client.get(f"/v1/memories/{restricted.id}/evidence")
            assert missing_ceiling.status_code == 400
        no_read = _principal(scopes=set(PLATFORM_SCOPES) - {"memory.read"})
        async with _client(composition, principal=no_read) as client:
            assert (await _evidence(client, restricted.id))[0] == 403


async def test_counts_match_the_list_they_label() -> None:
    async with build(
        settings=_enabled_settings(), storage="memory", sequential_ids=True, principal=READER
    ) as composition:
        corpus = [
            _belief(belief_id=1, position=1, belief_type=BeliefType.FACT),
            _belief(belief_id=2, position=2, belief_type=BeliefType.PREFERENCE),
            _belief(belief_id=3, position=3, status=MemoryStatus.PROVISIONAL),
            _belief(belief_id=4, position=4, status=MemoryStatus.SUPERSEDED),
            _belief(belief_id=5, position=5, status=MemoryStatus.RETIRED),
            _belief(belief_id=6, position=6, sensitivity=Sensitivity.RESTRICTED),
            _belief(belief_id=7, position=7, principal_id="someone-else"),
            _belief(belief_id=8, position=8, tenant_id="other-tenant"),
            _belief(belief_id=9, position=9).model_copy(update={"flagged_for_review": True}),
            _belief(belief_id=10, position=10, status=MemoryStatus.RETIRED).model_copy(
                update={"flagged_for_review": True}
            ),
        ]
        await _seed(composition, corpus)
        async with _client(composition) as client:

            async def listed(ceiling: str, **params: Any) -> int:
                response = await client.get(
                    "/v1/memories", params={"ceiling": ceiling, "limit": 200, **params}
                )
                assert response.status_code == 200, response.text
                return len(response.json()["items"])

            for ceiling in ("sensitive", "restricted"):
                response = await client.get("/v1/memories/counts", params={"ceiling": ceiling})
                assert response.status_code == 200, response.text
                assert response.headers["cache-control"] == "private, no-store"
                counts = response.json()
                assert counts["live"] == await listed(ceiling)
                assert counts["flagged"] == await listed(ceiling, flagged="true")
                for status in MemoryStatus:
                    assert counts["by_status"][status.value] == await listed(
                        ceiling, status=status.value
                    ), status
                for belief_type in BeliefType:
                    assert counts["by_type"][belief_type.value] == await listed(
                        ceiling, belief_type=belief_type.value
                    ), belief_type
                assert counts["synthesis"] is None
                assert counts["counted_at"]
            restricted = (
                await client.get("/v1/memories/counts", params={"ceiling": "restricted"})
            ).json()
        assert restricted["live"] == 5
        assert restricted["flagged"] == 1


async def test_counts_need_memory_read_a_ceiling_and_an_available_derived_surface() -> None:
    async with build(
        settings=_enabled_settings(), storage="memory", sequential_ids=True, principal=READER
    ) as composition:
        async with _client(composition) as client:
            assert (await client.get("/v1/memories/counts")).status_code == 400
            derived = await client.get(
                "/v1/memories/counts", params={"ceiling": "restricted", "include_derived": True}
            )
            assert derived.status_code == 400
        no_read = _principal(scopes=set(PLATFORM_SCOPES) - {"memory.read"})
        async with _client(composition, principal=no_read) as client:
            denied = await client.get("/v1/memories/counts", params={"ceiling": "restricted"})
            assert denied.status_code == 403
