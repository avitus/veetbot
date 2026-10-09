"""A scheduler-origin seed without an owner-message admission or wall-clock worker."""

from typing import cast
from uuid import UUID

from agent_core.application.public_services import PublicRunService
from agent_core.bootstrap import Composition
from agent_core.domain.views import TextContentBlock


async def submit_scheduled_seed(app: Composition, session_id: UUID, message: str) -> UUID:
    # Keep all configured scopes deliberately: scopes are not proof that the
    # owner personally requested this occurrence (ADR-0172).
    service = cast(PublicRunService, app.services.runs)
    async with app.uow_factory() as uow:
        session = await uow.sessions.get(session_id, app.principal)
        prepared = await service._submit_in(
            uow,
            app.principal,
            session,
            [TextContentBlock(text=message)],
            trace_id=None,
            actor_type="scheduler",
        )
    await service._dispatch_prepared(prepared)
    return prepared.run.id
