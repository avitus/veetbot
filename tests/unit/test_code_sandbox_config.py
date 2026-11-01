"""ADR-0281: the code sandbox image setting, beside the Cockpit builder's."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.config import ConfigurationError, load_settings
from tests.unit.test_config import base_environment

ROOT = Path(__file__).resolve().parents[2]


def test_the_code_sandbox_image_defaults_to_the_development_tag() -> None:
    assert load_settings(base_environment()).code_sandbox_image == "veetbot-code-sandbox:dev"


def test_the_code_sandbox_image_is_read_from_the_environment() -> None:
    settings = load_settings(
        {**base_environment(), "AGENT_CODE_SANDBOX_IMAGE": "veetbot-code-sandbox:production"}
    )
    assert settings.code_sandbox_image == "veetbot-code-sandbox:production"


def test_an_empty_code_sandbox_image_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="AGENT_CODE_SANDBOX_IMAGE"):
        load_settings({**base_environment(), "AGENT_CODE_SANDBOX_IMAGE": " "})


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        (".env.example", "AGENT_CODE_SANDBOX_IMAGE=veetbot-code-sandbox:dev"),
        ("deploy/veetbot.env.example", "AGENT_CODE_SANDBOX_IMAGE=veetbot-code-sandbox:production"),
    ],
)
def test_the_environment_templates_name_the_image(relative: str, expected: str) -> None:
    assert expected in (ROOT / relative).read_text(encoding="utf-8").splitlines()


def test_the_image_holds_named_source_and_never_the_whole_build_context() -> None:
    """A release's build context also holds `.release.env` and the virtual environment."""
    lines = (ROOT / "execution/code-sandbox.Dockerfile").read_text(encoding="utf-8").splitlines()
    copied = [
        line.split()[1:-1] for line in lines if line.startswith("COPY") and "--from" not in line
    ]
    assert copied
    for sources in copied:
        assert not {".", "./", "*"} & set(sources), sources
        assert not any(source == ".venv" or source.endswith(".env") for source in sources)
    assert "FROM python:3.12-slim-bookworm@sha256:" in "\n".join(lines)
    assert "FROM node:22.23.1-bookworm-slim@sha256:" in "\n".join(lines)
    assert "USER 65534:65534" in lines
