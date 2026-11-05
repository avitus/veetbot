"""A Cockpit publication stack against the in-memory GitHub remote (Milestone 35).

The publication service, the outbox worker and the importer run over the real
GitHub App adapter, which talks to `FakeGitHubRemote` through httpx. The
builder is scripted: it accepts any package unless a file contains `REJECT`,
and records every screen it is given, so the pipeline's own rules are what the
gate tests observe. The real builder runs in the sandbox suite.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx

from agent_core.adapters.cockpit.github import GitHubAppTokens, GitHubWidgetRepository
from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.application.cockpit import BranchImporter, CockpitPublication, CommitOutboxWorker
from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import BuildDiagnostic
from agent_core.ports.cockpit import BuildScreen, WidgetBuild
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.fake_github import (
    APP_ID,
    INSTALLATION_ID,
    NAME,
    OWNER,
    FakeGitHubRemote,
    app_private_key,
)
from tests.contract.support import memory_uow_factory

KEY = app_private_key()
PRINCIPAL = Principal(tenant_id="tenant-a", principal_id="owner")
OWNER_NAME = "Avery Quinn"


def package(marker: str = "v1") -> dict[str, str]:
    return {
        "widget.json": '{"schema": 1, "slug": "weather"}',
        "widget.tsx": f"export default function Weather() {{ return null; }} // {marker}\n",
        "fixtures/typical.json": "{}",
        "fixtures/empty.json": "{}",
        "fixtures/stale.json": "{}",
    }


@dataclass
class ScriptedBuilder:
    screens: list[BuildScreen] = field(default_factory=list)

    async def build(
        self,
        principal: Principal,
        slug: str,
        files: Mapping[str, str],
        *,
        screen: BuildScreen,
        preview: Mapping[str, str] | None = None,
    ) -> WidgetBuild:
        del principal
        self.screens.append(screen)
        if any("REJECT" in text for text in files.values()):
            return WidgetBuild(
                ok=False,
                diagnostics=(
                    BuildDiagnostic(rule="lint.banned", file="widget.tsx", message="not allowed"),
                ),
            )
        digest = hashlib.sha256(json.dumps(dict(files), sort_keys=True).encode()).hexdigest()
        bundle = f"(()=>{{}})();//{slug}:{digest}"
        return WidgetBuild(
            ok=True,
            diagnostics=(),
            kit_api=1,
            bundle=bundle,
            bundle_sha256=hashlib.sha256(bundle.encode()).hexdigest(),
            manifest={"slug": slug},
        )


@dataclass
class Stack:
    clock: FixedClock
    principal: Principal
    uow_factory: UnitOfWorkFactory
    remote: FakeGitHubRemote
    repository: GitHubWidgetRepository
    builder: ScriptedBuilder
    publication: CockpitPublication
    outbox: CommitOutboxWorker
    importer: BranchImporter

    def later(self, seconds: int = 3600) -> None:
        self.clock.advance(timedelta(seconds=seconds))

    def head_files(self, slug: str) -> dict[str, str]:
        head = self.remote.refs["refs/heads/cockpit/widgets"]
        prefix = f"cockpit/widgets/{slug}/"
        return {
            path.removeprefix(prefix): text
            for path, text in self.remote.files_at(head).items()
            if path.startswith(prefix)
        }

    def trailer_count(self, revision_id: object) -> int:
        head = self.remote.refs["refs/heads/cockpit/widgets"]
        return sum(
            1
            for sha in self.remote._ancestry(head)
            if f"Veetbot-Revision: {revision_id}" in self.remote.commits[sha].message
        )


@asynccontextmanager
async def publication_stack(
    *,
    uow_factory: UnitOfWorkFactory | None = None,
    clock: FixedClock | None = None,
    principal: Principal = PRINCIPAL,
) -> AsyncIterator[Stack]:
    """In memory by default; the PostgreSQL test passes its own unit of work."""

    if uow_factory is None or clock is None:
        clock, uow_factory = await memory_uow_factory()
    ids = SequenceIdFactory()
    remote = FakeGitHubRemote(private_key_pem=KEY, now=clock.now)
    builder = ScriptedBuilder()
    async with httpx.AsyncClient(transport=remote.transport()) as client:
        tokens = GitHubAppTokens(
            client,
            app_id=APP_ID,
            installation_id=INSTALLATION_ID,
            private_key_pem=KEY,
            clock=clock,
        )
        repository = GitHubWidgetRepository(client, tokens, repository=f"{OWNER}/{NAME}")
        await repository.ensure_branch()
        common: dict[str, Any] = {"uow_factory": uow_factory, "clock": clock, "ids": ids}
        yield Stack(
            clock=clock,
            principal=principal,
            uow_factory=uow_factory,
            remote=remote,
            repository=repository,
            builder=builder,
            publication=CockpitPublication(**common, builder=builder, owner_names=(OWNER_NAME,)),
            outbox=CommitOutboxWorker(**common, repository=repository, principal=principal),
            importer=BranchImporter(
                **common,
                repository=repository,
                builder=builder,
                principal=principal,
                owner_names=(OWNER_NAME,),
            ),
        )
