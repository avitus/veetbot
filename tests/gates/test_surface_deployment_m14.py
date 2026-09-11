"""Milestone 14/25 surface-role deployment confinement gates."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_surface_role_has_dedicated_unit_and_secret_minimized_environment() -> None:
    unit = (ROOT / "deploy/systemd/veetbot-surface.service").read_text(encoding="utf-8")
    example = (ROOT / "deploy/veetbot-surface.env.example").read_text(encoding="utf-8")

    assert "agent worker --role surface" in unit
    assert "EnvironmentFile=/etc/veetbot/veetbot-surface.env" in unit
    assert "NoNewPrivileges=true" in unit
    assert "AUTH_TOKEN=" not in example
    assert "OPENAI" not in example
    assert "BROWSER_" not in example
    assert "AGENT_SURFACE_WHATSAPP_TOKEN_FILE=" in example
    assert "AGENT_SURFACE_WHATSAPP_APP_SECRET_FILE=" in example
    assert "AGENT_SURFACE_WHATSAPP_VERIFY_TOKEN_FILE=" in example


def test_whatsapp_webhook_is_the_only_surface_proxy_and_targets_loopback() -> None:
    nginx = (ROOT / "nginx/veetbot.conf").read_text(encoding="utf-8")
    deploy = (ROOT / "deploy/nginx/deploy.sh").read_text(encoding="utf-8")
    cli = (ROOT / "src/agent_core/cli/main.py").read_text(encoding="utf-8")

    assert nginx.count("location = /webhooks/whatsapp") == 1
    assert "proxy_pass http://127.0.0.1:8002;" in nginx
    assert "client_max_body_size 1m;" in nginx
    assert nginx.count("# VEETBOT_WHATSAPP_ROUTE_BEGIN") == 1
    assert nginx.count("# VEETBOT_WHATSAPP_ROUTE_END") == 1
    whatsapp_location = nginx.split("# VEETBOT_WHATSAPP_ROUTE_BEGIN", 1)[1].split(
        "# VEETBOT_WHATSAPP_ROUTE_END", 1
    )[0]
    assert "access_log off;" in whatsapp_location
    assert "access_log=False" in cli
    assert 'AGENT_SURFACE_WHATSAPP_ENABLED"' in deploy
    assert "enabled == 1 || !inside { print }" in deploy


def test_release_validates_and_restarts_surface_role() -> None:
    release = (ROOT / "deploy/app/release.sh").read_text(encoding="utf-8")

    assert "VEETBOT_SURFACE_ENV_FILE" in release
    assert "AGENT_SURFACE_API_ENABLED" in release
    assert "AGENT_SURFACE_WORKER_ENABLED" in release
    assert "AGENT_SURFACE_WHATSAPP_ENABLED" in release
    assert "veetbot-surface" in release
