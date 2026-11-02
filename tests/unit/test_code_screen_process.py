"""ADR-0281: the code screen, run through its adapter against the real Node program."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from agent_core.adapters.cockpit.builder import cockpit_build_sandbox
from agent_core.adapters.code.screen import CODE_SCREEN_ARGV, SandboxCodeScreen
from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.execution.fake import FakeExecutionEnvironment, fake_image_digest
from agent_core.domain.code import CodeFinding
from agent_core.domain.execution import (
    EnvironmentHandle,
    ExecutionCommand,
    ExecutionResult,
    ResourceLimits,
)

ROOT = Path(__file__).resolve().parents[2]
LIMITS = ResourceLimits(1000, 256 * 1024 * 1024, 64, 4 * 1024 * 1024, 1000, 60)
# Assembled so this file holds no secret-shaped literal for the repository scanner.
SYNTHETIC_TOKEN = "gh" + "p_" + "a1B2" * 9


class _RealProgram(FakeExecutionEnvironment):
    """Run the screen where the builder image holds it, here the working tree."""

    async def execute(
        self, environment: EnvironmentHandle, command: ExecutionCommand
    ) -> ExecutionResult:
        assert tuple(command.argv) == CODE_SCREEN_ARGV
        program = str(command.argv[1]).replace("/opt/cockpit", str(ROOT / "cockpit"))
        done = subprocess.run(
            [str(command.argv[0]), program],
            input=command.stdin,
            capture_output=True,
            check=False,
        )
        return ExecutionResult(
            done.returncode, done.stdout, done.stderr, False, False, False, None, (), 1
        )


async def _screen(added: dict[str, str], message: str = "Fix the value") -> tuple[CodeFinding, ...]:
    environment = _RealProgram(FixedClock(datetime(2026, 11, 1, tzinfo=UTC)), SequenceIdFactory())
    manager = cockpit_build_sandbox(environment, image_digest=fake_image_digest(), limits=LIMITS)
    return await SandboxCodeScreen(manager, SequenceIdFactory()).screen(
        tenant_id="owner",
        run_id=UUID(int=284),
        lease_epoch=1,
        added_lines=added,
        commit_message=message,
        names=("Synthetic Private Owner",),
        settings_values=("private-account-value",),
    )


async def test_ordinary_code_that_mentions_tokens_passes() -> None:
    assert (
        await _screen(
            {
                "src/agent_core/tokens.py": (
                    "def count(token: str, api_key: str | None = None) -> int:\n"
                    "    password = token\n"
                    "    return len(password)\n"
                )
            }
        )
        == ()
    )


async def test_every_rule_names_the_file_and_code_and_never_the_value() -> None:
    findings = await _screen(
        {
            "src/secret.py": f"VALUE = '{SYNTHETIC_TOKEN}'\n",
            "src/name.py": "AUTHOR = 'Synthetic Private Owner'\n",
            "src/setting.py": "ACCOUNT = 'private-account-value'\n",
            "src/mail.py": "CONTACT = 'jordan@personal-mail.com'\n",
            "src/phone.py": "PHONE = '+1 (415) 555-0134'\n",
        },
        message="Thanks to Synthetic Private Owner",
    )
    assert {(finding.file, finding.code) for finding in findings} == {
        ("src/secret.py", "screen.secret"),
        ("src/name.py", "screen.name"),
        ("src/setting.py", "screen.setting"),
        ("src/mail.py", "screen.email"),
        ("src/phone.py", "screen.phone"),
        ("commit message", "screen.name"),
    }
    rendered = " ".join(finding.message for finding in findings)
    for value in (
        SYNTHETIC_TOKEN,
        "Synthetic Private Owner",
        "private-account-value",
        "jordan@personal-mail.com",
        "555-0134",
    ):
        assert value not in rendered


async def test_a_file_path_reaches_the_public_repository_so_it_is_screened_too() -> None:
    findings = await _screen({"tests/unit/synthetic private owner/test_x.py": "VALUE = 1\n"})
    assert [finding.code for finding in findings] == ["screen.name"]
