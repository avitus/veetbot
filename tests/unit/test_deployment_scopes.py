"""Deployment must carry every reviewed owner capability to existing hosts."""

from pathlib import Path

from agent_core.policy.scopes import PLATFORM_SCOPES

ROOT = Path(__file__).resolve().parents[2]


def test_every_platform_scope_has_a_deployment_classification() -> None:
    template = ROOT / "deploy/veetbot.env.example"
    scopes = next(
        set(line.removeprefix("AUTH_SCOPES=").split(","))
        for line in template.read_text().splitlines()
        if line.startswith("AUTH_SCOPES=")
    )
    # Surface identities belong to their restricted role; demo.write is test-only.
    non_owner = {"surface.read", "surface.write", "demo.write"}
    assert scopes.isdisjoint(non_owner)
    assert scopes | non_owner == PLATFORM_SCOPES


def test_only_owner_units_load_release_scopes_after_host_environment() -> None:
    owner_units = {"veetbot-api", "veetbot-worker", "veetbot-async-worker", "veetbot-maintenance"}
    directive = "EnvironmentFile=-/opt/veetbot/current/.owner-scopes.env"
    for path in (ROOT / "deploy/systemd").glob("*.service"):
        content = path.read_text()
        if path.stem in owner_units:
            assert directive in content
            assert content.index(directive) > content.index(
                "EnvironmentFile=/etc/veetbot/veetbot.env"
            )
        else:
            assert directive not in content


def test_scope_generation_preserves_host_grants_and_is_idempotent(tmp_path: Path) -> None:
    import os
    import subprocess

    template = tmp_path / "template.env"
    template.write_text("AUTH_SCOPES=settings.read,settings.write\nAUTH_TOKEN=must-not-be-copied\n")
    command = ["bash", str(ROOT / "deploy/app/owner-scopes.sh"), str(template)]
    environment = {**os.environ, "AUTH_SCOPES": "memory.read,settings.read"}
    result = subprocess.run(command, env=environment, capture_output=True, text=True, check=True)
    assert result.stdout == "AUTH_SCOPES=memory.read,settings.read,settings.write\n"
    environment["AUTH_SCOPES"] = result.stdout.strip().removeprefix("AUTH_SCOPES=")
    repeated = subprocess.run(command, env=environment, capture_output=True, text=True, check=True)
    assert repeated.stdout == result.stdout


def test_scope_generation_refuses_missing_duplicate_or_unsafe_assignments(tmp_path: Path) -> None:
    import os
    import subprocess

    template = tmp_path / "template.env"
    for contents, existing in (
        ("AUTH_TOKEN=irrelevant\n", ""),
        ("AUTH_SCOPES=\n", ""),
        ("AUTH_SCOPES=settings.read\nAUTH_SCOPES=settings.write\n", ""),
        ("AUTH_SCOPES=settings.read\n", "settings.write\nAUTH_TOKEN=unsafe"),
        ("AUTH_SCOPES=$(touch unexpected)\n", ""),
    ):
        template.write_text(contents)
        result = subprocess.run(
            ["bash", str(ROOT / "deploy/app/owner-scopes.sh"), str(template)],
            env={**os.environ, "AUTH_SCOPES": existing},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert result.stdout == ""
