"""Gates 2 and 4 with the real builder image (Milestone 35).

docs/plan/cockpit.md, Hard gates 2 and 4. The publication service builds each
package in the builder image through the Docker execution adapter, and the
commit path is the GitHub App adapter against the in-memory GitHub remote. A
package the builder rejects, for any rule, leaves no outbox entry, no commit and
no event; personal data in a file or the commit message is one of those rules,
and its diagnostic never repeats the value.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from agent_core.adapters.cockpit.builder import SandboxWidgetBuilder, cockpit_build_sandbox
from agent_core.adapters.determinism import RandomIdFactory, SystemClock
from agent_core.adapters.execution.docker import (
    DockerExecutionEnvironment,
    resolve_local_image_digest,
)
from agent_core.domain.cockpit import RevisionState, Widget, WidgetSettings, WidgetState
from agent_core.domain.execution import ResourceLimits
from agent_core.domain.people import Person
from tests.gates.cockpit_publication_support import (
    OWNER_NAME,
    PRINCIPAL,
    Stack,
    publication_stack,
)

pytestmark = [pytest.mark.integration, pytest.mark.sandbox]

REPOSITORY = Path(__file__).resolve().parents[2]
WEATHER = REPOSITORY / "cockpit/examples/widgets/weather"
PERSON = "Jordan Example"


def _weather(slug: str = "weather") -> dict[str, str]:
    files = {
        path.relative_to(WEATHER).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(WEATHER.rglob("*"))
        if path.is_file()
    }
    manifest = json.loads(files["widget.json"])
    files["widget.json"] = json.dumps({**manifest, "slug": slug}, indent=2) + "\n"
    return files


def _edit(name: str, change: Callable[[str], str]) -> Callable[[dict[str, str]], dict[str, str]]:
    def edited(files: dict[str, str]) -> dict[str, str]:
        return {**files, name: change(files[name])}

    return edited


def _manifest(**overrides: object) -> Callable[[dict[str, str]], dict[str, str]]:
    return _edit("widget.json", lambda text: json.dumps({**json.loads(text), **overrides}))


@pytest.fixture
async def real_stack() -> AsyncIterator[Stack]:
    limits = yaml.safe_load(
        (REPOSITORY / "src/agent_core/sandbox/limits.yaml").read_text(encoding="utf-8")
    )["resources"]
    adapter = DockerExecutionEnvironment(
        SystemClock(), RandomIdFactory(), hard_cap_seconds=300, reaper_grace_seconds=0
    )
    manager = cockpit_build_sandbox(
        adapter,
        image_digest=await resolve_local_image_digest("veetbot-cockpit-builder:dev"),
        limits=ResourceLimits(**{name: int(value) for name, value in limits.items()}),
    )
    builder = SandboxWidgetBuilder(manager, RandomIdFactory())
    try:
        async with publication_stack() as stack:
            stack.publication._builder = builder
            now = datetime.now(UTC)
            async with stack.uow_factory() as uow:
                await uow.people.put(
                    Person(
                        id=UUID(int=701),
                        tenant_id=PRINCIPAL.tenant_id,
                        principal_id=PRINCIPAL.principal_id,
                        display_name=PERSON,
                        created_at=now,
                        updated_at=now,
                    ),
                    expected_revision=0,
                )
            yield stack
    finally:
        await manager.close()


async def _assert_nothing_published(stack: Stack, slug: str, head: str | None) -> None:
    async with stack.uow_factory() as uow:
        assert await uow.cockpit_outbox.due("tenant-a", now=stack.clock.now(), limit=10) == []
        assert await uow.cockpit_events.after(PRINCIPAL, sequence=0, limit=10) == []
        widget = await uow.cockpit_widgets.get_widget(PRINCIPAL, slug)
    assert widget.live_revision_id is None
    assert await stack.outbox.run_once() == 0
    assert await stack.repository.head() == head


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        (_manifest(title="x" * 41), "manifest.schema"),
        (lambda files: {**files, "notes.txt": "x"}, "package.file_not_allowed"),
        (
            _edit("widget.tsx", lambda text: text + "//" + "x" * (64 * 1024)),
            "package.file_too_large",
        ),
        (_edit("widget.tsx", lambda text: "import _ from 'lodash';\n" + text), "lint.import"),
        (_edit("widget.tsx", lambda text: text + "\nvoid eval('1');\n"), "lint.banned"),
        (_edit("widget.tsx", lambda text: text + "\n// token = abc123\n"), "screen.credential"),
        (
            lambda files: {k: v for k, v in files.items() if k != "fixtures/stale.json"},
            "package.missing_file",
        ),
        (
            _edit(
                "widget.tsx",
                lambda text: text.replace(
                    "  const feed = feeds['agent.weather'];",
                    "  if (size) throw new Error('broken');\n"
                    "  const feed = feeds['agent.weather'];",
                ),
            ),
            "render.throws",
        ),
    ],
    ids=[
        "manifest",
        "file-allowlist",
        "size-cap",
        "import-allowlist",
        "banned-reference",
        "credential",
        "fixtures",
        "render",
    ],
)
async def test_every_rejection_rule_leaves_nothing_published(
    real_stack: Stack, change: Callable[[dict[str, str]], dict[str, str]], rule: str
) -> None:
    head = await real_stack.repository.head()
    revision = await real_stack.publication.write(
        PRINCIPAL, "weather", change(_weather()), expected_revision=None
    )
    assert revision.state is RevisionState.REJECTED
    assert rule in {diagnostic.rule for diagnostic in revision.diagnostics}, revision.diagnostics
    assert revision.bundle is None
    await _assert_nothing_published(real_stack, "weather", head)


async def test_a_valid_package_from_the_builder_image_commits_and_goes_live(
    real_stack: Stack,
) -> None:
    revision = await real_stack.publication.write(
        PRINCIPAL, "weather", _weather(), expected_revision=None
    )
    assert revision.state is RevisionState.PENDING_COMMIT, revision.diagnostics
    assert await real_stack.outbox.run_once() == 1
    assert real_stack.head_files("weather") == _weather()


@pytest.mark.parametrize(
    ("slug", "change", "rule", "file", "value"),
    [
        (
            "weather",
            _edit("widget.tsx", lambda t: t + f"\n// {PERSON}\n"),
            "screen.name",
            "widget.tsx",
            PERSON,
        ),
        (
            "weather",
            _edit("fixtures/typical.json", lambda t: t.replace("Example City", OWNER_NAME)),
            "screen.name",
            "fixtures/typical.json",
            OWNER_NAME,
        ),
        (
            "weather",
            _edit("widget.tsx", lambda t: t + "\n// Harbor Lane 12\n"),
            "screen.setting",
            "widget.tsx",
            "Harbor Lane 12",
        ),
        (
            "weather",
            _edit("widget.tsx", lambda t: t + "\n// jordan@personal.net\n"),
            "screen.email",
            "widget.tsx",
            "jordan@personal.net",
        ),
        (
            "weather",
            _edit("fixtures/typical.json", lambda t: t.replace("Example City", "+1 415 555 0134")),
            "screen.phone",
            "fixtures/typical.json",
            "415 555 0134",
        ),
        ("jordan-weather", lambda files: files, "screen.name", "commit message", "Jordan"),
    ],
    ids=["people-name", "owner-name", "settings-value", "email", "phone", "commit-message"],
)
async def test_personal_data_in_a_file_or_the_commit_message_is_never_published(
    real_stack: Stack,
    slug: str,
    change: Callable[[dict[str, str]], dict[str, str]],
    rule: str,
    file: str,
    value: str,
) -> None:
    async with real_stack.uow_factory() as uow:
        await uow.people.put(
            Person(
                id=UUID(int=702),
                tenant_id=PRINCIPAL.tenant_id,
                principal_id=PRINCIPAL.principal_id,
                display_name="Jordan",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
            expected_revision=0,
        )
        await uow.cockpit_widgets.create_widget(
            Widget(
                tenant_id=PRINCIPAL.tenant_id,
                principal_id=PRINCIPAL.principal_id,
                slug="markets",
                state=WidgetState.ACTIVE,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await uow.cockpit_widgets.put_settings(
            WidgetSettings(
                tenant_id=PRINCIPAL.tenant_id,
                principal_id=PRINCIPAL.principal_id,
                slug="markets",
                values={"street": "Harbor Lane 12"},
                updated_at=datetime.now(UTC),
            )
        )
    head = await real_stack.repository.head()
    revision = await real_stack.publication.write(
        PRINCIPAL, slug, change(_weather(slug)), expected_revision=None
    )
    assert revision.state is RevisionState.REJECTED
    found = {(diagnostic.rule, diagnostic.file) for diagnostic in revision.diagnostics}
    assert (rule, file) in found, revision.diagnostics
    assert all(value not in diagnostic.message for diagnostic in revision.diagnostics)
    await _assert_nothing_published(real_stack, slug, head)
