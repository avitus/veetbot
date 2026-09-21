"""Typed-judgment provider adapters and the census the contract suite runs over."""

from agent_core.adapters.judgment.fake import FakeJudgmentProvider
from agent_core.adapters.judgment.typesafe import TypeSafeJudgmentProvider

SHIPPED_JUDGMENT_PROVIDERS = (TypeSafeJudgmentProvider, FakeJudgmentProvider)

__all__ = ["SHIPPED_JUDGMENT_PROVIDERS", "FakeJudgmentProvider", "TypeSafeJudgmentProvider"]
