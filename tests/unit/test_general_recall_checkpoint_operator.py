"""The segment coordinator does not construct paid adapters before every guard."""

from pathlib import Path
from typing import Any

import pytest

from agent_core.evals import general_recall_checkpoint_runner as runner
from agent_core.evals.general_recall_checkpoint import _digest, _read
from tests.unit.test_general_recall_checkpoint_runner import inputs
from tests.unit.test_general_recall_continuation_budget import parents

pytestmark = pytest.mark.static


def operator_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], list[str]]:
    arguments, _, constructions = inputs(tmp_path, monkeypatch)
    budget = arguments.pop("budget")
    budget.seal()
    # The fixture opened an empty segment while testing the underlying bridge.
    # Start this independent orchestration fixture from its untouched imported state.
    for path in (budget.store.directory / "budget").iterdir():
        path.unlink()
    (budget.store.directory / "budget").rmdir()
    arguments.pop("deadline")
    prepared = arguments.pop("prepared")
    order: list[str] = []

    async def preflight(*_args: Any) -> Any:
        order.append("preflight")
        assert not constructions
        return prepared

    async def external(*_args: Any) -> None:
        order.append("external")
        assert not constructions

    async def collect(store: Any, **kwargs: Any) -> dict[str, Any]:
        order.append("collect")
        assert not constructions
        assert (store.directory / "deadline.json").exists()
        assert (store.directory / "operator-segments/000001.json").exists()
        assert len(store.completed()) == 4
        key = store.pending()[0]
        kwargs["admit"](key)
        digest = kwargs["seal_ledger"]()
        assert len(digest) == 64
        return {"state": "synthetic_boundary", "completed_slots": 4}

    monkeypatch.setattr(runner, "prepare_confirmation", preflight, raising=False)
    monkeypatch.setattr(runner, "verify_external_inputs", external, raising=False)
    monkeypatch.setattr(runner, "collect_segment", collect, raising=False)
    arguments.update(
        store=budget.store,
        parents=parents(),
        settings=prepared.settings,
        scratch=prepared.settings,
        protocol_sha256="a" * 64,
        create_deadline=True,
    )
    return arguments, order


async def test_coordinator_pins_originals_deadline_and_workers_before_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, order = operator_fixture(tmp_path, monkeypatch)
    try:
        before = args["parent"].archive.read("identity.json")
        result = await runner.run_checkpoint_segment(**args)
        assert result["state"] == "synthetic_boundary"
        assert order == ["preflight", "external", "collect"]
        assert args["parent"].archive.read("identity.json") == before
        assert (args["store"].directory / "budget/000001.sealed.json").exists()
        import json

        admission = json.loads(_read(args["store"].directory / "operator-segments/000001.json"))
        assert admission["plan_sha256"] == _digest(args["store"].plan.encoded())
        assert admission["protocol_sha256"] == "a" * 64
        assert len(set(admission["databases"])) == 2
    finally:
        args["store"].close()


@pytest.mark.parametrize(
    "change", ["protocol", "parent", "budget", "external", "preflight", "deadline", "unknown_slot"]
)
async def test_coordinator_refuses_before_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    args, order = operator_fixture(tmp_path, monkeypatch)

    def refuse(*_args: Any) -> None:
        raise ValueError("Synthetic admission refusal")

    async def async_refuse(*_args: Any) -> None:
        raise ValueError("Synthetic admission refusal")

    if change == "protocol":
        args["verify_protocol"] = refuse
    elif change == "parent":
        args["parent"].archive.directory.joinpath("identity.json").write_bytes(b"Changed")
    elif change == "budget":
        args["parents"] = parents()[:-1]
    elif change in {"external", "preflight"}:
        monkeypatch.setattr(
            runner,
            "verify_external_inputs" if change == "external" else "prepare_confirmation",
            async_refuse,
        )
    elif change == "deadline":
        args["create_deadline"] = False
    else:
        args["store"].begin(args["store"].pending()[0])
    try:
        with pytest.raises((ValueError, OSError)):
            await runner.run_checkpoint_segment(**args)
        assert "collect" not in order
    finally:
        args["store"].close()


async def test_next_segment_cannot_reuse_workers_or_renew_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, order = operator_fixture(tmp_path, monkeypatch)
    try:
        await runner.run_checkpoint_segment(**args)
        before = _read(args["store"].directory / "deadline.json")
        args["create_deadline"] = False
        with pytest.raises(ValueError, match=r"worker|database"):
            await runner.run_checkpoint_segment(**args)
        assert order.count("collect") == 1
        assert _read(args["store"].directory / "deadline.json") == before
        args["create_deadline"] = True
        with pytest.raises((ValueError, FileExistsError)):
            await runner.run_checkpoint_segment(**args)
        assert _read(args["store"].directory / "deadline.json") == before
    finally:
        args["store"].close()
