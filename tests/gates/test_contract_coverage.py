"""Every port Protocol must acquire a shared contract module."""

from pathlib import Path

from scripts.architecture_checks import contract_coverage_errors

ROOT = Path(__file__).resolve().parents[2]


def test_contract_coverage() -> None:
    assert contract_coverage_errors(ROOT) == []


def test_protocol_without_contract_is_rejected(tmp_path: Path) -> None:
    port = tmp_path / "src" / "agent_core" / "ports" / "models.py"
    port.parent.mkdir(parents=True)
    port.write_text(
        "from typing import Protocol\nclass ModelProvider(Protocol):\n    pass\n",
        encoding="utf-8",
    )
    assert contract_coverage_errors(tmp_path) == [
        "src/agent_core/ports/models.py:2: ModelProvider has no "
        "tests/contract/test_model_provider_contract.py"
    ]
