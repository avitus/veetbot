"""Read-only People tools; recalled people never supply action authority."""

from __future__ import annotations

import json
from typing import Any

from agent_core.application.authorization import require_scope
from agent_core.application.people import PublicPeopleService, _safe
from agent_core.application.people_context import PeopleContextService
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import RecallQuery, Sensitivity
from agent_core.domain.messages import TextPart
from agent_core.domain.people import PeopleInteraction, Person
from agent_core.domain.people_tools import (
    HistoryToolItem,
    PeopleContextArgs,
    PeopleContextResult,
    PeopleHistoryArgs,
    PeopleHistoryResult,
    PeopleSearchArgs,
    PeopleSearchResult,
)
from agent_core.domain.people_views import PeopleSectionQuery
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import ToolExecutionContext, ToolResult, ToolSpec
from agent_core.memory.people import resolve_identity
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory


def _spec(name: str, description: str, schema: dict[str, Any], output: dict[str, Any]) -> ToolSpec:
    return ToolSpec(
        name=name,
        version="1.0.0",
        description=description,
        input_schema={key: value for key, value in schema.items() if key != "title"},
        output_schema=output,
        required_scopes={"people.read"},
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        timeout_seconds=10,
        maximum_output_bytes=65536,
        allow_parallel=True,
        output_trust=TrustLevel.MEMORY,
    )


class PeopleSearchTool:
    spec = _spec(
        "people.search",
        "Resolve names, aliases or endpoints; preserve ambiguity.",
        PeopleSearchArgs.model_json_schema(),
        PeopleSearchResult.model_json_schema(),
    )

    def __init__(self, factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._factory = factory
        self._clock = clock

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        require_scope(context.principal, "people.read")
        request = PeopleSearchArgs.model_validate(arguments)
        async with self._factory() as uow, uow.people.lock(context.principal):
            result = await resolve_identity(
                uow.people,
                context.principal,
                kind=request.kind,
                namespace=request.namespace,
                value=request.text,
                context=request.context,
                at=request.at or self._clock.now(),
                ceiling=Sensitivity.SENSITIVE,
            )
            candidates = []
            for person_id in result.person_ids:
                person = await uow.people.get(
                    context.principal, person_id, ceiling=Sensitivity.SENSITIVE
                )
                if isinstance(person, Person) and _safe(person.display_name):
                    candidates.append(
                        {
                            "id": str(person.id),
                            "display_name": person.display_name,
                            "revision": person.revision,
                        }
                    )
            if candidates:
                await uow.events.append(
                    NewEvent(
                        session_id=context.session_id,
                        run_id=context.run_id,
                        event_type="people.context.used",
                        actor_type="memory",
                        payload={"record_ids": [item["id"] for item in candidates]},
                    )
                )
        payload = {"status": result.status, "candidates": candidates}
        return ToolResult(
            ok=True,
            content=[TextPart(text=json.dumps(payload))],
            structured=payload,
            output_trust=TrustLevel.MEMORY,
        )


class PeopleContextTool:
    spec = _spec(
        "people.context",
        "Recall grounded facts and recent interactions for up to three selected "
        "people within the existing memory budget.",
        PeopleContextArgs.model_json_schema(),
        PeopleContextResult.model_json_schema(),
    )

    def __init__(self, service: PeopleContextService) -> None:
        self._service = service

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        request = PeopleContextArgs.model_validate(arguments)
        result = await self._service.recall(
            context.principal,
            request.person_ids,
            RecallQuery(
                tenant_id=context.tenant_id,
                principal_id=context.principal.principal_id,
                current_scope=request.scope,
                text=request.text,
                as_of=request.as_of,
                known_at=request.known_at,
                budget_tokens=2000,
                max_items=20,
                min_score=0.1,
                sensitivity_ceiling=Sensitivity.SENSITIVE,
            ),
            session_id=context.session_id,
            run_id=context.run_id,
        )
        return ToolResult(
            ok=True,
            content=[TextPart(text=result.rendered)],
            structured={"trace_id": str(result.trace_id), "truncated": result.truncated},
            output_trust=TrustLevel.MEMORY,
        )


class PeopleHistoryTool:
    spec = _spec(
        "people.history",
        "Page a person's attributed interactions.",
        PeopleHistoryArgs.model_json_schema(),
        PeopleHistoryResult.model_json_schema(),
    )

    def __init__(self, service: PublicPeopleService) -> None:
        self._service = service

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        request = PeopleHistoryArgs.model_validate(arguments)
        result = PeopleHistoryResult(items=[], next_cursor=request.cursor, coverage="")
        for _ in range(request.limit):
            page = await self._service.section(
                context.principal,
                request.person_id,
                PeopleSectionQuery(
                    section="history",
                    **request.model_dump(exclude={"person_id", "limit", "cursor"}),
                    limit=1,
                    cursor=result.next_cursor,
                ),
                ceiling=Sensitivity.SENSITIVE,
                run_id=context.run_id,
            )
            if not page.items:
                result = result.model_copy(update={"next_cursor": None, "coverage": page.coverage})
                break
            row = page.items[0]
            assert isinstance(row, PeopleInteraction)
            item = HistoryToolItem(
                id=row.id,
                channel=row.channel,
                attribution=row.attribution,
                direction=row.direction,
                summary=row.summary[:400],
                occurred_at=row.occurred_at,
                participants=row.participants[:6],
                source_ids=row.support_ids[:4],
                details_truncated=(
                    len(row.summary) > 400 or len(row.participants) > 6 or len(row.support_ids) > 4
                ),
            )
            proposed = PeopleHistoryResult(
                items=[*result.items, item], next_cursor=page.next_cursor, coverage=page.coverage
            )
            if len(proposed.model_dump_json().encode()) > 8000:
                break
            result = proposed
            if result.next_cursor is None:
                break
        payload = result.model_dump(mode="json")
        return ToolResult(
            ok=True,
            content=[TextPart(text=result.model_dump_json())],
            structured=payload,
            output_trust=TrustLevel.MEMORY,
        )
