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


async def test_registered_specifications_carry_the_declared_classification() -> None:
    """Gate 4: the real servers, discovered live, map to the declared specs."""

    import json

    from agent_core.adapters.mcp.sdk import SDKMCPClientFactory
    from agent_core.domain.credentials import SecretValue
    from agent_core.domain.tools import ToolSource
    from agent_core.mcp.configuration import build_stdio_environment
    from agent_core.mcp.email import email_server_configs
    from agent_core.mcp.mapping import map_discovered_tools
    from gmail_mcp.server import MODES

    credential = SecretValue(
        json.dumps(
            {
                "client_id": "client-id",
                "client_secret": "client-secret",
                "refresh_token": "refresh-token",
                "scope": "https://www.googleapis.com/auth/gmail.readonly",
            },
            separators=(",", ":"),
        )
    )
    for config in email_server_configs("local"):
        environment = build_stdio_environment(config, credential)
        client = SDKMCPClientFactory()(config, credential, environment)
        async with client:
            discovery = await client.discover()
        report = map_discovered_tools(config, discovery.tools)
        assert not report.rejected and not report.conflicts
        names = tuple(sorted(mapped.remote_name for mapped in report.accepted))
        assert names == MODES[config.server_id.removeprefix("gmail_")]
        for mapped in report.accepted:
            spec = mapped.spec
            assert spec.side_effect is config.side_effect
            assert spec.risk is config.risk
            assert spec.idempotency is config.idempotency
            assert spec.output_trust is TrustLevel.EXTERNAL_UNTRUSTED
            assert spec.source is ToolSource.MCP
            assert spec.required_scopes == {f"mcp.{config.server_id}.use"}


def test_the_credential_reaches_the_child_only_as_the_declared_variable() -> None:
    """Gate 7: over generated credentials, the constructed environment holds
    the value under exactly the one declared name — never argv, never an
    inherited variable."""

    from hypothesis import given
    from hypothesis import strategies as st

    from agent_core.domain.credentials import SecretValue
    from agent_core.mcp.configuration import build_stdio_environment
    from agent_core.mcp.email import EMAIL_CREDENTIAL_VARIABLE, email_server_configs

    @given(
        credential=st.text(
            alphabet=st.characters(min_codepoint=33, max_codepoint=126),
            min_size=32,
            max_size=64,
        )
    )
    def confined(credential: str) -> None:
        for config in email_server_configs("local"):
            environment = build_stdio_environment(config, SecretValue(credential))
            assert environment[EMAIL_CREDENTIAL_VARIABLE] == credential
            assert credential not in config.endpoint
            carriers = [name for name, value in environment.items() if credential in value]
            assert carriers == [EMAIL_CREDENTIAL_VARIABLE]

    confined()


async def test_token_confinement_crosses_only_stable_codes() -> None:
    """Gate 8: failures cross the pipe as codes; tokens and raw upstream
    error text never do; normalized mailbox content is the only upstream
    content that crosses at all."""

    from tests.contract import test_gmail_mcp_contract as contract

    await contract.test_the_access_token_never_appears_in_results_or_errors()
    for status, code in (
        (401, "gmail.credential_rejected"),
        (403, "gmail.rejected"),
        (429, "gmail.rate_limited"),
        (500, "gmail.unavailable"),
    ):
        await contract.test_upstream_failures_map_to_stable_codes(status, code)
    await contract.test_undecodable_upstream_output_is_invalid_output()
    await contract.test_redirects_are_refused_not_followed()
    await contract.test_search_threads_returns_normalized_records()


async def test_default_off_composes_no_gmail_row_or_scope(tmp_path: Path) -> None:
    """Gate 9: with the flag unset there is no row, no scope, no tool."""

    from tests.unit import test_email_composition as composition

    await composition.test_email_default_off_composes_nothing(tmp_path)


async def test_bootstrap_consent_writes_round_tripping_owner_only_files(
    tmp_path: Path,
) -> None:
    """Gate 11: three consents, three 0600 files, exact scopes, no token."""

    from tests.unit import test_gmail_mcp_bootstrap as ceremony

    ceremony.test_the_ceremony_writes_three_owner_only_round_tripping_files(tmp_path / "a")
    ceremony.test_each_consent_requests_exactly_its_server_scope(tmp_path / "b")
    ceremony.test_the_ceremony_prints_paths_and_scopes_but_never_token_material(tmp_path / "c")
    ceremony.test_the_files_round_trip_through_the_settings_loader(tmp_path / "d")
    ceremony.test_a_state_mismatch_aborts_without_writing(tmp_path / "e")


async def test_failure_taxonomy_is_stable_and_bounded() -> None:
    """Gate 12: stable retryable codes, refused redirects, bounded bodies,
    a bounded re-authentication ladder, and a server that will not serve a
    credential it cannot parse."""

    import json

    from agent_core.adapters.mcp.sdk import SDKMCPClientFactory
    from agent_core.domain.credentials import SecretValue
    from agent_core.domain.errors import MCPTransportError, MCPUnauthorizedError
    from agent_core.mcp.configuration import build_stdio_environment
    from agent_core.mcp.email import email_server_configs
    from tests.contract import test_gmail_mcp_contract as contract
    from tests.gates import test_tool_m8 as tool_gates
    from tests.unit import test_gmail_mcp_credential as credential_suite

    for status, code in (
        (429, "gmail.rate_limited"),
        (500, "gmail.unavailable"),
        (503, "gmail.unavailable"),
        (403, "gmail.rejected"),
    ):
        await contract.test_upstream_failures_map_to_stable_codes(status, code)
    await contract.test_redirects_are_refused_not_followed()
    await contract.test_oversized_message_bodies_truncate_within_the_budget()
    await contract.test_oversized_upstream_responses_are_invalid_output()
    await credential_suite.test_refresh_failures_are_stable_and_content_free(
        400, "gmail.credential_rejected"
    )
    await tool_gates.test_mcp_reauth_bounded()

    unparseable = SecretValue(json.dumps({"not": "a-credential"}, separators=(",", ":")))
    config = email_server_configs("local")[0]
    environment = build_stdio_environment(config, unparseable)
    client = SDKMCPClientFactory()(config, unparseable, environment)
    import pytest

    with pytest.raises((MCPTransportError, MCPUnauthorizedError)):
        async with client:
            await client.discover()


def test_each_server_requires_exactly_its_own_scope() -> None:
    """Gate 10: one scope per server, and platform scopes are rejected."""

    import pytest

    from agent_core.domain.errors import ToolValidationError
    from agent_core.mcp.configuration import validate_mcp_config
    from agent_core.mcp.email import email_server_configs

    configs = email_server_configs("local")
    for config in configs:
        assert config.required_scopes == frozenset({f"mcp.{config.server_id}.use"})
        validate_mcp_config(config, destination_allowed=lambda url: False)

    misdeclared = configs[0].model_copy(update={"required_scopes": frozenset({"session.write"})})
    with pytest.raises(ToolValidationError):
        validate_mcp_config(misdeclared, destination_allowed=lambda url: False)


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
