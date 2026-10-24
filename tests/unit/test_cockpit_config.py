"""Cockpit composition settings (docs/plan/cockpit.md, migration, configuration, and operations)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.config import ConfigurationError, load_settings
from tests.unit.test_config import base_environment

ROOT = Path(__file__).resolve().parents[2]


def test_cockpit_is_off_and_names_the_development_builder_image_by_default() -> None:
    settings = load_settings(base_environment())
    assert settings.cockpit_enabled is False
    assert settings.cockpit_builder_image == "veetbot-cockpit-builder:dev"


def test_cockpit_settings_are_read_from_the_environment() -> None:
    settings = load_settings(
        {
            **base_environment(),
            "AGENT_COCKPIT_ENABLED": "1",
            "AGENT_COCKPIT_BUILDER_IMAGE": "veetbot-cockpit-builder:production",
        }
    )
    assert settings.cockpit_enabled is True
    assert settings.cockpit_builder_image == "veetbot-cockpit-builder:production"


def test_an_empty_builder_image_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError, match="AGENT_COCKPIT_BUILDER_IMAGE"):
        load_settings({**base_environment(), "AGENT_COCKPIT_BUILDER_IMAGE": " "})


def test_both_env_examples_document_the_cockpit_settings_off() -> None:
    for relative in (".env.example", "deploy/veetbot.env.example"):
        lines = (ROOT / relative).read_text(encoding="utf-8").splitlines()
        assert "AGENT_COCKPIT_ENABLED=0" in lines, relative
        expected = (
            "AGENT_COCKPIT_BUILDER_IMAGE=veetbot-cockpit-builder:production"
            if relative.startswith("deploy/")
            else "AGENT_COCKPIT_BUILDER_IMAGE=veetbot-cockpit-builder:dev"
        )
        assert expected in lines, relative
