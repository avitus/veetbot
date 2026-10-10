"""Loopback review boundaries, using fabricated owner input."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from agent_core.adapters.determinism import FixedClock
from agent_core.domain.messages import ModelAttempt, ModelEvent, ModelRequest, ResolvedModel
from agent_core.evals.owner_memory_budget import ExperimentBudget
from agent_core.evals.owner_memory_review import create_review_app
from scripts.review_owner_memory import fixture_resources
from tests.contract.reconsolidation_execution_cases import TrackedFactory
from tests.contract.support import NOW
from tests.unit.test_owner_memory_fixture import packet

TOKEN = "a" * 32
ORIGIN = "http://127.0.0.1:8774"


class UnusedProvider:
    name = "fake"

    async def stream(
        self, request: ModelRequest, model: ResolvedModel, attempt: ModelAttempt
    ) -> AsyncIterator[ModelEvent]:
        raise AssertionError("no provider call before complete human confirmation")
        yield

    async def close(self) -> None:
        pass


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(
        create_review_app(
            packet(),
            provider=UnusedProvider(),
            budget=ExperimentBudget(tmp_path / "budget.sqlite3"),
            clock=FixedClock(NOW),
            resources=lambda at: fixture_resources(at, FixedClock(NOW)),
            port=8774,
            token=TOKEN,
        ),
        base_url=ORIGIN,
    ) as value:
        yield value


def test_private_page_requires_capability_and_loopback_host(client: TestClient) -> None:
    assert client.get("/wrong/packet").status_code == 404
    assert client.get(f"/{TOKEN}/packet", headers={"Host": "evil.example"}).status_code == 404
    response = client.get(f"/{TOKEN}/packet")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.json()["packet"]["sources"][0]["statement"] == "I grow red flowers."


def test_cross_origin_and_missing_confirmation_refused(client: TestClient) -> None:
    path = f"/{TOKEN}/run"
    body = {"packet_digest": packet().digest, "reference_ids": [], "human_confirmed": True}
    assert client.post(path, json=body).status_code == 403
    assert (
        client.post(path, json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    )
    assert client.post(path, json=body, headers={"Origin": ORIGIN}).status_code == 409


def test_cancel_discards_private_input_and_prevents_run(client: TestClient) -> None:
    assert client.post(f"/{TOKEN}/cancel", headers={"Origin": ORIGIN}).status_code == 200
    response = client.get(f"/{TOKEN}/packet")
    assert response.status_code == 410
    assert "flowers" not in response.text
    assert (
        client.post(
            f"/{TOKEN}/run",
            json={"packet_digest": packet().digest, "reference_ids": [], "human_confirmed": True},
            headers={"Origin": ORIGIN},
        ).status_code
        == 410
    )


def test_confirmation_binds_exact_packet_and_is_not_adr_approval(client: TestClient) -> None:
    path = f"/{TOKEN}/confirm"
    body = {
        "packet_digest": "b" * 64,
        "reference_id": str(packet().sources[0].reference_id),
        "human_confirmed": True,
    }
    assert client.post(path, json=body, headers={"Origin": ORIGIN}).status_code == 409
    body["packet_digest"] = packet().digest
    body["human_confirmed"] = False
    assert client.post(path, json=body, headers={"Origin": ORIGIN}).status_code == 400
    body["human_confirmed"] = True
    assert client.post(path, json=body, headers={"Origin": ORIGIN}).status_code == 200
    assert client.get(f"/{TOKEN}/packet").json()["confirmed"] == [body["reference_id"]]
    assert (
        client.post(
            f"/{TOKEN}/run",
            json={"packet_digest": packet().digest, "reference_ids": [], "human_confirmed": True},
            headers={"Origin": ORIGIN},
        ).status_code
        == 409
    )


def test_oversized_and_malformed_commands_never_echo_private_text(client: TestClient) -> None:
    for content in ('{"secret":"' + "x" * 2000 + '"}', '{"statement":"private source'):
        response = client.post(f"/{TOKEN}/confirm", content=content, headers={"Origin": ORIGIN})
        assert response.status_code == 400
        assert "private source" not in response.text


def test_confirmations_are_idempotent_and_survive_waiting(tmp_path: Path) -> None:
    clock = FixedClock(NOW)
    from datetime import timedelta

    value = packet()

    # No provider is reachable while this test only prepares consent.
    with TestClient(
        create_review_app(
            value,
            provider=UnusedProvider(),
            budget=ExperimentBudget(tmp_path / "budget.sqlite3"),
            clock=clock,
            resources=lambda at: fixture_resources(at, clock),
            port=8774,
            token=TOKEN,
        ),
        base_url=ORIGIN,
    ) as client:
        body = {
            "packet_digest": value.digest,
            "reference_id": str(value.sources[0].reference_id),
            "human_confirmed": True,
        }
        assert (
            client.post(f"/{TOKEN}/confirm", json=body, headers={"Origin": ORIGIN}).status_code
            == 200
        )
        assert (
            client.post(f"/{TOKEN}/confirm", json=body, headers={"Origin": ORIGIN}).status_code
            == 200
        )
        assert len(client.get(f"/{TOKEN}/packet").json()["confirmed"]) == 1
        clock.advance(timedelta(days=2))
        response = client.get(f"/{TOKEN}/packet")
        assert response.status_code == 200
        assert response.json()["confirmed"] == [body["reference_id"]]


def test_old_preparation_is_retained_at_startup(tmp_path: Path) -> None:
    from datetime import timedelta

    clock = FixedClock(NOW + timedelta(days=2))
    with TestClient(
        create_review_app(
            packet(),
            provider=UnusedProvider(),
            budget=ExperimentBudget(tmp_path / "budget.sqlite3"),
            clock=clock,
            resources=lambda at: fixture_resources(at, clock),
            port=8774,
            token=TOKEN,
        ),
        base_url=ORIGIN,
    ) as client:
        response = client.get(f"/{TOKEN}/packet")
        assert response.status_code == 200
        assert response.json()["status"] == "pending"


@pytest.mark.parametrize(
    "references", [None, "not-a-list", [None], [], ["unknown"], ["same", "same"]]
)
def test_run_requires_the_exact_visible_selection(client: TestClient, references: object) -> None:
    value = packet()
    for source in value.sources:
        assert (
            client.post(
                f"/{TOKEN}/confirm",
                json={
                    "packet_digest": value.digest,
                    "reference_id": str(source.reference_id),
                    "human_confirmed": True,
                },
                headers={"Origin": ORIGIN},
            ).status_code
            == 200
        )
    response = client.post(
        f"/{TOKEN}/run",
        json={"packet_digest": value.digest, "reference_ids": references, "human_confirmed": True},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 409
    assert response.json()["error"] == "selection_changed"
    assert client.get(f"/{TOKEN}/packet").json()["status"] == "pending"


def test_saved_correction_and_selection_survive_reopening(client: TestClient) -> None:
    value = packet()
    ref = str(value.sources[0].reference_id)
    assert (
        client.post(
            f"/{TOKEN}/edit",
            json={
                "packet_digest": value.digest,
                "reference_id": ref,
                "statement": "I grow silver flowers.",
            },
            headers={"Origin": ORIGIN},
        ).status_code
        == 200
    )
    digest = client.get(f"/{TOKEN}/packet").json()["packet"]["packet_digest"]
    assert (
        client.post(
            f"/{TOKEN}/confirm",
            json={"packet_digest": digest, "reference_id": ref, "human_confirmed": True},
            headers={"Origin": ORIGIN},
        ).status_code
        == 200
    )
    assert client.get(f"/{TOKEN}/").status_code == 200
    data = client.get(f"/{TOKEN}/packet").json()
    assert data["confirmed"] == [ref]
    assert data["packet"]["sources"][0]["statement"] == "I grow silver flowers."


@pytest.mark.parametrize("wait_minutes", [0, 31, 2880])
def test_complete_owner_flow_runs_once_and_shows_real_abstention(
    tmp_path: Path, wait_minutes: int
) -> None:
    import time
    from datetime import timedelta

    from tests.contract.reconsolidation_execution_cases import ExecutionProvider

    class IdleFactory:
        def is_open(self) -> bool:
            return False

    provider = ExecutionProvider(cast(TrackedFactory, IdleFactory()))
    value = packet()
    clock = FixedClock(NOW)
    with TestClient(
        create_review_app(
            value,
            provider=provider,
            budget=ExperimentBudget(tmp_path / "budget.sqlite3"),
            clock=clock,
            resources=lambda at: fixture_resources(at, clock),
            port=8774,
            token=TOKEN,
        ),
        base_url=ORIGIN,
    ) as client:
        for source in value.sources:
            assert (
                client.post(
                    f"/{TOKEN}/confirm",
                    json={
                        "packet_digest": value.digest,
                        "reference_id": str(source.reference_id),
                        "human_confirmed": True,
                    },
                    headers={"Origin": ORIGIN},
                ).status_code
                == 200
            )
        clock.advance(timedelta(minutes=wait_minutes))
        request = {
            "packet_digest": value.digest,
            "reference_ids": [str(s.reference_id) for s in value.sources],
            "human_confirmed": True,
        }
        assert (
            client.post(f"/{TOKEN}/run", json=request, headers={"Origin": ORIGIN}).status_code
            == 202
        )
        assert (
            client.post(f"/{TOKEN}/run", json=request, headers={"Origin": ORIGIN}).status_code
            == 409
        )
        assert (
            client.post(
                f"/{TOKEN}/unconfirm",
                json={
                    "packet_digest": value.digest,
                    "reference_id": str(value.sources[0].reference_id),
                },
                headers={"Origin": ORIGIN},
            ).status_code
            == 409
        )
        for _ in range(100):
            data = client.get(f"/{TOKEN}/packet").json()
            if data["status"] == "finished":
                break
            time.sleep(0.01)
        assert data["result"]["calls"] == 2
        assert data["result"]["operations"] == []
        assert data["result"]["reason"] == "reviewed"
        assert "proposed no changes" in data["result"].get("summary", ""), (
            "a known abstention must not be presented as several possible causes"
        )
        assert len(provider.requests) == 2
        assert all(row["confirmed_at"] == NOW.isoformat() for row in data["result"]["inputs"])
        clock.advance(timedelta(days=2))
        assert client.get(f"/{TOKEN}/packet").json()["result"] == data["result"]
    # Shutdown, unlike navigation or elapsed time, destroys the review.
    assert client.get(f"/{TOKEN}/packet").status_code == 410


def test_correction_changes_only_draft_and_invalidates_previous_confirmations(
    client: TestClient,
) -> None:
    value = packet()
    headers = {"Origin": ORIGIN}
    assert (
        client.post(
            f"/{TOKEN}/confirm",
            json={
                "packet_digest": value.digest,
                "reference_id": str(value.sources[0].reference_id),
                "human_confirmed": True,
            },
            headers=headers,
        ).status_code
        == 200
    )
    body = {
        "packet_digest": value.digest,
        "reference_id": str(value.sources[0].reference_id),
        "statement": "I grow flowers in my own garden.",
    }
    assert client.post(f"/{TOKEN}/edit", json=body, headers=headers).status_code == 200
    data = client.get(f"/{TOKEN}/packet").json()
    assert data["confirmed"] == []
    assert data["packet"]["packet_digest"] != value.digest
    source = data["packet"]["sources"][0]
    assert source["statement"] == body["statement"]
    assert {k: v for k, v in source.items() if k != "statement"} == {
        k: v for k, v in value.sources[0].model_dump(mode="json").items() if k != "statement"
    }
    assert value.sources[0].statement == "I grow red flowers."
    # Stale writes/confirmations cannot restore the old draft or its approval.
    assert client.post(f"/{TOKEN}/edit", json=body, headers=headers).status_code == 409
    assert (
        client.post(
            f"/{TOKEN}/confirm",
            json={
                "packet_digest": value.digest,
                "reference_id": body["reference_id"],
                "human_confirmed": True,
            },
            headers=headers,
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "statement", ["", "   ", 42, "é" * 1025], ids=["empty", "blank", "nontext", "oversized"]
)
def test_invalid_corrections_do_not_change_the_packet(
    client: TestClient,
    statement: object,
) -> None:
    value = packet()
    response = client.post(
        f"/{TOKEN}/edit",
        json={
            "packet_digest": value.digest,
            "reference_id": str(value.sources[0].reference_id),
            "statement": statement,
        },
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 400
    assert client.get(f"/{TOKEN}/packet").json()["packet"]["packet_digest"] == value.digest


def test_corrections_require_origin_and_known_reference(client: TestClient) -> None:
    body = {
        "packet_digest": packet().digest,
        "reference_id": str(packet().sources[0].reference_id),
        "statement": "Corrected draft.",
    }
    assert client.post(f"/{TOKEN}/edit", json=body).status_code == 403
    body["reference_id"] = "00000000-0000-0000-0000-000000000099"
    assert client.post(f"/{TOKEN}/edit", json=body, headers={"Origin": ORIGIN}).status_code == 400


@pytest.mark.parametrize("corrected", [False, True])
def test_run_accepts_three_of_four_and_keeps_the_fourth_out(
    tmp_path: Path, corrected: bool
) -> None:
    import time

    from tests.contract.reconsolidation_execution_cases import ExecutionProvider
    from tests.unit.test_owner_memory_fixture import four_statements

    class IdleFactory:
        def is_open(self) -> bool:
            return False

    provider = ExecutionProvider(cast(TrackedFactory, IdleFactory()))
    value = four_statements()
    with TestClient(
        create_review_app(
            value,
            provider=provider,
            budget=ExperimentBudget(tmp_path / "subset.sqlite3"),
            clock=FixedClock(NOW),
            resources=lambda at: fixture_resources(at, FixedClock(NOW)),
            port=8774,
            token=TOKEN,
        ),
        base_url=ORIGIN,
    ) as client:
        if corrected:
            assert (
                client.post(
                    f"/{TOKEN}/edit",
                    json={
                        "packet_digest": value.digest,
                        "reference_id": str(value.sources[0].reference_id),
                        "statement": "I grow silver flowers.",
                    },
                    headers={"Origin": ORIGIN},
                ).status_code
                == 200
            )
            value = value.model_copy(
                update={
                    "sources": (
                        value.sources[0].model_copy(update={"statement": "I grow silver flowers."}),
                        *value.sources[1:],
                    )
                }
            )
        for source in value.sources[:3]:
            assert (
                client.post(
                    f"/{TOKEN}/confirm",
                    json={
                        "packet_digest": value.digest,
                        "reference_id": str(source.reference_id),
                        "human_confirmed": True,
                    },
                    headers={"Origin": ORIGIN},
                ).status_code
                == 200
            )
        assert (
            client.post(
                f"/{TOKEN}/run",
                json={
                    "packet_digest": value.digest,
                    "reference_ids": [str(s.reference_id) for s in value.sources[:3]],
                    "human_confirmed": True,
                },
                headers={"Origin": ORIGIN},
            ).status_code
            == 202
        )
        for _ in range(100):
            data = client.get(f"/{TOKEN}/packet").json()
            if data["status"] == "finished":
                break
            time.sleep(0.01)
        assert len(data["result"]["inputs"]) == 3
        assert data["result"]["outcome"] == "complete"
        assert all("UNSELECTED_SENTINEL" not in r.model_dump_json() for r in provider.requests)
        if corrected:
            assert any("I grow silver flowers." in r.model_dump_json() for r in provider.requests)
            assert all("I grow red flowers." not in r.model_dump_json() for r in provider.requests)
        # Completed experiments cannot be edited and restarted.
        assert (
            client.post(
                f"/{TOKEN}/edit",
                json={
                    "packet_digest": value.digest,
                    "reference_id": str(value.sources[0].reference_id),
                    "statement": "A late edit.",
                },
                headers={"Origin": ORIGIN},
            ).status_code
            == 409
        )


async def test_concurrent_correction_rejects_a_stale_request(tmp_path: Path) -> None:
    import asyncio
    import json

    import httpx

    value = packet()
    app = create_review_app(
        value,
        provider=UnusedProvider(),
        budget=ExperimentBudget(tmp_path / "race.sqlite3"),
        clock=FixedClock(NOW),
        resources=lambda at: fixture_resources(at, FixedClock(NOW)),
        port=8774,
        token=TOKEN,
    )
    started, release = asyncio.Event(), asyncio.Event()
    body = {
        "packet_digest": value.digest,
        "reference_id": str(value.sources[0].reference_id),
        "statement": "First correction.",
    }

    async def delayed() -> AsyncIterator[bytes]:
        yield b" "
        started.set()
        await release.wait()
        yield json.dumps(body).encode()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as client:
        pending = asyncio.create_task(client.post(f"/{TOKEN}/edit", content=delayed()))
        await asyncio.wait_for(started.wait(), 2)
        newer = await client.post(f"/{TOKEN}/edit", json={**body, "statement": "Newer correction."})
        release.set()
        stale = await pending
        assert newer.status_code == 200
        assert stale.status_code == 409
        data = (await client.get(f"/{TOKEN}/packet")).json()
        assert data["packet"]["sources"][0]["statement"] == "Newer correction."
