"""Persist the reader actually selected by the runtime, including its effort."""

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from tests.contract.support import NOW
from tests.gates.test_runtime_tool_budget import _settings


async def test_checkpoint_records_the_actual_reader_configuration() -> None:
    async with build(
        settings=_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(text="Done")]),
        clock=FixedClock(NOW),
    ) as app:
        run_id = await app.runs.submit("Suggest breakfast options")
        await app.runs.wait_terminal(run_id)
        async with app.uow_factory() as uow:
            saved = await uow.checkpoints.latest(run_id)
        assert saved is not None
        marker = saved.budget_state.get("context_reader_sha256")
        assert isinstance(marker, str) and len(marker) == 64


def test_reader_identity_tracks_effective_settings_but_not_clock_or_credential_name() -> None:
    from datetime import timedelta
    from decimal import Decimal

    from agent_core.domain.messages import ModelPricing, ReasoningEffort, ResolvedModel
    from agent_core.domain.semantic_reader import reader_identity

    model = ResolvedModel(provider="test", model="reader", resolved_at=NOW)

    def identity(
        selected: ResolvedModel = model,
        *,
        effort: ReasoningEffort = ReasoningEffort.HIGH,
        summary: bool = True,
        registry: str = "registry@1",
    ) -> str:
        return reader_identity(
            selected,
            reasoning_effort=effort,
            reasoning_summary=summary,
            registry_version=registry,
        )

    original = identity()
    assert identity(model.model_copy(update={"resolved_at": NOW + timedelta(days=1)})) == original
    assert identity(model.model_copy(update={"credential_ref": "rotated-name"})) == original
    assert identity(model.model_copy(update={"model": "different-reader"})) != original
    assert identity(effort=ReasoningEffort.LOW) != original
    assert identity(summary=False) != original
    assert identity(registry="registry@2") != original
    assert (
        identity(model.model_copy(update={"pricing": ModelPricing(input_per_mtok=Decimal("2"))}))
        != original
    )
