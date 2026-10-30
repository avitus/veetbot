"""A job is a typed run, with no implicit planning or final model turn."""

from collections.abc import Awaitable, Callable

from agent_core.domain.jobs import JobExecutionError, JobResult
from agent_core.domain.messages import AssistantMessage, TextPart
from agent_core.domain.runs import FailureReason, OutcomeKind, RunFailure, RunKind, RunOutcome
from agent_core.domain.schedules import JobTask
from agent_core.runtime.job_model import JobModelCompletion
from agent_core.runtime.loop import RunContext


async def execute_job(
    context: RunContext, execution: Callable[..., Awaitable[JobResult]] | None
) -> RunOutcome | None:
    """Run a saved job without model planning or synthesis and return its outcome."""
    if context.run.kind is not RunKind.JOB:
        return None
    try:
        if execution is None:
            raise JobExecutionError("job.disabled")
        async with context.uow_factory() as uow:
            session = await uow.sessions.get(context.run.session_id, context.principal)
        task = JobTask.model_validate(session.metadata.get("job_task"))
        model = JobModelCompletion(
            run=context.run,
            provider=context.model_provider,
            resolved=context.resolved_model,
            budgets=context.budgets,
            clock=context.clock,
            ids=context.ids,
        )
        result = await execution(
            principal=context.principal,
            slug=task.slug,
            revision_id=task.revision_id,
            grant_id=task.grant_id,
            run_id=context.run.id,
            lease_epoch=0 if context.lease is None else context.lease.lease_epoch,
            model=model.complete,
        )
        context.token.raise_if_cancelled()
    except (ValueError, TimeoutError) as exc:
        return RunOutcome(
            kind=OutcomeKind.FAILED,
            failure=RunFailure(
                reason=FailureReason.DEPENDENCY_UNAVAILABLE,
                error_class=type(exc).__name__,
                message=str(exc)[:2000] or "Job timed out.",
                occurred_at=context.clock.now(),
            ),
        )
    summary = result.value.get("summary") if isinstance(result.value, dict) else result.value
    text = (
        summary.splitlines()[0][:2000]
        if isinstance(summary, str) and summary.strip()
        else "Job completed."
    )
    if result.diagnostics:
        text += "\n\nLog:\n" + result.diagnostics[:8000]
    return RunOutcome(
        kind=OutcomeKind.COMPLETED, final_message=AssistantMessage(content=[TextPart(text=text)])
    )
