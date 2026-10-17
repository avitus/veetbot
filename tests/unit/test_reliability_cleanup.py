"""A failing or stalled optional hook cannot block cleanup or discard an answer."""

import asyncio
from typing import Any

import pytest

from agent_core.application.people_erasure import PeopleErasureService
from agent_core.application.skill_review import SkillBackgroundReview
from agent_core.bootstrap import build
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from tests.integration.m2_support import memory_settings


@pytest.mark.parametrize("stalled", [False, True])
async def test_optional_hook_failure_preserves_answer_and_later_cleanup(
    monkeypatch: pytest.MonkeyPatch, stalled: bool
) -> None:
    """Verify that optional hook failure preserves answer and later cleanup."""
    import agent_core.bootstrap as bootstrap

    monkeypatch.setattr(bootstrap, "POST_RUN_HOOK_TIMEOUT_SECONDS", 0.01, raising=False)
    seen: list[str] = []

    async def review(*_args: Any, **_kwargs: Any) -> None:
        """Fail or stall an optional review hook after recording that it ran."""
        seen.append("review")
        if stalled:
            await asyncio.Event().wait()
        raise RuntimeError("Synthetic optional hook failure")

    async def erasure(*_args: Any, **_kwargs: Any) -> int:
        """Record that later privacy cleanup still runs after an optional hook fails."""
        seen.append("erasure")
        return 0

    monkeypatch.setattr(SkillBackgroundReview, "after_run", review)
    monkeypatch.setattr(PeopleErasureService, "resume_pending", erasure)
    async with build(
        settings=memory_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(text="The answer survives.")]),
    ) as app:
        async with asyncio.timeout(2):
            run_id = await app.runs.submit("Answer the question.")
        run = await app.runs.get(run_id)
        assert run.status is RunStatus.COMPLETED
        assert run.final_message == "The answer survives."
        assert "review" in seen and "erasure" in seen[seen.index("review") + 1 :]


async def test_delegated_continuation_keeps_context_and_is_not_an_optional_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that delegated continuation keeps context and is not an optional hook."""
    from dataclasses import replace

    import agent_core.bootstrap as bootstrap
    from agent_core.application.delegations import DelegationJoin
    from agent_core.domain.failures import failure_context

    monkeypatch.setattr(bootstrap, "POST_RUN_HOOK_TIMEOUT_SECONDS", 0.01)
    contexts: list[dict[str, Any]] = []

    async def join(*_args: Any) -> None:
        """Delay a parent continuation beyond the optional-hook limit and capture its log
        scope.
        """
        await asyncio.sleep(0.03)
        contexts.append(failure_context())

    monkeypatch.setattr(DelegationJoin, "after_run", join)
    async with build(
        settings=replace(memory_settings(), delegation_enabled=True),
        script=FakeModelScript(turns=[ScriptedTurn(text="Done.")]),
    ) as app:
        run_id = await app.runs.submit("Finish the task.")
        assert len(contexts) == 1, "A resumed parent must not inherit the optional hook timeout."
        assert contexts[0]["run_id"] == str(run_id)
        assert contexts[0]["session_id"]
