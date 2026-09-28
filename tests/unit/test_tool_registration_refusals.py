"""Registry refusals beyond the M1 gate's reserved-domain and schema checks (tool-system.md).

The registry accepts entries from exactly three sources — the build, MCP discovery,
and a registered device's capabilities — and every entry passes the same startup
refusals: name grammar, namespace ownership, identity, limits, and the classification
a target kind requires.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_core.domain.errors import ConflictError, NotFoundError, ToolValidationError
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import (
    IdempotencyClass,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolKind,
    ToolResult,
    ToolSource,
    ToolSpec,
)
from agent_core.tools.registry import StaticToolRegistry, validate_registration


def _spec(name: str = "demo.probe", **changes: Any) -> ToolSpec:
    base = ToolSpec(
        name=name,
        version="1.0.0",
        description="registration probe",
        input_schema={"type": "object", "additionalProperties": False},
        output_schema=None,
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        timeout_seconds=1,
        maximum_output_bytes=1024,
        allow_parallel=False,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )
    return base.model_copy(update=changes)


def _mcp(name: str = "mcp.docs.search", version: str = "1.0.0", **changes: Any) -> ToolSpec:
    return _spec(
        name, **{"version": version, "source": ToolSource.MCP, "server_id": "docs", **changes}
    )


def _device(**changes: Any) -> ToolSpec:
    identity = {"source": ToolSource.DEVICE, "target_kind": "device", "device_id": "device-1"}
    return _spec("device.sms_send", **{**identity, **changes})


class _Tool:
    def __init__(self, spec: ToolSpec, label: str = "tool") -> None:
        self.spec = spec
        self.label = label

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        del arguments, context
        return ToolResult(ok=True, content=[TextPart(text=self.label)], structured={})


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (_spec("Demo.probe"), "invalid tool name"),
        (_spec("probe"), "invalid tool name"),
        (_spec("demo.1probe"), "invalid tool name"),
        (_spec("demo.probe-two"), "invalid tool name"),
        (_spec("demo." + "a" * 92), "invalid tool name"),
        (_spec("weather.today"), "unknown builtin tool domain"),
        (_spec("web.lookup", source=ToolSource.SANDBOX), "builtin-owned"),
        (_mcp(server_id=None), "valid server id"),
        (_mcp(server_id="Docs"), "valid server id"),
        (_mcp("mcp.other.search"), "does not match its server id"),
        (_mcp("mcp.docsearch.x"), "does not match its server id"),
        (_device(device_id=None), "device target"),
        (_spec("external.probe", source=ToolSource.SANDBOX, device_id="d"), "device identifier"),
        (_spec(timeout_seconds=0), "limits must be positive"),
        (_spec(maximum_output_bytes=0), "limits must be positive"),
        (_spec(output_schema={"type": "not-a-type"}), "Draft 2020-12"),
    ],
)
def test_startup_refuses_invalid_identity_and_limits(spec: ToolSpec, message: str) -> None:
    with pytest.raises(ToolValidationError, match=message):
        validate_registration(spec)


def test_the_name_length_bound_is_ninety_six() -> None:
    at_bound = "demo." + "a" * 91

    assert validate_registration(_spec(at_bound)).name == at_bound
    with pytest.raises(ToolValidationError, match="invalid tool name"):
        validate_registration(_spec(at_bound + "a"))


@pytest.mark.parametrize(
    "spec",
    [
        # A control-kind tool must be one of the declared control tools.
        _spec(kind=ToolKind.CONTROL),
        # A declared control tool may not be registered as a capability.
        _spec("conversation.ask_user"),
        # A control tool has no side effect, is idempotent, and runs in process.
        _spec(
            "conversation.ask_user",
            kind=ToolKind.CONTROL,
            side_effect=SideEffectClass.WORKSPACE_WRITE,
        ),
        _spec(
            "conversation.ask_user",
            kind=ToolKind.CONTROL,
            idempotency=IdempotencyClass.NON_IDEMPOTENT,
        ),
        _spec("conversation.ask_user", kind=ToolKind.CONTROL, target_kind="sandbox"),
    ],
)
def test_control_tools_keep_their_classification(spec: ToolSpec) -> None:
    with pytest.raises(ToolValidationError, match="control"):
        validate_registration(spec)


def _web(name: str = "web.search", **changes: Any) -> ToolSpec:
    classification = {
        "target_kind": "web_provider",
        "side_effect": SideEffectClass.NETWORK_READ,
        "output_trust": TrustLevel.EXTERNAL_UNTRUSTED,
    }
    return _spec(name, **{**classification, **changes})


def test_a_web_provider_tool_is_accepted_only_as_an_untrusted_read() -> None:
    assert validate_registration(_web()).target_kind == "web_provider"


@pytest.mark.parametrize(
    "spec",
    [
        _web(side_effect=SideEffectClass.EXTERNAL_WRITE),
        _web(idempotency=IdempotencyClass.IDEMPOTENT),
        _web(output_trust=TrustLevel.INTERNAL_TOOL),
        _web(name="workspace.search"),
        _mcp(target_kind="web_provider", side_effect=SideEffectClass.NETWORK_READ),
    ],
    ids=["write", "not_read_only", "trusted_output", "other_domain", "mcp_source"],
)
def test_the_web_provider_target_refuses_any_other_classification(spec: ToolSpec) -> None:
    with pytest.raises(ToolValidationError, match="web provider target"):
        validate_registration(spec)


@pytest.mark.parametrize(
    "spec",
    [
        _mcp(),
        _device(),
        _spec("external.run", source=ToolSource.SANDBOX),
        _spec("sandbox.run_probe", target_kind="sandbox"),
    ],
    ids=["mcp", "device", "sandbox_source", "sandbox_target"],
)
def test_output_from_outside_the_process_is_forced_untrusted(spec: ToolSpec) -> None:
    assert validate_registration(spec).output_trust is TrustLevel.EXTERNAL_UNTRUSTED


def test_only_mcp_and_device_sources_register_dynamically() -> None:
    registry = StaticToolRegistry()

    for spec in (_spec(), _spec("external.run", source=ToolSource.SANDBOX)):
        with pytest.raises(ToolValidationError, match="dynamically register"):
            registry.register_dynamic(_Tool(spec), tenant_id="tenant-a")
    with pytest.raises(ToolValidationError, match="requires a tenant"):
        registry.register_dynamic(_Tool(_mcp()), tenant_id="")
    with pytest.raises(ToolValidationError, match="tenant scoped"):
        registry.register_dynamic(_Tool(_mcp()), tenant_id="tenant-a", principal_id="principal-a")
    with pytest.raises(ToolValidationError, match="requires a principal"):
        registry.register_dynamic(_Tool(_device()), tenant_id="tenant-a")
    with pytest.raises(NotFoundError):
        registry.get("mcp.docs.search", tenant_id="tenant-a")


def test_static_and_dynamic_names_cannot_shadow_each_other() -> None:
    registry = StaticToolRegistry()
    static = _Tool(_mcp("mcp.docs.static"))
    registry.register(static)
    registry.register_dynamic(_Tool(_mcp()), tenant_id="tenant-a")

    with pytest.raises(ConflictError, match="reserved by a static"):
        registry.register_dynamic(_Tool(_mcp("mcp.docs.static")), tenant_id="tenant-a")
    with pytest.raises(ConflictError, match="duplicate tool name"):
        registry.register(_Tool(_mcp()))
    with pytest.raises(ConflictError, match="duplicate tool name"):
        registry.register(static)
    with pytest.raises(ConflictError, match="duplicate dynamic"):
        registry.register_dynamic(_Tool(_mcp()), tenant_id="tenant-a")


async def test_unregistering_the_latest_dynamic_version_falls_back_to_the_previous() -> None:
    registry = StaticToolRegistry()
    registry.register_dynamic(_Tool(_mcp(version="1.0.0"), "one"), tenant_id="tenant-a")
    registry.register_dynamic(_Tool(_mcp(version="2.0.0"), "two"), tenant_id="tenant-a")

    async def latest() -> str:
        result = await registry.get("mcp.docs.search", tenant_id="tenant-a").execute({}, {})  # type: ignore[arg-type]
        return result.content[0].text  # type: ignore[union-attr]

    assert await latest() == "two"
    registry.unregister_dynamic("mcp.docs.search", "1.0.0", tenant_id="tenant-a")
    assert await latest() == "two"
    registry.register_dynamic(_Tool(_mcp(version="1.0.0"), "one"), tenant_id="tenant-a")
    registry.unregister_dynamic("mcp.docs.search", "2.0.0", tenant_id="tenant-a")
    assert await latest() == "one"
    registry.unregister_dynamic("mcp.docs.search", "1.0.0", tenant_id="tenant-a")
    with pytest.raises(NotFoundError):
        registry.get("mcp.docs.search", tenant_id="tenant-a")


def test_lookup_honours_the_requested_identity() -> None:
    registry = StaticToolRegistry()
    registry.register(_Tool(_spec()))
    registry.register_dynamic(_Tool(_mcp()), tenant_id="tenant-a")

    assert registry.get("demo.probe", source=ToolSource.BUILTIN).spec.name == "demo.probe"
    assert registry.get("mcp.docs.search", tenant_id="tenant-a", server_id="docs")
    with pytest.raises(NotFoundError):
        registry.get("demo.probe", source=ToolSource.MCP)
    with pytest.raises(NotFoundError):
        registry.get("demo.probe", version="9.9.9")
    with pytest.raises(NotFoundError):
        registry.get("mcp.docs.search", tenant_id="tenant-a", server_id="other")
    with pytest.raises(NotFoundError):
        registry.get("mcp.docs.search", tenant_id="tenant-b")
