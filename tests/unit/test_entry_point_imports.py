"""What every service pays to start.

Each systemd unit, including the execution service and the calling ingress,
starts through the `agent` entry point, so its module graph is imported at every
start. The provider and MCP SDKs load where the composition root constructs
their adapters, never with the entry point.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from types import MappingProxyType

from pydantic import SecretStr

from agent_core import bootstrap
from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
from agent_core.adapters.models.unavailable import MissingCredentialProvider
from agent_core.config import PACKAGE_ROOT
from agent_core.model.registry import ProviderRegistry
from tests.integration.m2_support import memory_settings

# Together these are more than half the modules the entry point used to load.
CONSTRUCTION_TIME_SDKS = ("anthropic", "mcp", "openai")


def test_the_agent_entry_point_loads_no_provider_or_mcp_sdk() -> None:
    probe = (
        "import json, sys\n"
        "import agent_core.cli.main\n"
        f"print(json.dumps([name for name in {CONSTRUCTION_TIME_SDKS!r} if name in sys.modules]))\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        check=True,
        text=True,
        timeout=120,
    )

    assert json.loads(completed.stdout) == []


def test_a_credentialed_profile_still_builds_its_sdk_adapter() -> None:
    registry = ProviderRegistry.load(
        PACKAGE_ROOT / "models", adapters=ADAPTER_DEFINITIONS, overlay_root=None
    )
    credentialed = replace(
        memory_settings(),
        credentials=MappingProxyType(
            {"openai": SecretStr("synthetic-openai"), "anthropic": SecretStr("synthetic-anthropic")}
        ),
    )

    built = bootstrap._provider_adapters(credentialed, registry)
    missing = bootstrap._provider_adapters(memory_settings(), registry)

    assert type(built["openai"]).__name__ == "OpenAIResponsesProvider"
    assert type(built["anthropic"]).__name__ == "AnthropicMessagesProvider"
    assert isinstance(missing["openai"], MissingCredentialProvider)
    assert isinstance(missing["anthropic"], MissingCredentialProvider)
