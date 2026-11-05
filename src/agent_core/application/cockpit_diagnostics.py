"""Content-free device observations and bounded owner-scoped widget diagnostics."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import BuildDiagnostic
from agent_core.domain.cockpit_views import CockpitDeviceFailure, DeviceFailureCode
from agent_core.domain.errors import NotFoundError
from agent_core.domain.events import ProcessEvent
from agent_core.ports.persistence import RepositoryUnitOfWork


def _key(owner: Principal, revision: UUID, code: DeviceFailureCode) -> str:
    identity = json.dumps([owner.tenant_id, owner.principal_id, str(revision), code.value])
    return f"cockpit.device_failure:{uuid5(NAMESPACE_URL, identity)}"


async def record_failure(
    uow: RepositoryUnitOfWork,
    owner: Principal,
    slug: str,
    report: CockpitDeviceFailure,
    now: datetime,
) -> None:
    widget = await uow.cockpit_widgets.get_widget(owner, slug)
    if widget.live_revision_id != report.revision_id or widget.state.value != "active":
        raise NotFoundError("Live widget revision not found.")
    key = _key(owner, report.revision_id, report.code)
    await uow.process_events.append(
        ProcessEvent(
            id=uuid5(NAMESPACE_URL, key),
            derivation_key=key,
            event_type="cockpit.device_failure",
            actor_type="cockpit",
            actor_id=owner.principal_id,
            created_at=now,
            payload={
                "tenant_id": owner.tenant_id,
                "principal_id": owner.principal_id,
                "slug": slug,
                "revision_id": str(report.revision_id),
                "code": report.code.value,
            },
        )
    )


def _findings(items: tuple[BuildDiagnostic, ...]) -> list[dict[str, Any]]:
    return [
        {**item.model_dump(mode="json", exclude_none=True), "message": item.message[:240]}
        for item in items[:5]
    ]


def _retry_reason(error: str | None) -> str | None:
    if not error:
        return None
    status = re.match(r"GitHub answered ([1-5][0-9]{2}) to ", error)
    if status:
        return f"github_http_{status[1]}"
    if "token" in error.lower():
        return "github_authentication"
    if error.startswith("GitHub did not answer"):
        return "github_unreachable"
    return "publication_error"


async def diagnostics(
    uow: RepositoryUnitOfWork,
    owner: Principal,
    slug: str,
    revision_id: UUID | None,
) -> dict[str, Any]:
    imports = await uow.cockpit_outbox.import_failures(owner, slug=slug, limit=3)
    try:
        widget = await uow.cockpit_widgets.get_widget(owner, slug)
    except NotFoundError:
        # A rejected first import has no widget row yet.
        if not imports or revision_id is not None:
            raise
        widget = None
    if revision_id is None:
        latest = await uow.cockpit_widgets.list_revisions(owner, slug, limit=1) if widget else []
        revision = latest[0] if latest else None
    else:
        revision = await uow.cockpit_widgets.get_revision(owner, revision_id)
        if revision.slug != slug:
            raise NotFoundError("Widget revision not found.")
    observations = []
    if revision:
        for code in DeviceFailureCode:
            observation = await uow.process_events.get_by_derivation(_key(owner, revision.id, code))
            if observation:
                observations.append(
                    {"code": code.value, "first_seen": observation.created_at.isoformat()}
                )
    publication = await uow.cockpit_outbox.first_for_widget(owner, slug)
    return {
        "slug": slug,
        "revision_id": str(revision.id) if revision else None,
        "live_revision_id": str(widget.live_revision_id)
        if widget and widget.live_revision_id
        else None,
        "state": revision.state.value if revision else None,
        "diagnostics": _findings(revision.diagnostics) if revision else [],
        "device_failures": observations,
        "import_failures": [
            {"commit_sha": failure.commit_sha, "diagnostics": _findings(failure.diagnostics)}
            for failure in imports
        ],
        "publication": None
        if publication is None
        else {
            "operation": publication.operation.value,
            "request_id": str(publication.revision_id),
            "attempts": publication.attempts,
            "next_attempt_at": publication.next_attempt_at.isoformat(),
            "last_error_code": _retry_reason(publication.last_error),
        },
    }
