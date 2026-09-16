"""Bland calling boundary tests; no test dials a telephone number."""

import json
from typing import Any

import httpx
import pytest
from mcp.types import CallToolResult

from agent_core.domain.calls import CallConfiguration
from bland_mcp.client import BlandClient
from bland_mcp.server import create_server
from tests.contract.test_bland_client_contract import CALL_ID, KEY, NUMBER, RECIPIENT


def call_configuration() -> CallConfiguration:
    return CallConfiguration(
        account_id="primary",
        phone_number=NUMBER,
        public_name="Example Owner",
        public_profile="Take messages. The office is open Monday to Friday.",
        webhook_url="https://api.example.test/webhooks/bland",
    )


def test_receptionist_payload_contains_only_reviewed_public_profile() -> None:
    from scripts.prepare_bland_profile import receptionist_payload

    config = call_configuration()
    payload = receptionist_payload(config)
    assert config.public_profile in payload["prompt"]
    assert config.public_name in payload["first_sentence"]
    assert "AI assistant" in payload["first_sentence"]
    assert "transcribed" in payload["first_sentence"]
    assert payload["webhook"] == config.webhook_url
    assert payload["max_duration"] == 5 and payload["record"] is False
    assert payload["tools"] == [] and payload["transfer_list"] == {}
    assert payload["pathway_id"] is None and payload["dynamic_data"] is None
    assert payload["memory_id"] is None and payload["request_data"] == {}
    assert payload["transfer_phone_number"] is None and payload["fallback_number"] is None


def test_named_assistant_inbound_introduction_and_configuration() -> None:
    from scripts.prepare_bland_profile import receptionist_payload

    original = call_configuration()
    assert original.assistant_name == "Veetbot"
    config = CallConfiguration.model_validate(
        {
            **original.model_dump(),
            "assistant_name": "Willow",
            "public_name": "Andy",
            "voice": "Willow",
        }
    )
    payload = receptionist_payload(config)
    assert payload["first_sentence"] == (
        "Hi, I'm Willow, Andy's assistant. I'm an AI assistant, and this call is "
        "transcribed and shared with Andy. How can I help?"
    )
    assert "Willow" in payload["prompt"]
    assert payload["voice"] == "Willow"
    assert payload["metadata"]["veetbot_configuration_revision"] == config.revision
    renamed = CallConfiguration.model_validate({**config.model_dump(), "assistant_name": "Rowan"})
    assert renamed.revision != config.revision
    for invalid in ("", " ", "x" * 129):
        with pytest.raises(ValueError):
            CallConfiguration.model_validate({**config.model_dump(), "assistant_name": invalid})


async def test_named_assistant_outbound_identity_and_approval_revision() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "success", "call_id": CALL_ID})

    config = CallConfiguration.model_validate(
        {
            **call_configuration().model_dump(),
            "assistant_name": "Willow",
            "public_name": "Andy",
            "voice": "Willow",
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = BlandClient(KEY, NUMBER, http_client=http)
        server = create_server("call", client, config.model_dump())
        args = {
            "phone_number": RECIPIENT,
            "from_number": NUMBER,
            "brief": "Ask whether the repair is ready.",
            "disclosed_facts": "Ticket 12.",
            "config_revision": config.revision,
            "request_id": CALL_ID,
        }
        result = await server.call_tool("start_call", args)
        assert isinstance(result, CallToolResult) and not result.is_error
        payload = json.loads(requests[-1].content)
        assert "Hi, I'm Willow, Andy's assistant." in payload["task"]
        assert "AI assistant" in payload["task"] and "transcribed" in payload["task"]
        assert payload["voice"] == "Willow"
        assert config.public_profile not in payload["task"]

        renamed = CallConfiguration.model_validate(
            {**config.model_dump(), "assistant_name": "Rowan"}
        )
        server = create_server("call", client, renamed.model_dump())
        rejected = await server.call_tool("start_call", args)
        assert isinstance(rejected, CallToolResult) and rejected.is_error
        assert len(requests) == 1


async def test_call_rosters_and_approved_configuration() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "success", "call_id": CALL_ID})

    config = call_configuration()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = BlandClient(KEY, NUMBER, http_client=http)
        read = create_server("read", client, config.model_dump())
        call = create_server("call", client, config.model_dump())
        assert {tool.name for tool in await read.list_tools()} == {
            "list_calls",
            "get_call",
            "provider_get_call",
            "provider_list_calls",
        }
        assert {tool.name for tool in await call.list_tools()} == {
            "start_call",
            "provider_stop_call",
        }
        catalog = {tool.name: tool for tool in await call.list_tools()}
        assert catalog["provider_stop_call"].meta == {"veetbot/application-only": True}
        assert config.revision in (catalog["start_call"].description or "")
        args: dict[str, Any] = {
            "phone_number": RECIPIENT,
            "from_number": NUMBER,
            "brief": "Ask whether the repair is ready.",
            "disclosed_facts": "Ticket 12.",
            "config_revision": config.revision,
            "request_id": CALL_ID,
            "max_duration_minutes": 5,
            "record": False,
        }
        result = await call.call_tool("start_call", args)
        assert isinstance(result, CallToolResult)
        assert not result.is_error
        payload = json.loads(requests[-1].content)
        assert payload["phone_number"] == RECIPIENT and payload["from"] == NUMBER
        assert payload["record"] is False and payload["tools"] == []
        assert payload["transfer_list"] == {}
        assert payload["max_duration"] == 5
        assert "Ticket 12" in payload["task"]
        assert config.public_profile not in payload["task"]
        args["config_revision"] = "0" * 64
        rejected = await call.call_tool("start_call", args)
        assert isinstance(rejected, CallToolResult)
        assert rejected.is_error
        assert len(requests) == 1


@pytest.mark.parametrize(
    "change", [{"record": True}, {"tools": ["email"]}, {"max_duration_minutes": 6}]
)
def test_public_configuration_has_no_private_capabilities(change: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        CallConfiguration.model_validate({**call_configuration().model_dump(), **change})


def test_call_configuration_default_off_and_private_credentials(tmp_path: Any) -> None:
    from agent_core.config import ConfigurationError, load_settings
    from tests.unit.test_config import base_environment

    assert getattr(load_settings(base_environment()), "call_enabled", None) is False
    config_file = tmp_path / "calls.json"
    config_file.write_text(call_configuration().model_dump_json())
    key_file = tmp_path / "bland-key"
    key_file.write_text(KEY)
    key_file.chmod(0o600)
    values = {
        **base_environment(),
        "AGENT_CALL_ENABLED": "1",
        "BLAND_CONFIGURATION_FILE": str(config_file),
        "BLAND_API_KEY_FILE": str(key_file),
    }
    settings = load_settings(values)
    assert settings.call_configuration == call_configuration()
    assert set(settings.credentials) == {"bland_read", "bland_call"}
    assert KEY not in repr(settings)
    key_file.chmod(0o644)
    with pytest.raises(ConfigurationError):
        load_settings(values)
    with pytest.raises(ConfigurationError):
        load_settings({**base_environment(), "AGENT_CALL_ENABLED": "1"})


def test_call_server_composition_and_approval_floor() -> None:
    import agent_core.mcp.configuration as configuration
    from agent_core.domain.policies import IdempotencyClass, SideEffectClass

    factory = getattr(configuration, "calling_server_configs", None)
    assert callable(factory), "calling server composition must be implemented"
    assert factory("tenant", enabled=False) == ()
    read, call = factory("tenant", enabled=True)
    assert read.server_id == "bland_read" and call.server_id == "bland_call"
    assert read.side_effect is SideEffectClass.NETWORK_READ
    assert call.side_effect is SideEffectClass.EXTERNAL_MESSAGE
    assert call.idempotency is IdempotencyClass.NON_IDEMPOTENT
    assert read.required_scopes == {"mcp.bland_read.use"}
    assert call.required_scopes == {"mcp.bland_call.use"}


def test_call_cannot_be_autoapproved_by_a_permissive_policy_profile() -> None:
    from agent_core.domain.policies import IdempotencyClass, PolicyDecisionType, SideEffectClass
    from agent_core.policy.engine import evaluate_deterministic
    from tests.gates.test_email_m18 import _action, _principal, _ruleset, _run

    ruleset = _ruleset()
    ruleset = ruleset.model_copy(
        update={
            "rules": tuple(
                rule.model_copy(update={"decision": PolicyDecisionType.ALLOW})
                if rule.side_effect is SideEffectClass.EXTERNAL_MESSAGE
                else rule
                for rule in ruleset.rules
            )
        }
    )
    scopes = {"mcp.bland_call.use"}
    decision = evaluate_deterministic(
        _action(
            server_id="bland_call",
            side_effect=SideEffectClass.EXTERNAL_MESSAGE,
            idempotency=IdempotencyClass.NON_IDEMPOTENT,
        ),
        _principal().model_copy(update={"scopes": scopes}),
        _run().model_copy(update={"principal_scopes": scopes}),
        ruleset,
    )
    assert decision.decision is PolicyDecisionType.REQUIRE_APPROVAL


def test_call_roles_confine_provider_and_signing_credentials(tmp_path: Any) -> None:
    from agent_core.config import load_call_worker_settings
    from tests.unit.test_config import base_environment

    configuration = tmp_path / "calls.json"
    configuration.write_text(call_configuration().model_dump_json())
    key = tmp_path / "api-key"
    key.write_text(KEY)
    key.chmod(0o600)
    signing = tmp_path / "signing-key"
    signing.write_text("fixture-signing-secret")
    signing.chmod(0o600)
    environment = {
        **base_environment(),
        "AGENT_CALL_ENABLED": "1",
        "AGENT_CALL_INGRESS_ENABLED": "1",
        "BLAND_CONFIGURATION_FILE": str(configuration),
        "BLAND_API_KEY_FILE": str(key),
        "BLAND_WEBHOOK_SECRET_FILE": str(signing),
        "OPENAI_API_KEY": "unrelated-model-secret",
    }
    ingress = load_call_worker_settings(environment, ingress=True)
    worker = load_call_worker_settings(environment)
    assert ingress.credentials == {} and ingress.call_webhook_secret is not None
    assert set(worker.credentials) == {"bland_read", "bland_call"}
    assert worker.call_webhook_secret is None
    assert ingress.auth_token is None and worker.auth_token is None


def test_private_key_bootstrap_uses_a_hidden_prompt_and_never_overwrites(
    tmp_path: Any, monkeypatch: Any, capsys: Any
) -> None:
    import getpass

    from bland_mcp.__main__ import main

    path = tmp_path / "bland-api-key"
    monkeypatch.setattr(getpass, "getpass", lambda prompt: KEY)
    main(["bootstrap", "--output-file", str(path)])
    assert path.read_text() == KEY + "\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert KEY not in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["bootstrap", "--output-file", str(path)])
    assert path.read_text() == KEY + "\n"
