"""Code authoring keeps the trust overlay; widened access needs fresh approval."""

from typing import Any
from uuid import UUID

import pytest
import yaml

from agent_core.application.job_grants import JobGrantAuthorizer
from agent_core.domain.policies import (
    AuthorizationTurn,
    PolicyDecisionType,
    SideEffectClass,
    TrustLevel,
)
from agent_core.policy.engine import DeterministicPolicyEngine
from agent_core.policy.jobs import JobPolicyEngine, job_policy_version, job_publication_ruleset
from agent_core.policy.loader import DEFAULT_RULESET, POLICY_DIRECTORY, load_ruleset_documents
from tests.contract.support import NOW, memory_uow_factory, principal, run
from tests.contract.test_job_store_contract import record
from tests.unit.test_policy_tool_rules import _action


@pytest.mark.parametrize(
    "trust,expected",
    [
        (TrustLevel.USER, PolicyDecisionType.ALLOW),
        (TrustLevel.EXTERNAL_UNTRUSTED, PolicyDecisionType.REQUIRE_APPROVAL),
    ],
)
async def test_job_publication_retains_untrusted_content_approval(
    trust: TrustLevel, expected: PolicyDecisionType
) -> None:
    action = _action(name="job.manage", origin_trust=trust, argument_trust=trust).model_copy(
        update={"side_effect": SideEffectClass.PUBLICATION}
    )
    engine = JobPolicyEngine(
        DeterministicPolicyEngine(DEFAULT_RULESET),
        DeterministicPolicyEngine(job_publication_ruleset(DEFAULT_RULESET)),
    )
    decision = await engine.evaluate(action, principal(), run())
    assert decision.decision is expected
    assert decision.policy_version != DEFAULT_RULESET.policy_version
    unrelated = await engine.evaluate(
        action.model_copy(update={"name": "other.publish"}), principal(), run()
    )
    assert unrelated.policy_version == DEFAULT_RULESET.policy_version
    assert unrelated.decision is PolicyDecisionType.REQUIRE_APPROVAL


def _edit(profile: dict[str, Any], hardline: dict[str, Any], section: str) -> None:
    if section == "name":
        profile["name"] = "strict"
    elif section == "default_effect":
        profile["default_effect"] = "require_approval"
    elif section == "rules":
        profile["rules"]["publication"] = {"decision": "deny"}
    elif section == "tool_rules":
        profile["tool_rules"]["job.run"] = {"decision": "require_approval"}
    elif section == "trust_overlay":
        profile["trust_overlay"] = {"external_untrusted_requires_approval": False}
    elif section == "approval_expiry_seconds":
        profile["approval_expiry_seconds"]["medium"] = 3600
    elif section == "self_approval":
        profile["self_approval"] = {"enabled": False}
    elif section == "advisory":
        profile["advisory"] = {"enabled": True}
    else:
        hardline["rules"] = hardline["rules"][:-1]


@pytest.mark.parametrize(
    "section",
    [
        "name",
        "default_effect",
        "rules",
        "tool_rules",
        "trust_overlay",
        "approval_expiry_seconds",
        "self_approval",
        "advisory",
        "hardline",
    ],
)
def test_a_policy_edit_moves_the_base_and_the_jobs_policy_version(section: str) -> None:
    """A standing grant pinned to either profile's version sees every policy edit."""

    def documents() -> tuple[dict[str, Any], dict[str, Any]]:
        profile, hardline = (
            yaml.safe_load((POLICY_DIRECTORY / name).read_text())
            for name in ("default.yaml", "hardline.yaml")
        )
        return profile, hardline

    before = load_ruleset_documents(*documents())
    profile, hardline = documents()
    _edit(profile, hardline, section)
    after = load_ruleset_documents(profile, hardline)
    for tool_name in ("job.manage", "cockpit.install", "job.run", "schedule.update"):
        assert job_policy_version(
            tool_name, base=before, publication=job_publication_ruleset(before)
        ) != job_policy_version(tool_name, base=after, publication=job_publication_ruleset(after))


def test_job_policy_version_names_the_profile_that_judges_each_tool() -> None:
    publication = job_publication_ruleset(DEFAULT_RULESET)
    versions = {
        name: job_policy_version(name, base=DEFAULT_RULESET, publication=publication)
        for name in ("job.manage", "cockpit.install", "job.run", "schedule.update")
    }
    assert versions["job.manage"] == versions["cockpit.install"] == publication.policy_version
    assert versions["job.run"] == versions["schedule.update"] == DEFAULT_RULESET.policy_version


async def test_widened_access_and_foreign_grants_do_not_receive_standing_authority() -> None:
    _, factory = await memory_uow_factory()
    async with factory() as uow:
        await uow.jobs.put(record(principal()), expected_version=None)
    authorizer = JobGrantAuthorizer(factory)
    for extra in (
        {"access": {"inputs": {}, "hosts": {"example.com": None}}},
        {"grant_id": str(UUID(int=999))},
    ):
        action = _action(name="job.run", argument_trust=TrustLevel.USER).model_copy(
            update={
                "arguments": {"slug": "count-items", "revision_id": str(UUID(int=1)), **extra},
                "side_effect": SideEffectClass.EXTERNAL_WRITE,
            }
        )
        decision = await DeterministicPolicyEngine(DEFAULT_RULESET).evaluate(
            action, principal(), run()
        )
        result = await authorizer.authorize(
            action=action,
            decision=decision,
            principal=principal(),
            run=run(),
            agent_version="1",
            action_deadline=NOW,
            turn=AuthorizationTurn(newest_user_trust=TrustLevel.USER),
        )
        assert not result.allowed


@pytest.mark.parametrize(
    "feeds,expected", [(frozenset(), False), (frozenset({"agent.counts"}), True)]
)
def test_first_feed_grant_needs_approval_but_local_computation_does_not(
    feeds: frozenset[str], expected: bool
) -> None:
    from agent_core.application.job_grants import access_needs_approval
    from agent_core.domain.jobs import JobAccess

    saved = record(principal()).model_copy(update={"grants": {}})
    assert access_needs_approval(JobAccess({}, {}, feeds=feeds), saved) is expected


@pytest.mark.parametrize("operation", ["read", "trial"])
@pytest.mark.parametrize("trust", [TrustLevel.MEMORY, TrustLevel.EXTERNAL_UNTRUSTED])
async def test_owner_preparation_does_not_repeat_publication_approval(
    operation: str, trust: TrustLevel
) -> None:
    """Allow owner-directed reads and offline trials after untrusted results."""
    _, factory = await memory_uow_factory()
    action = _action(name="job.manage", origin_trust=trust, argument_trust=trust).model_copy(
        update={"arguments": {"operation": operation}, "side_effect": SideEffectClass.PUBLICATION}
    )
    decision = await DeterministicPolicyEngine(job_publication_ruleset(DEFAULT_RULESET)).evaluate(
        action, principal(), run()
    )
    result = await JobGrantAuthorizer(factory).authorize(
        action=action,
        decision=decision,
        principal=principal(),
        run=run(),
        agent_version="1",
        action_deadline=NOW,
        turn=AuthorizationTurn(newest_user_trust=TrustLevel.USER),
    )
    assert result.allowed, "Reading or preparing code must not ask to publish it."


@pytest.mark.parametrize("mutation", ["denied", "untrusted_user", "delegated", "publication"])
async def test_preparation_authority_does_not_escape_its_boundary(mutation: str) -> None:
    """Refuse preparation exceptions for denials, untrusted users or non-owner runs."""
    from agent_core.domain.runs import RunKind

    _, factory = await memory_uow_factory()
    action = _action(name="job.manage", origin_trust=TrustLevel.EXTERNAL_UNTRUSTED).model_copy(
        update={
            "arguments": {"operation": "create" if mutation == "publication" else "trial"},
            "side_effect": SideEffectClass.PUBLICATION,
        }
    )
    current = run()
    if mutation == "delegated":
        current = current.model_copy(update={"kind": RunKind.DELEGATED})
    decision = await DeterministicPolicyEngine(job_publication_ruleset(DEFAULT_RULESET)).evaluate(
        action, principal(), current
    )
    if mutation == "denied":
        decision = decision.model_copy(update={"decision": PolicyDecisionType.DENY})
    result = await JobGrantAuthorizer(factory).authorize(
        action=action,
        decision=decision,
        principal=principal(),
        run=current,
        agent_version="1",
        action_deadline=NOW,
        turn=AuthorizationTurn(
            newest_user_trust=(
                TrustLevel.EXTERNAL_UNTRUSTED if mutation == "untrusted_user" else TrustLevel.USER
            )
        ),
    )
    assert not result.allowed


@pytest.mark.parametrize("changed", [False, True])
async def test_live_trial_reuses_only_the_exact_owned_grant_inputs(changed: bool) -> None:
    """Allow an existing grant after untrusted results only with its original inputs."""
    _, factory = await memory_uow_factory()
    async with factory() as uow:
        await uow.jobs.put(record(principal()), expected_version=None)
    action = _action(name="job.manage", origin_trust=TrustLevel.EXTERNAL_UNTRUSTED).model_copy(
        update={
            "arguments": {
                "operation": "trial",
                "slug": "count-items",
                "grant_id": str(UUID(int=2)),
                "inputs": {"items": [99] if changed else [1, 2]},
            },
            "side_effect": SideEffectClass.PUBLICATION,
        }
    )
    decision = await DeterministicPolicyEngine(job_publication_ruleset(DEFAULT_RULESET)).evaluate(
        action, principal(), run()
    )
    result = await JobGrantAuthorizer(factory).authorize(
        action=action,
        decision=decision,
        principal=principal(),
        run=run(),
        agent_version="1",
        action_deadline=NOW,
        turn=AuthorizationTurn(newest_user_trust=TrustLevel.USER),
    )
    assert result.allowed is not changed


async def test_external_grant_cannot_silently_authorize_a_different_revision() -> None:
    """Publication does not transfer a prior revision's external execution approval."""
    from dataclasses import replace

    _, factory = await memory_uow_factory()
    saved = record(principal())
    grant = saved.grants[str(UUID(int=2))]
    grant = grant.model_copy(
        update={
            "access": replace(grant.access, hosts={"api.github.com": "github-reader"}),
            "approval_id": UUID(int=99),
        }
    )
    saved = saved.model_copy(update={"grants": {str(grant.id): grant}})
    async with factory() as uow:
        await uow.jobs.put(saved, expected_version=None)
    action = _action(name="job.run", argument_trust=TrustLevel.USER).model_copy(
        update={
            "arguments": {
                "slug": saved.slug,
                "revision_id": str(UUID(int=1)),
                "grant_id": str(grant.id),
            },
            "side_effect": SideEffectClass.EXTERNAL_WRITE,
        }
    )
    decision = await DeterministicPolicyEngine(DEFAULT_RULESET).evaluate(action, principal(), run())
    result = await JobGrantAuthorizer(factory).authorize(
        action=action,
        decision=decision,
        principal=principal(),
        run=run(),
        agent_version="1",
        action_deadline=NOW,
        turn=AuthorizationTurn(newest_user_trust=TrustLevel.USER),
    )
    assert not result.allowed, "A grant approved for other code must not bypass approval."


async def test_live_trial_requires_execution_authority_even_for_owner_authored_code() -> None:
    """The publication default cannot approve live execution of arbitrary trial code."""
    engine = JobPolicyEngine(
        DeterministicPolicyEngine(DEFAULT_RULESET),
        DeterministicPolicyEngine(job_publication_ruleset(DEFAULT_RULESET)),
    )
    action = _action(
        name="job.manage", origin_trust=TrustLevel.USER, argument_trust=TrustLevel.USER
    ).model_copy(
        update={
            "arguments": {"operation": "trial", "grant_id": str(UUID(int=2))},
            "side_effect": SideEffectClass.PUBLICATION,
        }
    )
    decision = await engine.evaluate(action, principal(), run())
    assert decision.decision is PolicyDecisionType.REQUIRE_APPROVAL


def test_old_feed_grant_does_not_approve_new_execution_authority() -> None:
    """An unrelated earlier feed grant is not approval for newly supplied code."""
    from dataclasses import replace

    from agent_core.application.job_grants import access_needs_approval

    saved = record(principal())
    grant = saved.grants[str(UUID(int=2))]
    access = replace(grant.access, feeds=frozenset({"agent.counts"}))
    saved = saved.model_copy(
        update={"grants": {str(grant.id): grant.model_copy(update={"access": access})}}
    )
    assert access_needs_approval(access, saved)
