"""Serve an ADR-0171 packet from stdin; never accepts a production DB connection."""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx
import uvicorn

from agent_core.adapters.determinism import FixedClock, RandomIdFactory, SystemClock
from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryEventRepository,
    InMemoryRunRepository,
    InMemorySessionRepository,
    InMemoryToolInvocationRepository,
)
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.bootstrap import _memory_uow_repositories
from agent_core.evals.owner_memory_budget import ExperimentBudget
from agent_core.evals.owner_memory_fixture import FixturePacket
from agent_core.evals.owner_memory_review import create_review_app
from agent_core.evals.owner_memory_runtime import FixtureResources
from agent_core.ports.determinism import Clock


def fixture_resources(at: datetime, clock: Clock) -> FixtureResources:
    """Compose a fresh volatile store, with no settings or database connection."""
    sessions = InMemorySessionRepository()
    runs = InMemoryRunRepository(sessions, clock)
    event_clock = FixedClock(at)
    events = InMemoryEventRepository(sessions, event_clock)
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=InMemoryAgentRepository(),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=InMemoryToolInvocationRepository(runs),
            clock=clock,
        )
    )
    return FixtureResources(factory, sessions, events, event_clock, RandomIdFactory())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8774)
    args = parser.parse_args()
    raw = sys.stdin.buffer.read(262145)
    try:
        if len(raw) > 262144:
            raise ValueError("oversized packet")
        packet = FixturePacket.model_validate_json(raw)
        if packet.model.provider != "openai" or packet.residency_provider != "openai":
            raise ValueError("unsupported evaluation provider")
        # Match the application's canonical credential precedence without loading
        # production settings or passing other credentials into the evaluator.
        api_key = (
            os.environ.get("VEETBOT_OPENAI_KEY", "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip()
        )
        if not api_key:
            raise ValueError("missing provider credential")
        try:
            # No memory text or generation request: fail before inviting review.
            response = httpx.get(
                "https://api.openai.com/v1/models/" + quote(packet.model.model, safe=""),
                headers={"Authorization": "Bearer " + api_key},
                timeout=15,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            raise SystemExit(
                "Cannot open review: check the configured OpenAI credential and model access."
            ) from None
        provider = OpenAIResponsesProvider(api_key=api_key)
        budget = ExperimentBudget(
            Path.home() / ".local/state/veetbot/owner-memory-evaluation.sqlite3"
        )
        clock = SystemClock()
        app = create_review_app(
            packet,
            provider=provider,
            budget=budget,
            clock=clock,
            port=args.port,
            resources=lambda at: fixture_resources(at, clock),
        )
    except (ValueError, OSError):
        raise SystemExit(
            "Cannot prepare review: invalid packet, credential or local budget."
        ) from None
    del raw, packet
    print(app.state.review_url, flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="error")


if __name__ == "__main__":
    main()
