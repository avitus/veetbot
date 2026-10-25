"""The Cockpit evaluation's builder and branch (Milestone 35).

docs/plan/cockpit.md, the tools. The deterministic suite has no builder image,
so its builder judges what can be judged without one: the package's files and
limits, the manifest against the shipped widget schema, and the privacy
screen's names and settings values. The case writes a reference package whose
real build `cockpit/tests/builder.test.mjs` proves, and a gate test keeps the
two identical. After the run the outbox commits to an in-memory branch, as the
maintenance role would.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema.validators import validator_for

from agent_core.application.cockpit import CommitOutboxWorker
from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import BuildDiagnostic, package_violation
from agent_core.ports.cockpit import BuildScreen, WidgetBuild, WidgetRepository
from agent_core.ports.persistence import UnitOfWorkFactory

WIDGET_SCHEMA = Path("cockpit/schema/widget.schema.json")
_REQUIRED_FILES = (
    "widget.json",
    "widget.tsx",
    "fixtures/typical.json",
    "fixtures/empty.json",
    "fixtures/stale.json",
)
# Every queued change settles within a few passes; a bound keeps a stuck one from spinning.
_DRAIN_PASSES = 8


class ManifestWidgetBuilder:
    """A builder that checks files, the manifest and the screen, and bundles nothing real."""

    def __init__(self, repository_root: Path) -> None:
        schema = json.loads((repository_root / WIDGET_SCHEMA).read_text(encoding="utf-8"))
        self._validator = validator_for(schema)(schema)

    async def build(
        self,
        principal: Principal,
        slug: str,
        files: Mapping[str, str],
        *,
        screen: BuildScreen,
    ) -> WidgetBuild:
        del principal
        found: list[BuildDiagnostic] = []
        violation = package_violation(files)
        if violation is not None:
            found.append(BuildDiagnostic(rule="package.files", message=violation))
        for name in _REQUIRED_FILES:
            if name not in files:
                found.append(BuildDiagnostic(rule="package.missing", file=name, message="missing"))
        manifest: Any = None
        try:
            manifest = json.loads(files.get("widget.json", "null"))
        except ValueError:
            found.append(
                BuildDiagnostic(rule="manifest.json", file="widget.json", message="not JSON")
            )
        if manifest is not None:
            for error in sorted(self._validator.iter_errors(manifest), key=lambda e: e.message):
                found.append(
                    BuildDiagnostic(
                        rule="manifest.schema", file="widget.json", message=error.message
                    )
                )
            if isinstance(manifest, dict) and manifest.get("slug") != slug:
                found.append(
                    BuildDiagnostic(
                        rule="manifest.slug", file="widget.json", message="slug differs"
                    )
                )
        for rule, values in (
            ("privacy.name", screen.names),
            ("privacy.setting", screen.settings_values),
        ):
            for value in values:
                for name, text in sorted(files.items()):
                    if value.strip() and value.casefold() in text.casefold():
                        found.append(
                            BuildDiagnostic(rule=rule, file=name, message="personal value")
                        )
        if found or not isinstance(manifest, dict):
            return WidgetBuild(ok=False, diagnostics=tuple(found))
        digest = hashlib.sha256(json.dumps(dict(files), sort_keys=True).encode("utf-8")).hexdigest()
        bundle = f"/* evaluation bundle {slug} {digest} */"
        return WidgetBuild(
            ok=True,
            diagnostics=(),
            kit_api=int(manifest["kit_api"]),
            bundle=bundle,
            bundle_sha256=hashlib.sha256(bundle.encode("utf-8")).hexdigest(),
            manifest=manifest,
        )


@dataclass(frozen=True, slots=True)
class CommittedWidget:
    """A widget after the drain: its live revision and the files the branch holds for it."""

    slug: str
    state: str
    commit_sha: str | None
    files: Mapping[str, str]
    branch_files: Mapping[str, str]


async def drain_cockpit_outbox(
    *,
    uow_factory: UnitOfWorkFactory,
    repository: WidgetRepository,
    worker: CommitOutboxWorker,
    principal: Principal,
) -> tuple[CommittedWidget, ...]:
    """Commit every queued change to the composition's in-memory branch; report each widget."""

    await repository.ensure_branch()
    for _ in range(_DRAIN_PASSES):
        if not await worker.run_once():
            break
    head = await repository.head()
    widgets: list[CommittedWidget] = []
    async with uow_factory() as uow:
        for widget in await uow.cockpit_widgets.list_widgets(principal, include_deleted=True):
            live = (
                None
                if widget.live_revision_id is None
                else await uow.cockpit_widgets.get_revision(principal, widget.live_revision_id)
            )
            branch = None if head is None else await repository.read_widget(widget.slug, at=head)
            widgets.append(
                CommittedWidget(
                    slug=widget.slug,
                    state=live.state.value if live is not None else widget.state.value,
                    commit_sha=None if live is None else live.commit_sha,
                    files={} if live is None else dict(live.files),
                    branch_files=branch or {},
                )
            )
    return tuple(sorted(widgets, key=lambda item: item.slug))
