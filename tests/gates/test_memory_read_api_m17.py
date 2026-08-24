"""Milestone 17 memory read API and browse gates.

The ten hard gates `docs/plan/memory-read-api-and-browser.md` declares, one
test function per gate, named so `evals/gates/memory.yaml` resolves each
`check:` to exactly one node. The sibling boundary coverage for the same
surface — happy path, validation, authorization, failure — lives in
`tests/gates/test_memory_api_boundary_m17.py`; these are the acceptance
instruments on top of it and repeat nothing from it that a regression could
not also break here.
"""

from __future__ import annotations

import ast
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from pydantic import SecretStr, ValidationError

from agent_core.api import create_app
from agent_core.api.errors import API_ERROR_STATUS, ERROR_CODE_VOCABULARY, ERROR_STATUS_MAP
from agent_core.bootstrap import Composition, build
from agent_core.config import AuthMode, Settings, load_settings
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ToolValidationError
from agent_core.domain.memory import (
    LIVE_MEMORY_STATUSES,
    SENSITIVITY_ORDER,
    BeliefType,
    MemoryAuthority,
    MemoryRecord,
    MemoryStatus,
    Polarity,
    Portability,
    Sensitivity,
    lexical_query_terms,
    lexical_term_lexemes,
    lexical_text_matches,
)
from agent_core.domain.views import MemoryView
from agent_core.policy.scopes import PLATFORM_SCOPES, validate_required_scopes
from tests.gates.memory_api_support import memory_routes
from tests.integration.m2_support import memory_settings

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 23, 16, tzinfo=UTC)
TENANT = "local"
PRINCIPAL_ID = "local-user"
SESSION_A = UUID(int=901)
SESSION_B = UUID(int=902)

# The spec's exposure list, verbatim: twenty-six names that serialize.
EXPOSED_FIELDS = (
    "id",
    "subject",
    "statement",
    "belief_type",
    "status",
    "polarity",
    "scope",
    "portability",
    "authority",
    "sensitivity",
    "confidence",
    "corroboration_count",
    "flagged_for_review",
    "conflicts_with",
    "superseded_by",
    "source_session_id",
    "source_event_ids",
    "formation_run_id",
    "consolidation_policy_version",
    "origin_scopes",
    "valid_from",
    "valid_to",
    "expires_at",
    "last_reinforced_at",
    "created_at",
    "updated_at",
)

# The spec's withheld list, verbatim: four names that must never appear.
WITHHELD_FIELDS = (
    "tenant_id",
    "principal_id",
    "utility",
    "store_position",
)

SENSITIVITIES = (
    Sensitivity.PUBLIC,
    Sensitivity.INTERNAL,
    Sensitivity.SENSITIVE,
    Sensitivity.RESTRICTED,
)


def _principal(
    *,
    tenant_id: str = TENANT,
    principal_id: str = PRINCIPAL_ID,
    scopes: set[str] | None = None,
) -> Principal:
    """Build the principal used by the Milestone 17 gate fixtures."""

    return Principal(
        tenant_id=tenant_id,
        principal_id=principal_id,
        roles={"user"},
        scopes=set(PLATFORM_SCOPES) if scopes is None else scopes,
    )


def _enabled_settings() -> Settings:
    """Return memory-backed settings with the read API enabled."""

    return replace(memory_settings(), memory_api_enabled=True)


def _belief(
    *,
    belief_id: int,
    position: int,
    tenant_id: str = TENANT,
    principal_id: str = PRINCIPAL_ID,
    subject: str | None = None,
    statement: str | None = None,
    sensitivity: Sensitivity = Sensitivity.INTERNAL,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    belief_type: BeliefType = BeliefType.PREFERENCE,
    session_id: UUID = SESSION_A,
) -> MemoryRecord:
    """Build a deterministic belief record for the hard-gate corpus."""

    closed = status in {MemoryStatus.SUPERSEDED, MemoryStatus.EXPIRED, MemoryStatus.RETIRED}
    return MemoryRecord(
        id=UUID(int=belief_id),
        tenant_id=tenant_id,
        principal_id=principal_id,
        scope="project-a",
        subject=subject if subject is not None else f"subject-{belief_id}",
        statement=statement if statement is not None else f"statement {belief_id}",
        source_session_id=session_id,
        source_event_ids=[1],
        confidence=0.9,
        sensitivity=sensitivity,
        valid_from=NOW,
        valid_to=NOW if closed else None,
        status=status,
        belief_type=belief_type,
        polarity=Polarity.ASSERT,
        portability=Portability.PORTABLE,
        origin_scopes=["project-a"],
        corroboration_count=1,
        last_reinforced_at=NOW,
        formation_run_id=UUID(int=belief_id + 10_000),
        consolidation_policy_version="formation@1",
        authority=MemoryAuthority.USER,
        store_position=position,
        created_at=NOW,
        updated_at=NOW,
    )


@asynccontextmanager
async def _client(
    composition: Composition,
    *,
    principal: Principal | None = None,
    app_settings: Settings | None = None,
) -> Any:
    """Yield an in-process client with optional identity and settings overrides."""

    app = create_app(
        composition.services,
        app_settings or composition.settings,
        principal or composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


async def _seed(composition: Composition, records: list[MemoryRecord]) -> None:
    """Persist the supplied records through the composition's unit of work."""

    async with composition.uow_factory() as uow:
        for record in records:
            await uow.memories.upsert_belief(record)


def _ids(body: dict[str, Any]) -> list[str]:
    """Extract item identifiers from a serialized page."""

    return [item["id"] for item in body["items"]]


def _redacted(response: httpx.Response) -> bytes:
    """The response body with only the per-request identifier neutralized.

    Every error envelope carries a fresh `request_id`, so two otherwise
    identical bodies differ in exactly those bytes. Substituting it is what
    lets the ceiling gate assert byte identity rather than field equality.
    """

    request_id = response.json()["error"]["request_id"]
    return response.content.replace(str(request_id).encode("utf-8"), b"<request-id>")


# ---------------------------------------------------------------------------
# Gate 1 — gate.memory.read_api_ceiling_required
# ---------------------------------------------------------------------------


async def test_a_request_without_a_ceiling_is_refused() -> None:
    """List and detail both refuse a missing or unknown `ceiling`.

    The error names the parameter, no belief crosses the boundary, and the
    server applies no default: a request omitting `ceiling` must not be
    served as though it had asked for the permissive end.
    """

    async with build(
        settings=_enabled_settings(),
        storage="memory",
        sequential_ids=True,
        principal=_principal(),
    ) as composition:
        public = _belief(belief_id=1, position=1, sensitivity=Sensitivity.PUBLIC)
        restricted = _belief(belief_id=2, position=2, sensitivity=Sensitivity.RESTRICTED)
        await _seed(composition, [public, restricted])

        async with _client(composition) as client:
            omitted_list = await client.get("/v1/memories")
            omitted_detail = await client.get(f"/v1/memories/{public.id}")
            unknown_list = await client.get("/v1/memories", params={"ceiling": "top-secret"})
            unknown_detail = await client.get(
                f"/v1/memories/{public.id}", params={"ceiling": "top-secret"}
            )

            for response in (omitted_list, omitted_detail, unknown_list, unknown_detail):
                assert response.status_code == 400, response.text
                error = response.json()["error"]
                assert error["code"] == "malformed_request"
                # The message names the offending parameter; `details` stays
                # the closed-vocabulary `{}` (http-api-and-streaming.md rule 3).
                assert "ceiling" in error["message"], error["message"]
                assert error["details"] == {}
                # No belief is returned and no default is applied: the body is
                # the error envelope alone, and carries no page and no belief.
                assert set(response.json()) == {"error"}
                assert public.statement not in response.text
                assert restricted.statement not in response.text
                assert str(public.id) not in response.text

            # A supplied ceiling is honored strictly, so the refusal above is
            # a refusal rather than a permissive default in disguise.
            served = await client.get("/v1/memories", params={"ceiling": "public"})
            assert served.status_code == 200, served.text
            assert _ids(served.json()) == [str(public.id)]


# ---------------------------------------------------------------------------
# Gate 2 — gate.memory.read_api_ceiling_filter
# ---------------------------------------------------------------------------


@given(
    sensitivities=st.lists(st.sampled_from(SENSITIVITIES), min_size=1, max_size=6),
    ceiling=st.sampled_from(SENSITIVITIES),
)
@hypothesis_settings(max_examples=30, deadline=None, derandomize=True)
def test_nothing_above_the_ceiling_is_returned_or_distinguishable(
    sensitivities: list[Sensitivity],
    ceiling: Sensitivity,
) -> None:
    """No list item exceeds the ceiling, and an above-ceiling detail read is absent.

    Byte identity, not merely status parity: an above-ceiling detail read and
    a read of an identifier that does not exist differ in nothing but the
    per-request identifier, so no response field is an oracle over which
    beliefs merely sit too high to show.
    """

    async def exercise_ceiling() -> None:
        """Exercise one generated sensitivity corpus and request ceiling."""

        async with build(
            settings=_enabled_settings(),
            storage="memory",
            sequential_ids=True,
            principal=_principal(),
        ) as composition:
            records = [
                _belief(belief_id=index + 1, position=index + 1, sensitivity=sensitivity)
                for index, sensitivity in enumerate(sensitivities)
            ]
            await _seed(composition, records)
            visible = {
                record.id
                for record in records
                if SENSITIVITY_ORDER[record.sensitivity] <= SENSITIVITY_ORDER[ceiling]
            }
            above = [
                record
                for record in records
                if SENSITIVITY_ORDER[record.sensitivity] > SENSITIVITY_ORDER[ceiling]
            ]

            async with _client(composition) as client:
                listed = await client.get(
                    "/v1/memories",
                    params={"ceiling": ceiling.value, "limit": 200},
                )
                assert listed.status_code == 200, listed.text
                items = listed.json()["items"]
                for item in items:
                    assert (
                        SENSITIVITY_ORDER[Sensitivity(item["sensitivity"])]
                        <= SENSITIVITY_ORDER[ceiling]
                    ), item
                assert {UUID(item["id"]) for item in items} == visible

                absent = await client.get(
                    f"/v1/memories/{UUID(int=0xBEEF)}", params={"ceiling": ceiling.value}
                )
                assert absent.status_code == 404, absent.text
                for record in above:
                    hidden = await client.get(
                        f"/v1/memories/{record.id}", params={"ceiling": ceiling.value}
                    )
                    assert hidden.status_code == absent.status_code
                    assert hidden.headers["content-type"] == absent.headers["content-type"]
                    assert _redacted(hidden) == _redacted(absent)
                    assert str(record.id) not in hidden.text
                    assert record.statement not in hidden.text

    asyncio.run(exercise_ceiling())


# ---------------------------------------------------------------------------
# Gate 3 — gate.memory.read_api_principal_isolation
# ---------------------------------------------------------------------------


async def test_a_principal_sees_only_its_own_beliefs() -> None:
    """Another principal's and another tenant's beliefs are invisible, and 404 not 403.

    `authorization_error` on a foreign identifier would confirm the belief
    exists; the cross-tenant not-found rule extends to the neighbouring
    principal for the same reason.
    """

    async with build(
        settings=_enabled_settings(),
        storage="memory",
        sequential_ids=True,
        principal=_principal(),
    ) as composition:
        mine = _belief(belief_id=1, position=1)
        other_principal = _belief(belief_id=2, position=2, principal_id="neighbour")
        other_tenant = _belief(belief_id=3, position=3, tenant_id="tenant-b")
        await _seed(composition, [mine, other_principal, other_tenant])

        async with _client(composition) as client:
            listed = await client.get(
                "/v1/memories", params={"ceiling": "restricted", "limit": 200}
            )
            assert listed.status_code == 200, listed.text
            assert _ids(listed.json()) == [str(mine.id)]
            assert other_principal.statement not in listed.text
            assert other_tenant.statement not in listed.text

            for foreign in (other_principal, other_tenant):
                detail = await client.get(
                    f"/v1/memories/{foreign.id}", params={"ceiling": "restricted"}
                )
                assert detail.status_code == 404, detail.text
                assert detail.json()["error"]["code"] == "not_found"
                assert foreign.statement not in detail.text

        # The isolation is symmetric: the neighbour cannot see the owner's row.
        neighbour = _principal(principal_id="neighbour")
        async with _client(composition, principal=neighbour) as client:
            listed = await client.get(
                "/v1/memories", params={"ceiling": "restricted", "limit": 200}
            )
            assert _ids(listed.json()) == [str(other_principal.id)]
            detail = await client.get(f"/v1/memories/{mine.id}", params={"ceiling": "restricted"})
            assert detail.status_code == 404
            assert detail.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Gate 4 — gate.memory.read_api_pagination
# ---------------------------------------------------------------------------


async def test_keyset_paging_neither_skips_nor_repeats() -> None:
    """A walk under concurrent writes and retirements loses and doubles nothing.

    A belief live throughout the walk appears exactly once; a belief written
    or retired mid-walk appears at most once. `next_cursor` is null on the
    last page and only there, a malformed cursor is refused, and a cursor
    replayed against an unchanged store returns the identical page.
    """

    async with build(
        settings=_enabled_settings(),
        storage="memory",
        sequential_ids=True,
        principal=_principal(),
    ) as composition:
        seeded = [_belief(belief_id=index, position=index * 2) for index in range(1, 7)]
        await _seed(composition, seeded)
        retired_source = seeded[0]  # position 2, walked last
        survivors = {record.id for record in seeded[1:]}

        async with _client(composition) as client:
            malformed = await client.get(
                "/v1/memories", params={"ceiling": "restricted", "cursor": "not-a-cursor"}
            )
            assert malformed.status_code == 400, malformed.text
            assert malformed.json()["error"]["code"] == "malformed_request"

            first = await client.get("/v1/memories", params={"ceiling": "restricted", "limit": 2})
            assert first.status_code == 200, first.text
            first_body = first.json()
            assert first_body["next_cursor"] is not None

            # The cursorless first page is itself idempotent over an unchanged
            # store. This is not the declaration's cursor-replay clause — no
            # cursor is sent — so it is a preliminary; every real cursor is
            # replayed after the walk below.
            unpaged_again = await client.get(
                "/v1/memories", params={"ceiling": "restricted", "limit": 2}
            )
            assert unpaged_again.json() == first_body

            # One belief lands inside the unwalked range and one lands behind
            # the walk; one unwalked belief is retired out of the live set.
            inserted = _belief(belief_id=99, position=5)
            behind = _belief(belief_id=98, position=99)
            await _seed(composition, [inserted, behind])
            async with composition.uow_factory() as uow:
                # The port method the decay sweep uses. Its position is left
                # where it was, which is the stricter case: the belief is
                # still inside the unwalked range and must drop out of the
                # live default there rather than by moving.
                await uow.memories.reinforce(
                    retired_source.model_copy(
                        update={"status": MemoryStatus.RETIRED, "valid_to": NOW}
                    )
                )

            walked = list(first_body["items"])
            cursor = first_body["next_cursor"]
            pages = [first_body]
            for _ in range(10):
                page = await client.get(
                    "/v1/memories",
                    params={"ceiling": "restricted", "limit": 2, "cursor": cursor},
                )
                assert page.status_code == 200, page.text
                body = page.json()
                pages.append(body)
                walked.extend(body["items"])
                cursor = body["next_cursor"]
                if cursor is None:
                    break
            else:  # pragma: no cover - the walk must terminate
                pytest.fail("the keyset walk did not reach a final page")

            # `next_cursor` is null on the last page and only there.
            assert pages[-1]["next_cursor"] is None
            assert all(body["next_cursor"] is not None for body in pages[:-1])

            walked_ids = [item["id"] for item in walked]
            assert len(walked_ids) == len(set(walked_ids)), "the walk repeated a belief"
            # Every belief live throughout the walk is seen exactly once.
            assert survivors <= {UUID(identifier) for identifier in walked_ids}
            # Nothing outside the corpus appears, and the belief written
            # behind the walk is not resurrected into it.
            assert {UUID(identifier) for identifier in walked_ids} <= survivors | {
                inserted.id,
                retired_source.id,
            }
            assert str(behind.id) not in walked_ids
            # Retiring an unwalked belief drops it from the live default.
            assert str(retired_source.id) not in walked_ids

            # A short page is the end of the walk, never a hole in it: the
            # store overfetches by one, so only the final page is short.
            assert all(len(body["items"]) == 2 for body in pages[:-1])

            # Re-reading a cursor against an unchanged store returns an
            # identical page. Nothing has been written since the walk began
            # its second request, so every cursor the walk emitted must
            # reproduce, byte for byte, the page it originally produced —
            # including that page's own `next_cursor`. This is the clause's
            # real instrument: it drives the encode/decode round trip, so a
            # cursor carrying a nonce or a timestamp, or one that decoded
            # lossily, would fail here where the cursorless re-read above
            # would not notice.
            assert len(pages) >= 3, pages
            for index, body in enumerate(pages[:-1]):
                again = await client.get(
                    "/v1/memories",
                    params={
                        "ceiling": "restricted",
                        "limit": 2,
                        "cursor": body["next_cursor"],
                    },
                )
                assert again.status_code == 200, again.text
                assert again.json() == pages[index + 1], body["next_cursor"]


# ---------------------------------------------------------------------------
# Gate 5 — gate.memory.read_api_filters
# ---------------------------------------------------------------------------


async def test_every_filter_selects_the_documented_set() -> None:
    """Status, belief type, subject, session, and text each select the declared set.

    They compose across kinds and union within a kind, the default status set
    is the live one, and the text filter agrees exactly with the shared
    lexical helpers applied directly to the same corpus.
    """

    corpus = [
        _belief(
            belief_id=1,
            position=1,
            subject="Answer Style",
            statement="Dashboards use the emerald theme",
            session_id=SESSION_A,
        ),
        _belief(
            belief_id=2,
            position=2,
            subject="release cadence",
            statement="Deployment runs on Fridays",
            belief_type=BeliefType.FACT,
            status=MemoryStatus.PROVISIONAL,
            session_id=SESSION_A,
        ),
        _belief(
            belief_id=3,
            position=3,
            subject="history",
            statement="The old emerald preference",
            status=MemoryStatus.SUPERSEDED,
            session_id=SESSION_B,
        ),
        _belief(
            belief_id=4,
            position=4,
            subject="wearables",
            statement="Apple Watch charges overnight",
            belief_type=BeliefType.FACT,
            session_id=SESSION_B,
        ),
        _belief(
            belief_id=5,
            position=5,
            subject="retired topic",
            statement="An emerald belief nobody holds",
            status=MemoryStatus.RETIRED,
            session_id=SESSION_B,
        ),
    ]
    live = {record.id for record in corpus if record.status in LIVE_MEMORY_STATUSES}
    assert live == {UUID(int=1), UUID(int=2), UUID(int=4)}

    async with build(
        settings=_enabled_settings(),
        storage="memory",
        sequential_ids=True,
        principal=_principal(),
    ) as composition:
        await _seed(composition, corpus)

        async def listed(**params: Any) -> set[UUID]:
            """Return the identifiers selected by one filter combination."""

            response = await client.get(
                "/v1/memories", params={"ceiling": "restricted", "limit": 200, **params}
            )
            assert response.status_code == 200, response.text
            return {UUID(identifier) for identifier in _ids(response.json())}

        async with _client(composition) as client:
            # The default status set is the live one.
            assert await listed() == live
            assert await listed(status=["active", "provisional"]) == live

            # A repeated parameter unions within its kind.
            assert await listed(status=["superseded"]) == {UUID(int=3)}
            assert await listed(status=["superseded", "retired"]) == {UUID(int=3), UUID(int=5)}
            # `candidate` is legal and selects nothing, since a candidate is
            # not a stored belief.
            assert await listed(status=["candidate"]) == set()

            assert await listed(belief_type=["fact"]) == {UUID(int=2), UUID(int=4)}
            assert await listed(belief_type=["fact", "preference"]) == live

            # Subject is an exact, case-insensitive match, never a prefix.
            assert await listed(subject="answer style") == {UUID(int=1)}
            assert await listed(subject="ANSWER STYLE") == {UUID(int=1)}
            assert await listed(subject="answer") == set()
            assert await listed(subject="answer styles") == set()

            assert await listed(session_id=str(SESSION_A)) == {UUID(int=1), UUID(int=2)}
            assert await listed(session_id=str(SESSION_B)) == {UUID(int=4)}

            # Kinds intersect: a status and a belief type mean both conditions.
            assert await listed(belief_type=["fact"], session_id=str(SESSION_B)) == {UUID(int=4)}
            assert await listed(status=["superseded"], session_id=str(SESSION_A)) == set()
            assert await listed(text="emerald", status=["superseded"]) == {UUID(int=3)}
            assert await listed(text="emerald", belief_type=["fact"]) == set()

            # The text filter equals the shared lexical helpers applied
            # directly, over the same statuses the request selects.
            for text in ("emerald", "themes", "apple watch", "friday", "..."):
                term_lexemes = lexical_term_lexemes(lexical_query_terms(text))
                expected = {
                    record.id
                    for record in corpus
                    if record.id in live
                    and lexical_text_matches(term_lexemes, f"{record.subject} {record.statement}")
                }
                assert await listed(text=text) == expected, f"mismatch for text={text!r}"

            # And it composes with a subject the same way.
            assert await listed(text="emerald", subject="answer style") == {UUID(int=1)}
            assert await listed(text="watch", subject="answer style") == set()


# ---------------------------------------------------------------------------
# Gate 6 — gate.memory.read_api_read_only
# ---------------------------------------------------------------------------


async def test_the_router_is_read_only() -> None:
    """Every mounted `/v1/memories` route is GET and declares exactly `memory.read`.

    Structural, so a write route added to the router fails the build rather
    than shipping: ADR-0070 decision 3 keeps every correction on the governed
    formation service rather than around it.
    """

    async with build(
        settings=_enabled_settings(),
        storage="memory",
        sequential_ids=True,
        principal=_principal(),
    ) as composition:
        app = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )

    routes = memory_routes(app)
    assert {route.path for route in routes} == {"/v1/memories", "/v1/memories/{memory_id}"}
    for route in routes:
        methods = set(route.methods or set())
        # Starlette adds HEAD alongside GET; no other verb may appear.
        assert methods <= {"GET", "HEAD"}, (route.path, methods)
        assert "GET" in methods, (route.path, methods)
        declared = (route.openapi_extra or {}).get("required_scope")
        assert declared == "memory.read", (route.path, declared)

    # The same statement over the published document: the surface advertises
    # only GET, and every operation on it requires the one exact scope.
    document = app.openapi()
    memory_paths = {
        path: operations
        for path, operations in document["paths"].items()
        if path.startswith("/v1/memories")
    }
    assert set(memory_paths) == {"/v1/memories", "/v1/memories/{memory_id}"}
    for path, operations in memory_paths.items():
        assert set(operations) == {"get"}, (path, sorted(operations))


# ---------------------------------------------------------------------------
# Gate 7 — gate.memory.read_api_flag_absent
# ---------------------------------------------------------------------------


async def test_the_flag_is_a_real_switch() -> None:
    """Unset, the flag removes the routes and the document entries, not the scope.

    A read surface over everything the platform believes is turned on
    deliberately; configuration validation must still recognize a principal
    granted `memory.read` while it is off.
    """

    off = memory_settings()
    assert off.memory_api_enabled is False
    async with build(
        settings=off,
        storage="memory",
        sequential_ids=True,
        principal=_principal(),
    ) as composition:
        app = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        assert memory_routes(app) == []
        document = app.openapi()
        assert not [path for path in document["paths"] if path.startswith("/v1/memories")]

        # Nothing answers on the surface, and the miss is an ordinary 404.
        async with _client(composition) as client:
            for path in ("/v1/memories", f"/v1/memories/{UUID(int=1)}"):
                response = await client.get(path, params={"ceiling": "restricted"})
                assert response.status_code == 404, response.text
                assert response.json()["error"]["code"] == "not_found"

    # The scope stays in the closed vocabulary and survives configuration
    # validation with the router switched off. Loading a real token-mode
    # environment that grants only `memory.read`, with
    # AGENT_MEMORY_API_ENABLED absent, is the configuration path: it must
    # produce a principal holding the scope and a flag that is still off.
    assert "memory.read" in PLATFORM_SCOPES
    granted = load_settings(
        {
            "DATABASE_URL": "postgresql+asyncpg://127.0.0.1:1/unused",
            "DEPLOYMENT_MODE": "development",
            "AUTH_MODE": "token",
            "AUTH_TOKEN": "test-bearer-token",
            "AUTH_TENANT_ID": TENANT,
            "AUTH_PRINCIPAL_ID": PRINCIPAL_ID,
            "AUTH_ROLES": "user",
            "AUTH_SCOPES": "memory.read",
            "SANDBOX_MECHANISM": "microvm",
            "OPENAI_MODEL": "",
        }
    )
    assert granted.memory_api_enabled is False
    assert granted.auth_scopes == frozenset({"memory.read"})
    # The predicate every composition root applies to those settings, and the
    # closed-vocabulary validator, both accept the scope; a neighbouring name
    # that is not in the vocabulary is refused, so the acceptance is not
    # vacuous.
    assert set(granted.auth_scopes) - set(PLATFORM_SCOPES) == set()
    validate_required_scopes({"memory.read"})
    with pytest.raises(ToolValidationError):
        validate_required_scopes({"memory.write"})


# ---------------------------------------------------------------------------
# Gate 8 — gate.memory.browse_contract_parity
# ---------------------------------------------------------------------------

_CONTRACT_SUITE = ROOT / "tests" / "contract" / "test_memory_store_contract.py"
_PARITY_SUITE = ROOT / "tests" / "integration" / "test_memory_postgres_m9.py"
_IN_MEMORY_ADAPTER = ROOT / "src" / "agent_core" / "adapters" / "memory" / "in_memory.py"
_POSTGRES_ADAPTER = (
    ROOT / "src" / "agent_core" / "adapters" / "persistence" / "memory_repositories.py"
)


def _module(path: Path) -> ast.Module:
    """Parse a Python module used by the structural parity gate."""

    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function_names(tree: ast.Module) -> set[str]:
    """Collect every function name declared in a parsed module."""

    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _browse_method(path: Path) -> ast.AsyncFunctionDef:
    """Find the async browse implementation in an adapter module."""

    for node in ast.walk(_module(path)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "browse":
            return node
    raise AssertionError(f"{path} declares no async browse method")


def _referenced_names(node: ast.AST) -> set[str]:
    """Collect simple and attribute names referenced under an AST node."""

    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names


def test_both_stores_browse_identically() -> None:
    """The two-half parity mechanism the design names is in place for browse.

    The shared contract suite fixes browse behavior against the in-memory
    adapter — order, the keyset boundary and its identifier tiebreak, every
    filter, and the text query — and the PostgreSQL parity suite answers the
    same `MemoryBrowseQuery` values from a live store and compares the two
    adapters over one corpus. Only one half of lexical parity holds by
    construction: both adapters derive the query's terms through the same
    `lexical_query_terms` helper, then match with two different engines —
    `plainto_tsquery('simple', ...)` against a `to_tsvector('simple', ...)` in
    PostgreSQL, `lexical_text_matches` emulating that lexeme split and its
    conjunction in memory — so the second half is asserted rather than
    assumed. The subject predicate is the same shape: SQL `lower()` and
    Python `lower()` are two implementations shown to agree here, not one
    shared function.
    """

    contract_functions = _function_names(_module(_CONTRACT_SUITE))
    required_contract = {
        "test_browse_ceiling_filter_excludes_records_above_it",
        "test_browse_enforces_tenant_and_principal_isolation",
        "test_browse_status_default_is_the_live_set_and_can_be_overridden",
        "test_browse_belief_type_filter_composes_with_the_live_default",
        "test_browse_subject_filter_is_exact_and_lowercased",
        "test_browse_session_id_filter_matches_the_source_session",
        "test_browse_text_filter_matches_the_shared_lexical_helpers_directly",
        "test_browse_orders_newest_first_with_id_tiebreak_and_overfetches_by_one",
        "test_browse_keyset_predicate_walks_without_skipping_or_repeating_across_writes",
        "test_browse_keyset_tiebreak_includes_the_higher_identifier_sibling",
    }
    missing = required_contract - contract_functions
    assert not missing, f"the shared browse contract suite lost coverage: {sorted(missing)}"

    parity_functions = _function_names(_module(_PARITY_SUITE))
    required_parity = {
        "test_postgres_and_memory_stores_agree_on_browse_filters_and_text",
        "test_postgres_browse_keyset_pagination_matches_the_documented_predicate",
        "test_postgres_browse_enforces_principal_isolation_and_status_override",
        "test_postgres_browse_keyset_tiebreak_includes_the_higher_identifier_sibling",
    }
    missing = required_parity - parity_functions
    assert not missing, f"the PostgreSQL browse parity suite lost coverage: {sorted(missing)}"

    # The parity half genuinely compares the two adapters rather than
    # re-asserting the PostgreSQL answer against a hand-written expectation.
    parity_source = _PARITY_SUITE.read_text(encoding="utf-8")
    assert "InMemoryMemoryStore" in parity_source
    for call in ("uow.memories.browse(query)", "mirror.browse(query)"):
        assert call in parity_source, call
    # It lives in the integration tree, so it runs against a real database.
    assert _PARITY_SUITE.parent.name == "integration"

    # Both adapters derive their query terms through the same
    # lexical_query_terms helper and lowercase their subject comparison, which
    # is what the shared contract and PostgreSQL parity suites above then show
    # two different matching engines agree on — structural rather than a
    # coincidence of two implementations.
    for adapter in (_IN_MEMORY_ADAPTER, _POSTGRES_ADAPTER):
        referenced = _referenced_names(_browse_method(adapter))
        assert "lexical_query_terms" in referenced, adapter
        assert "lower" in referenced, adapter
        assert "casefold" not in referenced, adapter


# ---------------------------------------------------------------------------
# Gate 9 — gate.memory.read_api_view_projection
# ---------------------------------------------------------------------------


def test_the_projection_is_exactly_the_exposure_list() -> None:
    """`MemoryView` carries the exposure list and can carry nothing else.

    Both directions: every exposed name serializes, and no withheld name can
    reach a response however the view is constructed, because the model has
    no such field and forbids unknown ones.
    """

    assert set(MemoryView.model_fields) == set(EXPOSED_FIELDS)
    assert len(EXPOSED_FIELDS) == 26
    assert MemoryView.model_config["extra"] == "forbid"
    assert MemoryView.model_config["frozen"] is True
    assert set(WITHHELD_FIELDS).isdisjoint(MemoryView.model_fields)

    # Every withheld field is populated with a value that would be
    # unmistakable if it leaked; the exposed provenance trio is populated
    # with a value that would be unmistakable if it did NOT appear.
    record = _belief(belief_id=7, position=4242).model_copy(
        update={
            "tenant_id": "leaked-tenant",
            "principal_id": "leaked-principal",
            "utility": 0.7654321,
            "formation_run_id": UUID(int=0xF0F0),
            "consolidation_policy_version": "exposed-policy@99",
            "origin_scopes": ["exposed-scope"],
        }
    )
    view = MemoryView.from_record(record)
    for dumped in (view.model_dump(), view.model_dump(mode="json")):
        assert set(dumped) == set(EXPOSED_FIELDS)
    serialized = view.model_dump_json()
    for sentinel in (
        "leaked-tenant",
        "leaked-principal",
        "0.7654321",
        "4242",
    ):
        assert sentinel not in serialized, sentinel
    for withheld in WITHHELD_FIELDS:
        assert f'"{withheld}"' not in serialized, withheld

    # The trio is exposed: its value is present and its key serializes.
    for exposed_sentinel in ("exposed-policy@99", "exposed-scope", str(UUID(int=0xF0F0))):
        assert exposed_sentinel in serialized, exposed_sentinel
    for exposed_name in ("formation_run_id", "consolidation_policy_version", "origin_scopes"):
        assert f'"{exposed_name}"' in serialized, exposed_name

    # A withheld field cannot be introduced on any construction path.
    for withheld in WITHHELD_FIELDS:
        with pytest.raises(ValidationError):
            MemoryView(**{**view.model_dump(), withheld: "smuggled"})

    # And no response path — list, detail, or error envelope — emits one.
    asyncio.run(_projection_over_the_wire())


async def _projection_over_the_wire() -> None:
    """The list, detail, and error paths all obey the exposure list."""

    async with build(
        settings=_enabled_settings(),
        storage="memory",
        sequential_ids=True,
        principal=_principal(),
    ) as composition:
        record = _belief(belief_id=7, position=4242).model_copy(
            update={"consolidation_policy_version": "exposed-policy@99"}
        )
        await _seed(composition, [record])
        async with _client(composition) as client:
            listed = await client.get("/v1/memories", params={"ceiling": "restricted"})
            detail = await client.get(f"/v1/memories/{record.id}", params={"ceiling": "restricted"})
            missing = await client.get(
                f"/v1/memories/{UUID(int=404)}", params={"ceiling": "restricted"}
            )
    assert set(listed.json()["items"][0]) == set(EXPOSED_FIELDS)
    assert set(detail.json()) == set(EXPOSED_FIELDS)
    for response in (listed, detail, missing):
        for withheld in WITHHELD_FIELDS:
            assert f'"{withheld}"' not in response.text, (response.url, withheld)
    # The store-position sentinel is a bare number, so it is only meaningful in
    # a body that can carry a belief. The error envelope carries a random
    # request id instead, which contains "4242" by chance about once in a few
    # thousand runs; asserting over it would make the gate a coin flip without
    # covering anything, since `store_position` cannot reach an error body.
    for response in (listed, detail):
        assert "4242" not in response.text, response.url
        assert "exposed-policy@99" in response.text, response.url


# ---------------------------------------------------------------------------
# Gate 10 — gate.memory.read_api_error_vocabulary
# ---------------------------------------------------------------------------


async def test_every_error_is_a_member_of_the_closed_vocabulary() -> None:
    """Every failure of these routes carries a closed-vocabulary code and status.

    The design's error table names the four members of the closed vocabulary
    (http-api-and-streaming.md:111) these routes may raise:
    `malformed_request`, `authentication_error`, `authorization_error`, and
    `not_found`. They add no code of their own, so the observed set of
    (status, code) pairs is asserted to be exactly those four rather than
    merely contained in the vocabulary.
    """

    missing_id = UUID(int=0xD15C)
    async with build(
        settings=_enabled_settings(),
        storage="memory",
        sequential_ids=True,
        principal=_principal(),
    ) as composition:
        restricted = _belief(belief_id=1, position=1, sensitivity=Sensitivity.RESTRICTED)
        await _seed(composition, [restricted])

        # "Either succeeds": a served read is not an error envelope at all.
        async with _client(composition) as client:
            for path in ("/v1/memories", f"/v1/memories/{restricted.id}"):
                served = await client.get(path, params={"ceiling": "restricted"})
                assert served.status_code == 200, served.text
                assert "error" not in served.json()

        token_settings = replace(
            composition.settings,
            auth_mode=AuthMode.TOKEN,
            auth_token=SecretStr("test-bearer-token"),
            auth_tenant_id=TENANT,
            auth_principal_id=PRINCIPAL_ID,
            auth_roles=frozenset({"user"}),
            auth_scopes=PLATFORM_SCOPES,
        )
        authorized = {"Authorization": "Bearer test-bearer-token"}

        # Missing and malformed parameters, unknown identifiers, and an
        # above-ceiling read, all under a credential that resolves.
        requests: list[tuple[str, dict[str, Any], dict[str, str]]] = [
            ("/v1/memories", {}, authorized),
            ("/v1/memories", {"ceiling": "top-secret"}, authorized),
            ("/v1/memories", {"ceiling": "restricted", "limit": 0}, authorized),
            ("/v1/memories", {"ceiling": "restricted", "limit": "many"}, authorized),
            ("/v1/memories", {"ceiling": "restricted", "status": "unknown"}, authorized),
            ("/v1/memories", {"ceiling": "restricted", "belief_type": "unknown"}, authorized),
            ("/v1/memories", {"ceiling": "restricted", "cursor": "not-a-cursor"}, authorized),
            ("/v1/memories", {"ceiling": "restricted", "session_id": "not-a-uuid"}, authorized),
            (f"/v1/memories/{missing_id}", {}, authorized),
            (f"/v1/memories/{missing_id}", {"ceiling": "restricted"}, authorized),
            (f"/v1/memories/{restricted.id}", {"ceiling": "public"}, authorized),
            ("/v1/memories/not-a-uuid", {"ceiling": "restricted"}, authorized),
            # No credential at all.
            ("/v1/memories", {"ceiling": "restricted"}, {}),
            (f"/v1/memories/{restricted.id}", {"ceiling": "restricted"}, {}),
            # A credential that does not resolve.
            ("/v1/memories", {"ceiling": "restricted"}, {"Authorization": "Bearer wrong"}),
        ]

        observed: set[tuple[int, str]] = set()

        def record(response: httpx.Response, where: object) -> None:
            """Validate and remember one closed-vocabulary error response."""

            assert response.status_code >= 400, (where, response.text)
            error = response.json()["error"]
            code = error["code"]
            assert code in ERROR_CODE_VOCABULARY, (where, code)
            # The status the shared map documents for that code, and no other.
            statuses = {
                mapping.status for mapping in ERROR_STATUS_MAP.values() if mapping.code == code
            }
            expected_status = API_ERROR_STATUS.get(code)
            if expected_status is None:
                assert len(statuses) == 1, code
                expected_status = statuses.pop()
            assert response.status_code == expected_status, (where, code)
            assert set(error) == {"code", "message", "details", "request_id"}
            observed.add((response.status_code, code))

        async with _client(composition, app_settings=token_settings) as client:
            for path, params, headers in requests:
                record(await client.get(path, params=params, headers=headers), (path, params))

        # A principal without the scope is forbidden rather than served.
        unscoped = _principal(scopes=set(PLATFORM_SCOPES) - {"memory.read"})
        async with _client(composition, principal=unscoped, app_settings=token_settings) as client:
            for path in ("/v1/memories", f"/v1/memories/{restricted.id}"):
                record(
                    await client.get(path, params={"ceiling": "restricted"}, headers=authorized),
                    path,
                )

    # The routes raise exactly the four documented conditions and no others.
    assert observed == {
        (400, "malformed_request"),
        (401, "authentication_error"),
        (403, "authorization_error"),
        (404, "not_found"),
    }, sorted(observed)
