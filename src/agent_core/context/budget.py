"""Absolute-cap context budget allocation."""

from __future__ import annotations

from collections.abc import Mapping

from agent_core.domain.context import ContextBudget
from agent_core.domain.errors import ContextOverflow
from agent_core.domain.messages import ResolvedModel


class ContextBudgetAllocator:
    def __init__(self, config: Mapping[str, object]) -> None:
        self._config = config

    @staticmethod
    def _mapping(value: object, name: str) -> Mapping[str, object]:
        if not isinstance(value, dict):
            raise ValueError(f"context configuration {name} must be a mapping")
        return value

    @staticmethod
    def _integer(mapping: Mapping[str, object], key: str) -> int:
        value = mapping.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"context configuration {key} must be a nonnegative integer")
        return value

    def allocate(self, model: ResolvedModel, *, prefix_tokens: int) -> ContextBudget:
        classes = self._mapping(self._config.get("classes"), "classes")
        output = self._mapping(self._config.get("output"), "output")
        estimator = self._mapping(self._config.get("estimator"), "estimator")
        reserve_cap = self._integer(output, "reserve_tokens")
        reserve = min(
            reserve_cap,
            model.limits.default_output_reserve,
            model.limits.max_output_tokens,
        )
        total = model.limits.context_window_tokens

        def class_config(name: str) -> Mapping[str, object]:
            return self._mapping(classes.get(name), f"classes.{name}")

        platform = self._integer(class_config("platform_policy"), "max_tokens")
        agent = self._integer(class_config("agent_instructions"), "max_tokens")
        tools = self._integer(class_config("tool_definitions"), "max_tokens")
        skill_catalog = self._integer(class_config("skill_catalog"), "max_tokens")
        skill_body = self._integer(class_config("skill_bodies"), "max_tokens")
        raw_memory = classes.get("memory_snapshot")
        memory = (
            0
            if raw_memory is None
            else self._integer(self._mapping(raw_memory, "classes.memory_snapshot"), "max_tokens")
        )
        recall = self._integer(class_config("in_turn_recall"), "max_tokens")
        working = self._integer(class_config("working_state"), "max_tokens")
        knowledge = self._integer(class_config("knowledge_passages"), "max_tokens")
        history_floor = self._integer(class_config("history"), "floor_tokens")
        raw_tool_ratio = class_config("tool_results").get("max_body_ratio")
        raw_margin = estimator.get("safety_margin_ratio")
        if (
            not isinstance(raw_tool_ratio, (int, float))
            or isinstance(raw_tool_ratio, bool)
            or not 0 <= raw_tool_ratio < 1
        ):
            raise ValueError("tool-result body ratio must be in [0, 1)")
        if (
            not isinstance(raw_margin, (int, float))
            or isinstance(raw_margin, bool)
            or not 0 <= raw_margin < 1
        ):
            raise ValueError("context safety margin must be in [0, 1)")

        body_capacity = total - reserve - prefix_tokens
        if body_capacity <= 0:
            raise ContextOverflow("the frozen prefix and output reserve exhaust the model window")
        safety_tokens = int(body_capacity * float(raw_margin))
        usable_body = body_capacity - safety_tokens
        tool_results = int(usable_body * float(raw_tool_ratio))
        # Yieldable recall and knowledge consume the history remainder only when
        # present; reserving every optional cap simultaneously would make the
        # documented 8k history floor impossible on the default 32k model.
        history = usable_body - working - tool_results
        if history < history_floor:
            raise ContextOverflow(
                "the context window cannot provide the configured history floor after fixed caps"
            )
        return ContextBudget(
            total_tokens=total,
            reserve_output_tokens=reserve,
            platform_tokens=platform,
            agent_tokens=agent,
            tool_tokens=tools,
            skill_catalog_tokens=skill_catalog,
            skill_body_tokens=skill_body,
            retrieved_context_tokens=memory + recall,
            history_tokens=history,
            working_state_tokens=working,
            tool_result_tokens=tool_results,
            knowledge_tokens=knowledge,
            safety_margin_ratio=float(raw_margin),
        )
