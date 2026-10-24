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


def test_the_github_app_settings_default_to_absent_on_the_widget_branch() -> None:
    settings = load_settings(base_environment())
    assert settings.cockpit_github_repository is None
    assert settings.cockpit_github_branch == "cockpit/widgets"
    assert settings.cockpit_github_app_id is None
    assert settings.cockpit_github_installation_id is None
    assert settings.cockpit_github_key_file is None


def test_the_github_app_settings_are_read_from_the_environment() -> None:
    settings = load_settings(
        {
            **base_environment(),
            "AGENT_COCKPIT_GITHUB_REPOSITORY": "avitus/veetbot",
            "AGENT_COCKPIT_GITHUB_BRANCH": "cockpit/widgets-staging",
            "AGENT_COCKPIT_GITHUB_APP_ID": "123456",
            "AGENT_COCKPIT_GITHUB_INSTALLATION_ID": "7890",
            "AGENT_COCKPIT_GITHUB_KEY_FILE": "/run/credentials/veetbot-maintenance.service/x",
        }
    )
    assert settings.cockpit_github_repository == "avitus/veetbot"
    assert settings.cockpit_github_branch == "cockpit/widgets-staging"
    assert (settings.cockpit_github_app_id, settings.cockpit_github_installation_id) == (
        123456,
        7890,
    )
    assert settings.cockpit_github_key_file == Path(
        "/run/credentials/veetbot-maintenance.service/x"
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AGENT_COCKPIT_GITHUB_REPOSITORY", "veetbot"),
        ("AGENT_COCKPIT_GITHUB_REPOSITORY", "avitus/veetbot/extra"),
        ("AGENT_COCKPIT_GITHUB_BRANCH", "main"),
        ("AGENT_COCKPIT_GITHUB_BRANCH", "cockpit/../main"),
        ("AGENT_COCKPIT_GITHUB_APP_ID", "0"),
        ("AGENT_COCKPIT_GITHUB_APP_ID", "twelve"),
        ("AGENT_COCKPIT_GITHUB_INSTALLATION_ID", "-4"),
        ("AGENT_COCKPIT_GITHUB_KEY_FILE", "relative/key.pem"),
    ],
)
def test_a_malformed_github_app_setting_is_a_configuration_error(name: str, value: str) -> None:
    with pytest.raises(ConfigurationError, match=name):
        load_settings({**base_environment(), name: value})


def test_both_env_examples_document_the_github_app_settings() -> None:
    for relative in (".env.example", "deploy/veetbot.env.example"):
        lines = (ROOT / relative).read_text(encoding="utf-8").splitlines()
        for name in (
            "AGENT_COCKPIT_GITHUB_REPOSITORY",
            "AGENT_COCKPIT_GITHUB_APP_ID",
            "AGENT_COCKPIT_GITHUB_INSTALLATION_ID",
        ):
            assert any(line.startswith(f"{name}=") for line in lines), (relative, name)


def test_the_shipped_cockpit_profiles_match_the_defaults() -> None:
    from agent_core.cockpit.profiles import CockpitProfiles
    from agent_core.config import load_config_document
    from tests.integration.m2_support import memory_settings

    document = load_config_document(memory_settings(), "cockpit/profiles.yaml")
    assert CockpitProfiles.from_document(document) == CockpitProfiles()
    publication = CockpitProfiles().publication
    assert (
        publication.commits_per_widget_per_hour,
        publication.commits_per_principal_per_hour,
        publication.import_interval_seconds,
    ) == (12, 60, 60)


def test_an_invalid_cockpit_profile_names_the_file() -> None:
    from agent_core.cockpit.profiles import CockpitProfiles

    with pytest.raises(ConfigurationError, match=r"cockpit/profiles\.yaml"):
        CockpitProfiles.from_document({"schema_version": 1, "publication": {"x": 1}})
    with pytest.raises(ConfigurationError, match="commits_per_principal_per_hour"):
        CockpitProfiles.from_document(
            {
                "schema_version": 1,
                "publication": {
                    "commits_per_widget_per_hour": 20,
                    "commits_per_principal_per_hour": 10,
                },
            }
        )


def test_only_the_maintenance_unit_receives_the_github_app_key() -> None:
    units = ROOT / "deploy/systemd"
    maintenance = (units / "veetbot-maintenance.service").read_text(encoding="utf-8")
    assert (
        "LoadCredential=cockpit-github-app:/etc/veetbot/secrets/cockpit-github-app.pem"
        in maintenance.splitlines()
    )
    # An empty default keeps the unit starting before the owner installs the App.
    assert "SetCredential=cockpit-github-app:" in maintenance.splitlines()
    assert (
        "Environment=AGENT_COCKPIT_GITHUB_KEY_FILE=%d/cockpit-github-app"
        in maintenance.splitlines()
    )
    for unit in units.glob("*.service"):
        if unit.name != "veetbot-maintenance.service":
            text = unit.read_text(encoding="utf-8")
            assert "cockpit-github-app" not in text, unit.name
            assert "AGENT_COCKPIT_GITHUB_KEY_FILE" not in text, unit.name
