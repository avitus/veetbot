"""ADR-0134: a Chat plan has the same identity in every worker process.

Each worker process draws its own string hash seed, and set iteration order
follows it. Before ADR-0134 the production roster's two tools with two scopes,
`email.feedback` and `email.unsubscribe`, dumped their scope sets in that
order. After a restart a chat's next message could rotate its plan, or fail
with "the frozen context prefix no longer matches its plan" when the planner
and the builder saw differently rebuilt copies of the same plan.

Each process below answers a new chat's first message on the production-shaped
roster, then re-renders the stored plan event with jsonb's key order after
repeated copies, as the planner cache and a restarted worker do.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.unit.test_tool_scope_order import HASH_SEEDS

_REPOSITORY = Path(__file__).resolve().parents[2]
_PLAN_IDENTITY = """
import asyncio
import hashlib
import json
import tempfile
from pathlib import Path

from agent_core.context.rendering import build_prefix, prefix_bytes
from agent_core.domain.context import ContextPlan
from agent_core.domain.messages import ScriptedTurn, StopReason
from tests.gates.test_deferred_tools_adr0123 import _production
from tests.unit.test_deferred_tool_index import jsonb_key_order


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _factory, context = await _production(
            Path(tmp), [ScriptedTurn(text="Ready.", stop_reason=StopReason.END_TURN)]
        )
        async with context as app:
            session_id = await app.sessions.create()
            run_id = await app.runs.submit("What can you do for me?", session_id)
            run = await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=60)
            async with app.uow_factory() as uow:
                session = await uow.sessions.get(session_id, app.principal)
                agent = await uow.agents.get_version(session.agent_id, session.agent_version)
                event = await uow.events.latest_before(
                    session_id, (1 << 63) - 1, "context.plan.created", app.principal
                )
    stored = json.loads(json.dumps(event.payload["plan"]))
    plan = ContextPlan.model_validate(jsonb_key_order(stored))
    rendered = set()
    for _ in range(4):
        prefix = build_prefix(
            agent,
            plan.tool_specs,
            plan.skill_catalog,
            plan.memory_snapshot,
            persona=plan.persona_text,
            deferred_tools=plan.deferred_tool_specs,
        )
        encoded = prefix_bytes(prefix, plan.tool_specs, plan.deferred_tool_specs)
        rendered.add(hashlib.sha256(encoded).hexdigest())
        plan = plan.model_copy(deep=True)
    print(
        json.dumps(
            {
                "status": run.status.value,
                "scopes": sorted(
                    sorted(spec["required_scopes"])
                    for spec in (*stored["tool_specs"], *stored["deferred_tool_specs"])
                    if len(spec["required_scopes"]) > 1
                ),
                "identity": [stored["prefix_sha256"], stored["tool_schema_sha256"]],
                "rendered": sorted(rendered),
            }
        )
    )


asyncio.run(main())
"""


def test_the_production_plan_has_one_identity_under_every_hash_seed() -> None:
    processes = {
        seed: subprocess.Popen(
            [sys.executable, "-c", _PLAN_IDENTITY],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=_REPOSITORY,
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
        )
        for seed in HASH_SEEDS
    }
    observed = {}
    for seed, process in processes.items():
        stdout, stderr = process.communicate(timeout=180)
        assert process.returncode == 0, stderr
        observed[seed] = json.loads(stdout.splitlines()[-1])

    identities = {tuple(result["identity"]) for result in observed.values()}
    assert all(result["status"] == "COMPLETED" for result in observed.values()), observed
    # The roster still carries the sets this guards: email.feedback and email.unsubscribe.
    assert all(
        result["scopes"] == [["email.read", "email.write"]] * 2 for result in observed.values()
    ), observed
    assert len(identities) == 1, observed
    [(prefix_sha256, _schema_sha256)] = identities
    assert all(result["rendered"] == [prefix_sha256] for result in observed.values()), observed
