"""Produce the same scoped queries that automatic context and memory.search read."""

from collections.abc import Sequence

from jsonschema import Draft202012Validator

from agent_core.context.builder import current_user_text
from agent_core.context.recall_budget import select_current_search_results
from agent_core.context.working_state import WorkingStateManager
from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError
from agent_core.domain.memory import RecallQuery
from agent_core.domain.messages import ToolCallItem
from agent_core.domain.runs import Run, RunCheckpoint
from agent_core.domain.sessions import project_scope
from agent_core.memory.retrieval import HybridMemoryRetriever
from agent_core.ports.memory import QueryFormer
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.tools.memory_search import MemorySearchTool
from agent_core.tools.tool_call import TOOL_CALL_TOOL_NAME, resolve_deferred_target


class SemanticRecallPreparation:
    execution_budget = True

    def __init__(
        self,
        factory: UnitOfWorkFactory,
        principal: Principal,
        retriever: HybridMemoryRetriever,
        former: QueryFormer,
    ) -> None:
        self._factory, self._principal = factory, principal.model_copy(deep=True)
        self._retriever, self._former = retriever, former
        self._tool = MemorySearchTool(retriever, model_planning=True)
        self._validate = Draft202012Validator(self._tool.spec.input_schema)

    async def plan(self, run: Run, checkpoint: RunCheckpoint) -> list[RecallQuery]:
        if checkpoint.pending_tool_calls:
            calls = [ToolCallItem.model_validate(value) for value in checkpoint.pending_tool_calls]
            if not any(call.name in {TOOL_CALL_TOOL_NAME, self._tool.spec.name} for call in calls):
                return []
            try:
                async with self._factory() as uow:
                    configured = await uow.agents.get_version(run.agent_id, run.agent_version)
            except NotFoundError:
                return []
            if self._tool.spec.name not in configured.enabled_tools:
                return []
            if any(call.name == TOOL_CALL_TOOL_NAME for call in calls):
                targets = [
                    resolve_deferred_target(call, checkpoint)
                    if call.name == TOOL_CALL_TOOL_NAME
                    and call.parse_error is None
                    and TOOL_CALL_TOOL_NAME in configured.enabled_tools
                    else call
                    for call in calls
                ]
                calls = [target for target in targets if isinstance(target, ToolCallItem)]
            searches = [
                call
                for call in calls
                if call.name == self._tool.spec.name and call.parse_error is None
            ]
            if not 1 <= len(searches) <= 4:
                return []
            async with self._factory() as uow:
                session = await uow.sessions.get(run.session_id, self._principal)
            queries = [
                self._tool.query(
                    call.arguments,
                    tenant_id=self._principal.tenant_id,
                    principal_id=self._principal.principal_id,
                    current_scope=project_scope(session.metadata),
                )
                for call in searches
                if self._validate.is_valid(call.arguments)
            ]
        else:
            active = [
                item
                for item in checkpoint.conversation
                if (
                    (sequence := getattr(item, "source_event_sequence", None)) is None
                    or sequence >= run.seed_event_sequence
                )
            ]
            message = current_user_text(active)
            if message is None:
                return []
            async with self._factory() as uow:
                session = await uow.sessions.get(run.session_id, self._principal)
            queries = self._former.form(
                run,
                WorkingStateManager.load(checkpoint.working_state),
                message,
                current_scope=project_scope(session.metadata),
            )
            _, allowance, _ = await select_current_search_results(
                checkpoint.conversation, self._retriever, session_id=run.session_id, run_id=run.id
            )
            queries = [
                bounded for query in queries if (bounded := allowance.bound(query)) is not None
            ]
        return queries

    async def prepare(self, run: Run, queries: Sequence[RecallQuery]) -> None:
        if queries:
            await self._retriever.prepare_semantic_queries(
                queries, session_id=run.session_id, run_id=run.id
            )
