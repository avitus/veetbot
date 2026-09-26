"""A tool's scope set cannot move the frozen prefix identity.

`ToolSpec.required_scopes` is a set, and a set iterates in an order that
depends on the process's string hash seed and on how the set was built. Every
`model_copy(deep=True)` and every read of a plan event rebuilds it. The prefix
hash and the tool schema hash are taken over JSON dumps of the specs, so an
iteration-order dump let a restarted worker rotate a chat's plan, or fail its
run with "the frozen context prefix no longer matches its plan" when the
planner and the builder saw different rebuilds.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from agent_core.adapters.determinism import FixedClock
from agent_core.domain.tools import ToolSpec
from agent_core.tools.current_time import CurrentTimeTool
from tests.contract.support import NOW

SCOPES = {"email.read", "email.write"}
# Under CPython 3.12 these seeds cover each way the pair above iterates: 1 in
# sorted order, 5 reversed, 4 in insertion order, and 3 and 8 flipping on
# every rebuild. Any seed must give the same bytes.
HASH_SEEDS = (1, 3, 4, 5, 8)
_REPOSITORY = Path(__file__).resolve().parents[2]
_IDENTITIES = """
import hashlib
import json

from agent_core.adapters.determinism import FixedClock
from agent_core.context.estimator import canonical_json_bytes
from agent_core.context.rendering import build_prefix, prefix_bytes
from agent_core.domain.tools import ToolSpec
from agent_core.tools.current_time import CurrentTimeTool
from tests.contract.support import NOW, agent

base = CurrentTimeTool(FixedClock(NOW)).spec.model_dump()
registered = ToolSpec.model_validate({**base, "required_scopes": {"email.read", "email.write"}})


def identities(spec):
    digest = lambda encoded: hashlib.sha256(encoded).hexdigest()
    return (
        digest(prefix_bytes(build_prefix(agent(), [spec]), [spec])),
        digest(prefix_bytes(build_prefix(agent(), [], deferred_tools=[spec]), [], [spec])),
        digest(canonical_json_bytes([spec.model_dump(mode="json")])),
    )


variants = {"registered": registered}
copied = reloaded = registered
for count in range(1, 5):
    copied = copied.model_copy(deep=True)
    reloaded = ToolSpec.model_validate(reloaded.model_dump(mode="json"))
    variants[f"copied {count}"] = copied
    variants[f"reloaded {count}"] = reloaded
print(json.dumps({name: identities(spec) for name, spec in variants.items()}))
"""


def test_prefix_and_schema_identities_ignore_scope_set_order() -> None:
    processes = {
        seed: subprocess.Popen(
            [sys.executable, "-c", _IDENTITIES],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=_REPOSITORY,
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
        )
        for seed in HASH_SEEDS
    }
    variants_by_identity: dict[tuple[str, ...], list[str]] = {}
    for seed, process in processes.items():
        stdout, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, stderr
        for variant, digests in json.loads(stdout).items():
            variants_by_identity.setdefault(tuple(digests), []).append(f"seed {seed} {variant}")

    assert len(variants_by_identity) == 1, list(variants_by_identity.values())


def test_scopes_dump_sorted_to_json_and_stay_a_set_in_python() -> None:
    base = CurrentTimeTool(FixedClock(NOW)).spec.model_dump()
    spec = ToolSpec.model_validate({**base, "required_scopes": {"email.write", "email.read"}})

    assert spec.model_dump(mode="json")["required_scopes"] == ["email.read", "email.write"]
    assert json.loads(spec.model_dump_json())["required_scopes"] == ["email.read", "email.write"]
    assert spec.model_dump()["required_scopes"] == SCOPES
    assert spec.model_copy(deep=True).required_scopes == SCOPES
    assert ToolSpec.model_validate(spec.model_dump(mode="json")) == spec
