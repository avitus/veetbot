"""Synthetic Email replay through the production assessment and formation path."""

from __future__ import annotations

import importlib
import json
import os
import tempfile
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from agent_core.application.email import save_value
from agent_core.config import DeploymentMode, Settings, load_settings
from agent_core.context.rendering import render_email_context
from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailAccount, EmailTask, EmailThread
from agent_core.domain.email_semantics import EmailSemanticSource
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import MemoryAuthority, MemoryDerivation, Sensitivity
from agent_core.domain.messages import AssistantMessage, ResolvedModel, TextPart, ToolResultItem
from agent_core.domain.people import PeopleCommitment, PeopleInteraction, PeopleQuery, PeopleSource
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import OutcomeKind, RunOutcome, RunStatus
from agent_core.evals.email_people import (
    EmailPeopleCase,
    EmailPeopleObservation,
    ObservedAssessment,
    load_email_corpora,
    score_email_case,
    score_observations,
)
from agent_core.evals.memory_distillation import _evaluation_settings, require_committed_tree
from agent_core.evals.people import Observation
from agent_core.evals.people_comparison import observe
from agent_core.evals.people_execution import BorrowedProvider, BudgetedProvider, EvaluationBudget
from agent_core.memory.email_people import EmailPeopleFormationService
from agent_core.memory.email_semantics import EmailSemanticFormationService
from agent_core.model import NON_ROUTED_MODEL_POLICIES
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.runtime.email_tasks import _TaskIO
from agent_core.runtime.loop import RunContext
from agent_core.tools.registry import StaticToolRegistry

EmailPolicy = Literal["email-semantic@1", "email-semantic@2"]


class _EvaluatedEmail(EmailPeopleFormationService):
    @property
    def enabled(self) -> bool:
        return True


class _EvaluatedBaseline(EmailSemanticFormationService):
    @property
    def enabled(self) -> bool:
        return True


async def evaluate_email_case(
    settings: Settings,
    case: EmailPeopleCase,
    *,
    policy: EmailPolicy,
    repeat: int,
    model_policy: str,
    policy_profile: str,
    provider: BudgetedProvider,
    resolved: ResolvedModel,
) -> EmailPeopleObservation:
    if settings.deployment_mode is DeploymentMode.PRODUCTION:
        raise ValueError("Email comparison is unavailable in production")
    bootstrap = importlib.import_module("agent_core.bootstrap")
    owner = Principal(
        tenant_id="email-people-evaluation",
        principal_id="synthetic-owner",
        scopes=set(PLATFORM_SCOPES) | {"mcp.gmail_synthetic_read.use"},
        roles={"evaluator"},
    )
    start_calls, start_cost = provider.budget.calls, provider.budget.spent
    event_indices: dict[tuple[UUID, int], int] = {}
    assessments: list[ObservedAssessment] = []
    assessment_calls = failed_runs = 0
    failure_codes: list[str] = []
    async with bootstrap.build(
        settings=replace(
            _evaluation_settings(settings, settings.artifact_root), people_enabled=True
        ),
        storage="memory",
        principal=owner,
        fixed_clock_at=case.evaluated_at,
        sequential_ids=True,
        model_policy=model_policy,
        policy_profile=policy_profile,
        enabled_tools=[],
        enabled_skills=[],
        memory_people_evaluation_mode=True,
        model_provider_overrides={resolved.provider: BorrowedProvider(provider)},
    ) as app:
        if model_policy in NON_ROUTED_MODEL_POLICIES:
            app.executor._model_provider = BorrowedProvider(provider)
            app.executor._resolved_model = resolved
        service = app.services.email
        assert service is not None
        bindings = {header.account_id: {"read": "gmail_synthetic_read"} for header in case.headers}
        service.account_servers = bindings
        semantics = (_EvaluatedEmail if policy == "email-semantic@2" else _EvaluatedBaseline)(
            app.uow_factory,
            app.clock,
            app.ids,
            owner,
            provider=resolved.provider,
            model=resolved.model,
        )
        template_id = await app.sessions.create()
        async with app.uow_factory() as uow:
            template = await uow.sessions.get(template_id, owner)
            for account in bindings:
                await save_value(
                    uow.email,
                    owner,
                    "account",
                    account,
                    EmailAccount(
                        id=account,
                        label=account,
                        status="ready",
                        email_address="owner@example.test",
                    ),
                    app.clock.now(),
                )
        for index, event in sorted(
            enumerate(case.events), key=lambda pair: (pair[1].occurred_at, pair[0])
        ):
            header = case.headers[index]
            sid = app.ids.new_id()
            message_id, thread_id = f"message-{index}", f"thread-{index}"
            message = {
                "id": message_id,
                "thread_id": thread_id,
                "from": header.sender,
                "to": ", ".join(header.to),
                "subject": header.subject,
                "body": event.text,
                "label_ids": header.labels,
                "headers_complete": True,
                "body_complete": True,
                "internal_date": str(int(event.occurred_at.timestamp() * 1000)),
            }
            document = {"thread_id": thread_id, "messages": [message], "complete": True}
            async with app.uow_factory() as uow:
                await uow.sessions.create(
                    template.model_copy(
                        update={
                            "id": sid,
                            "metadata": {
                                "email_operational": True,
                                "email_account_servers": bindings,
                            },
                        }
                    )
                )
                source_event = await uow.events.append(
                    NewEvent(
                        session_id=sid,
                        run_id=None,
                        event_type="tool.call.completed",
                        actor_type="runtime",
                        payload={
                            "name": "mcp.gmail_synthetic_read.get_thread_page",
                            "result_item": ToolResultItem(
                                call_id=f"read-{index}",
                                trust=TrustLevel.EXTERNAL_UNTRUSTED,
                                content=[TextPart(text=json.dumps(document))],
                            ).model_dump(mode="json"),
                        },
                    )
                )
            event_indices[(sid, source_event.sequence)] = index
            source = EmailSemanticSource(
                account_id=header.account_id,
                provider_thread_id=thread_id,
                message_id=message_id,
                session_id=sid,
                source_event_sequence=source_event.sequence,
                tool_name="mcp.gmail_synthetic_read.get_thread_page",
                sender=header.sender,
                body=event.text,
                sent_at=event.occurred_at,
            )
            await semantics.register_source(source)
            thread = await service.import_thread(owner, header.account_id, document, sid)

            async def assess(
                context: RunContext,
                *,
                sid: UUID = sid,
                thread: EmailThread = thread,
                account_id: str = header.account_id,
            ) -> RunOutcome:
                if (context.resolved_model.provider, context.resolved_model.model) != (
                    resolved.provider,
                    resolved.model,
                ):
                    raise ValueError("Email comparison model differs from the pinned tuple")
                task = EmailTask(
                    id=app.ids.new_id(),
                    run_id=context.run.id,
                    session_id=sid,
                    kind="refresh",
                    account_ids=[account_id],
                    created_at=app.clock.now(),
                )
                io = _TaskIO(
                    context, task, StaticToolRegistry(), service, semantics, render_email_context
                )
                await io.assess(thread, await service.learning_context(owner, thread))
                return RunOutcome(
                    kind=OutcomeKind.COMPLETED,
                    final_message=AssistantMessage(
                        content=[TextPart(text="Synthetic assessment completed.")]
                    ),
                )

            app.executor._task_runner = assess
            run_id = await app.runs.submit(
                "Assess the retained synthetic email source.", session_id=sid
            )
            run = await app.runs.get(run_id)
            assessment_calls += run.model_call_count
            failed_runs += int(run.status is not RunStatus.COMPLETED)
            if run.failure is not None:
                failure_codes.append(f"{run.failure.reason.value}:{run.failure.error_class}")
            if provider.budget.failed or provider.budget.held:
                raise ValueError("email replay has unresolved provider accounting")
            async with app.uow_factory() as uow:
                saved = await uow.email.get(owner, "assessment", str(thread.id))
                if saved is not None:
                    assessments.append(
                        ObservedAssessment(
                            event=index,
                            needs_reply=saved.payload["needs_reply"],
                            grounded=saved.payload["grounded"],
                            content_importance=saved.payload["content_importance"],
                        )
                    )
        mentions, facts, sources, organizations = await observe(
            app.uow_factory, owner, event_indices
        )
        draft_failures = old_capture = 0
        async with app.uow_factory() as uow:
            query = PeopleQuery(
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                kinds=["commitment", "interaction", "source"],
                sensitivity_ceiling=Sensitivity.RESTRICTED,
                limit=100,
            )
            while True:
                page = await uow.people.query(query)
                for row in page[:100]:
                    indices = {sources[key] for key in row.support_ids if key in sources}
                    if isinstance(row, PeopleSource):
                        indices.add(sources[row.id])
                        old_capture += int(
                            any(
                                case.evaluated_at - case.events[i].occurred_at > timedelta(days=90)
                                for i in indices
                            )
                        )
                    if indices and all("DRAFT" in case.headers[i].labels for i in indices):
                        draft_failures += int(
                            isinstance(row, PeopleInteraction)
                            or (
                                isinstance(row, PeopleCommitment)
                                and row.state not in {"proposed", "uncertain"}
                            )
                        )
                if len(page) <= 100:
                    break
                query = query.model_copy(update={"after": page[99].id})
    return EmailPeopleObservation(
        policy=policy,
        people=Observation(
            case_id=case.id,
            pipeline="full-people",
            repeat=repeat,
            mentions=mentions,
            facts=facts,
            organizations=organizations,
            tasks=[],
            provider_calls=provider.budget.calls - start_calls,
            cost_usd=format(provider.budget.spent - start_cost, "f"),
        ),
        assessments=assessments,
        assessment_calls=assessment_calls,
        failed_runs=failed_runs,
        failure_codes=failure_codes,
        draft_completion_failures=draft_failures,
        automatic_older_mail_capture=old_capture,
        authority_failures=sum(
            f.authority != MemoryAuthority.INFERRED or f.derivation != MemoryDerivation.HYPOTHESIS
            for f in facts
        ),
    )


async def run_comparison(
    root: Path,
    *,
    output: Path,
    model_policy: str,
    policy_profile: str,
    build_ref: str,
    maximum_cost: Decimal,
    repeats: int = 3,
    development_case: str | None = None,
    settings: Settings | None = None,
) -> dict[str, object]:
    if os.environ.get("RUN_LIVE_MODEL_TESTS") != "1":
        raise ValueError("set RUN_LIVE_MODEL_TESTS=1 to authorize live provider evaluation")
    if not 3 <= repeats <= 10:
        raise ValueError("Email comparison requires three to ten repeats")
    require_committed_tree(root, build_ref)
    development, holdout, digests = load_email_corpora(root)
    cases = [*development.cases, *holdout.cases]
    if development_case is not None:
        cases = [case for case in development.cases if case.id == development_case]
        if not cases:
            raise ValueError("smoke comparisons require an exact development case")
    bootstrap = importlib.import_module("agent_core.bootstrap")
    from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
    from agent_core.config import PACKAGE_ROOT, shipped_policy_version
    from agent_core.memory.email_people_evidence import (
        email_people_implementation_digest,
        email_people_schema_digest,
    )
    from agent_core.model.registry import ProviderRegistry, StaticModelRouter

    with tempfile.TemporaryDirectory(prefix="veetbot-email-people-eval-") as temporary:
        effective = _evaluation_settings(settings or load_settings(), Path(temporary))
        registry = ProviderRegistry.load(
            PACKAGE_ROOT / "models", adapters=ADAPTER_DEFINITIONS, overlay_root=effective.config_dir
        )
        clock = bootstrap.system_clock()
        resolved = await StaticModelRouter(registry, clock).resolve(
            model_policy, tenant_id="email-people-evaluation"
        )
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
        budget = EvaluationBudget(maximum_cost, output / "provider-costs.jsonl")
        metadata: dict[str, Any] = {
            "schema_version": 1,
            "build_ref": build_ref,
            "corpus_sha256": digests,
            "schema_sha256": email_people_schema_digest(),
            "implementation_sha256": email_people_implementation_digest(),
            "model_policy": model_policy,
            "provider": resolved.provider,
            "model": resolved.model,
            "reasoning_configuration": "provider-default",
            "policy_profile": policy_profile,
            "policy_version": shipped_policy_version(policy_profile),
            "repeats": repeats,
            "review_status": [development.review_status, holdout.review_status],
            "pipeline_definitions": {
                "email-semantic@1": "production assessment and attributed semantic formation",
                "email-semantic@2": "production assessment and attributed People formation",
            },
            "ordinary_email_benchmarks_complete": False,
            "activation_evidence": False,
            "maximum_cost_usd": str(maximum_cost),
            "state": "running",
        }
        (output / "run.json").write_text(json.dumps(metadata, sort_keys=True) + "\n")
        observations = []
        try:
            for repeat in range(repeats):
                for case_index, case in enumerate(cases):
                    policies: tuple[EmailPolicy, ...] = ("email-semantic@1", "email-semantic@2")
                    rotation = (case_index + repeat) % len(policies)
                    for policy in (*policies[rotation:], *policies[:rotation]):
                        adapters = bootstrap._provider_adapters(effective, registry)
                        inner = adapters[resolved.provider]
                        for name, unused in adapters.items():
                            if name != resolved.provider:
                                await unused.close()
                        provider = BudgetedProvider(inner, budget)
                        try:
                            observed = await evaluate_email_case(
                                effective,
                                case,
                                policy=policy,
                                repeat=repeat,
                                model_policy=model_policy,
                                policy_profile=policy_profile,
                                provider=provider,
                                resolved=resolved,
                            )
                        finally:
                            await provider.close()
                        observations.append(observed.model_dump(mode="json"))
                        with (output / "observations.jsonl").open("a") as stream:
                            stream.write(observed.model_dump_json() + "\n")
                            stream.flush()
                            os.fsync(stream.fileno())
            path = output / "observations.json"
            path.write_text(json.dumps(observations) + "\n")
            report = (
                score_observations(root, path)
                if development_case is None
                else {
                    "activation_evidence": False,
                    "smoke_only": True,
                    "scores": [
                        score_email_case(cases[0], EmailPeopleObservation.model_validate(row))
                        for row in observations
                    ],
                }
            )
            (output / "scores.json").write_text(json.dumps(report, sort_keys=True) + "\n")
            metadata["state"] = "completed"
        except BaseException as error:
            metadata["state"] = "failed"
            metadata["error_class"] = type(error).__name__
            raise
        finally:
            metadata.update(
                evaluated_at=clock.now().isoformat(),
                provider_calls=budget.calls,
                spent_usd=str(budget.spent),
                reserved_usd=str(budget.held),
            )
            (output / "run.json").write_text(json.dumps(metadata, sort_keys=True) + "\n")
        return metadata
