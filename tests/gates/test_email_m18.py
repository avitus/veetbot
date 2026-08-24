"""Milestone 18 gate checks for the first-party Gmail MCP servers."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from agent_core.domain.policies import (
    ActionKind,
    ExecutionTarget,
    IdempotencyClass,
    PolicyDecisionType,
    ProposedAction,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.policy.engine import DeterministicPolicyEngine
from agent_core.policy.loader import DEFAULT_RULESET
from scripts.architecture_checks import architecture_errors
from tests.contract.support import NOW, RUN_ID, SESSION_ID, principal, run

ROOT = Path(__file__).resolve().parents[2]


def _gmail_action(
    *,
    name: str,
    server_id: str,
    side_effect: SideEffectClass,
    risk: RiskLevel,
    idempotency: IdempotencyClass,
    argument_trust: dict[str, TrustLevel] | None = None,
) -> ProposedAction:
    return ProposedAction(
        kind=ActionKind.TOOL_CALL,
        action_id=UUID(int=181),
        tenant_id="tenant-a",
        session_id=SESSION_ID,
        run_id=RUN_ID,
        step_number=1,
        name=name,
        version="1.0.0",
        summary="A Gmail server tool call.",
        side_effect=side_effect,
        risk=risk,
        idempotency=idempotency,
        arguments={"query": "newer_than:1d"},
        normalized_arguments_hash="hash",
        argument_trust=argument_trust or {},
        origin_trust=TrustLevel.USER,
        target=ExecutionTarget(
            kind="mcp",
            isolated=False,
            network_enabled=False,
            server_id=server_id,
        ),
        evaluated_at=NOW,
    )


async def test_reads_allow_while_writes_and_sends_require_approval() -> None:
    """Gate 5: the default ruleset splits the three servers by their classes."""

    engine = DeterministicPolicyEngine(DEFAULT_RULESET)
    read = await engine.evaluate(
        _gmail_action(
            name="mcp.gmail_read.search_threads",
            server_id="gmail_read",
            side_effect=SideEffectClass.NETWORK_READ,
            risk=RiskLevel.LOW,
            idempotency=IdempotencyClass.READ_ONLY,
        ),
        principal(),
        run(),
    )
    assert read.decision is PolicyDecisionType.ALLOW

    write = await engine.evaluate(
        _gmail_action(
            name="mcp.gmail_write.trash_thread",
            server_id="gmail_write",
            side_effect=SideEffectClass.EXTERNAL_WRITE,
            risk=RiskLevel.MEDIUM,
            idempotency=IdempotencyClass.NON_IDEMPOTENT,
        ),
        principal(),
        run(),
    )
    assert write.decision is PolicyDecisionType.REQUIRE_APPROVAL

    send = await engine.evaluate(
        _gmail_action(
            name="mcp.gmail_send.send_message",
            server_id="gmail_send",
            side_effect=SideEffectClass.EXTERNAL_MESSAGE,
            risk=RiskLevel.HIGH,
            idempotency=IdempotencyClass.NON_IDEMPOTENT,
        ),
        principal(),
        run(),
    )
    assert send.decision is PolicyDecisionType.REQUIRE_APPROVAL

    not_read_only = await engine.evaluate(
        _gmail_action(
            name="mcp.gmail_read.search_threads",
            server_id="gmail_read",
            side_effect=SideEffectClass.NETWORK_READ,
            risk=RiskLevel.LOW,
            idempotency=IdempotencyClass.NON_IDEMPOTENT,
        ),
        principal(),
        run(),
    )
    assert not_read_only.decision is PolicyDecisionType.DENY


async def test_a_send_proposed_from_untrusted_mail_cannot_be_plain_allowed() -> None:
    """Gate 6: the trust overlay outranks even a profile that allows sends."""

    permissive = DEFAULT_RULESET.model_copy(
        update={
            "rules": tuple(
                rule
                if rule.side_effect is not SideEffectClass.EXTERNAL_MESSAGE
                else rule.model_copy(
                    update={"decision": PolicyDecisionType.ALLOW, "condition": None}
                )
                for rule in DEFAULT_RULESET.rules
            )
        }
    )
    engine = DeterministicPolicyEngine(permissive)

    def send(argument_trust: dict[str, TrustLevel]) -> ProposedAction:
        return _gmail_action(
            name="mcp.gmail_send.send_message",
            server_id="gmail_send",
            side_effect=SideEffectClass.EXTERNAL_MESSAGE,
            risk=RiskLevel.HIGH,
            idempotency=IdempotencyClass.NON_IDEMPOTENT,
            argument_trust=argument_trust,
        )

    trusted = await engine.evaluate(send({}), principal(), run())
    assert trusted.decision is PolicyDecisionType.ALLOW

    tainted = await engine.evaluate(
        send({"body": TrustLevel.EXTERNAL_UNTRUSTED}), principal(), run()
    )
    assert tainted.decision is PolicyDecisionType.REQUIRE_APPROVAL


def test_gmail_mcp_two_way_isolation(tmp_path: Path) -> None:
    """Gate 1: `gmail_mcp` and `agent_core` import nothing from each other."""

    assert not [error for error in architecture_errors(ROOT) if "gmail_mcp" in error]

    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")
    fixtures = {
        "src/agent_core/runtime/leak.py": "from gmail_mcp.server import MODES\nVALUE = MODES\n",
        "src/gmail_mcp/bad.py": (
            "from agent_core.domain.policies import TrustLevel\nVALUE = TrustLevel\n"
        ),
    }
    for relative, content in fixtures.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    errors = architecture_errors(tmp_path)
    assert any("agent_core imports the gmail_mcp server package" in error for error in errors), (
        errors
    )
    assert any("gmail_mcp imports the agent_core platform package" in error for error in errors), (
        errors
    )


async def test_all_modes_pass_the_shared_contract_suite() -> None:
    """Gate 2: one shared suite, all three modes, one fake Gmail API.

    The complete behavioral contract runs in the contract partition; this
    gate asserts the mechanism — the suite exists, drives every declared
    mode through one fake, and covers the claims the design names — and
    smokes each mode live against the seeded fake.
    """

    import ast

    from gmail_mcp.server import MODES
    from tests.contract.gmail_support import seeded_fake
    from tests.contract.test_gmail_mcp_contract import ROSTERS, server_for

    assert set(ROSTERS) == set(MODES) == {"read", "send", "write"}
    for mode, roster in ROSTERS.items():
        server = server_for(seeded_fake(), mode)
        assert {tool.name for tool in await server.list_tools()} == roster

    suite = ROOT / "tests" / "contract" / "test_gmail_mcp_contract.py"
    functions = {
        node.name
        for node in ast.walk(ast.parse(suite.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert {
        "test_every_mode_advertises_exactly_its_roster",
        "test_search_threads_returns_normalized_records",
        "test_get_thread_reduces_html_and_never_fetches_attachments",
        "test_upstream_failures_map_to_stable_codes",
        "test_the_access_token_never_appears_in_results_or_errors",
        "test_create_draft_stores_a_draft_by_value",
        "test_modify_labels_batches_over_threads",
        "test_trash_and_untrash_round_trip",
        "test_send_message_sends_by_value",
        "test_write_and_send_failures_are_stable_too",
    } <= functions


async def test_roster_confinement_and_no_permanent_deletion() -> None:
    """Gate 3: each mode serves exactly its roster; deletion is not a verb.

    Beyond the advertised names, the confinement is structural: the package
    speaks no `delete` anywhere — no tool, no client method, no Gmail
    endpoint — so permanent deletion cannot return as an implementation
    detail without failing this gate.
    """

    from gmail_mcp.gmail import GmailClient
    from gmail_mcp.server import MODES
    from tests.contract.gmail_support import seeded_fake
    from tests.contract.test_gmail_mcp_contract import server_for

    for mode, roster in MODES.items():
        server = server_for(seeded_fake(), mode)
        assert tuple(sorted(tool.name for tool in await server.list_tools())) == roster
        assert not any("delete" in name for name in roster)
    assert not [name for name in dir(GmailClient) if "delete" in name.lower()]
    for path in sorted((ROOT / "src" / "gmail_mcp").rglob("*.py")):
        assert "delete" not in path.read_text(encoding="utf-8").lower(), path
