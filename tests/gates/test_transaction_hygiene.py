"""The Milestone 0 transaction-hygiene checker over the current source tree."""

from pathlib import Path

from scripts.architecture_checks import transaction_hygiene_errors

ROOT = Path(__file__).resolve().parents[2]


def test_transaction_hygiene() -> None:
    assert transaction_hygiene_errors(ROOT) == []


def test_transaction_check_distinguishes_external_io_from_database_io(tmp_path: Path) -> None:
    module = tmp_path / "src" / "agent_core" / "runtime" / "unsafe.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "async def run(session, model):\n"
        "    async with session.begin():\n"
        "        await session.execute('select 1')\n"
        "        await model.complete()\n",
        encoding="utf-8",
    )

    errors = transaction_hygiene_errors(tmp_path)
    assert len(errors) == 1
    assert "model.complete" in errors[0]
