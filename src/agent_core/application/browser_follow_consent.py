"""One observed X Follow action authorized by its owner's explicit chat request."""

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser import BrowserAction, BrowserDispatchConstraint, BrowserProfileStatus
from agent_core.domain.browser_act_views import describe_browser_action, element_labels
from agent_core.domain.browser_follow import exact_follow_control
from agent_core.domain.events import NewEvent
from agent_core.domain.policies import (
    AuthorizationTurn,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    StandingAuthorization,
)
from agent_core.domain.runs import Run, RunKind
from agent_core.ports.browser import BrowserProvider, browser_snapshot_in_session
from agent_core.ports.persistence import UnitOfWorkFactory


class BrowserFollowConsentAuthorizer:
    def __init__(
        self,
        *,
        provider: BrowserProvider,
        uow_factory: UnitOfWorkFactory,
        now: Callable[[], datetime],
    ) -> None:
        self._provider, self._uow_factory, self._now = provider, uow_factory, now

    async def authorize(
        self,
        *,
        action: ProposedAction,
        decision: PolicyDecision,
        principal: Principal,
        run: Run,
        agent_version: str,
        action_deadline: datetime,
        turn: AuthorizationTurn | None = None,
    ) -> StandingAuthorization:
        del agent_version, turn
        refused = StandingAuthorization(allowed=False, reason_code="browser.follow.no_consent")
        if (
            action.name != "browser.act"
            or action.target.kind != "browser_provider"
            or decision.decision is not PolicyDecisionType.REQUIRE_APPROVAL
            or run.kind is not RunKind.INTERACTIVE
            or run.parent_run_id is not None
            or "run.write" not in run.principal_scopes
        ):
            return refused
        try:
            browser_action = BrowserAction.model_validate(
                {k: v for k, v in action.arguments.items() if k != "postcondition"}
            )
        except ValueError:
            return refused
        snapshot = await browser_snapshot_in_session(self._provider, run.session_id)
        view = describe_browser_action(browser_action, snapshot)
        if not view.described or view.observation is None or view.element is None:
            return refused
        now = self._now()
        async with (
            self._uow_factory() as uow,
            uow.sessions.admission(run.session_id, principal) as session,
        ):
            seed = await uow.events.latest_before(
                run.session_id, run.seed_event_sequence + 1, "user.message.created", principal
            )
            newest = await uow.events.latest_before(
                run.session_id, (1 << 63) - 1, "user.message.created", principal
            )
            if (
                seed is None
                or newest is None
                or seed.id != newest.id
                or seed.run_id != run.id
                or seed.actor_type != "principal"
                or seed.actor_id != principal.principal_id
                or seed.created_at + timedelta(minutes=30) <= now
            ):
                return refused
            handle = seed.payload.get("browser_follow_target")
            if not isinstance(handle, str) or not exact_follow_control(
                handle,
                action=browser_action,
                page_url=view.observation.url,
                role=view.element.role,
                labels=element_labels(view.element, view.facts),
                facts=view.facts,
                disabled=view.element.disabled,
            ):
                return refused
            selected = session.metadata.get("browser_profile_id")
            if not isinstance(selected, str) or "browser.profile.read" not in principal.scopes:
                return refused
            profile = await uow.browser_profiles.get(UUID(selected), principal)
            if (
                profile.status is not BrowserProfileStatus.READY
                or "https://x.com" not in profile.allowed_origins
            ):
                return refused
            key = f"browser.follow.consumed:{run.id}:{seed.sequence}"
            receipt = await uow.events.get_by_derivation(key, principal)
            if receipt is not None:
                # An invocation is never authorized a second time, even if its
                # transport failed before returning an outcome.
                return refused
            receipt = await uow.events.append(
                NewEvent(
                    session_id=run.session_id,
                    run_id=run.id,
                    event_type="browser.follow.consumed",
                    actor_type="application",
                    actor_id=principal.principal_id,
                    payload={
                        "handle": handle,
                        "source_sequence": seed.sequence,
                        "invocation_id": str(action.action_id),
                        "profile_id": selected,
                        "profile_generation": profile.generation,
                    },
                    derivation_key=key,
                )
            )
        return StandingAuthorization(
            allowed=True,
            reason_code="browser.follow.owner_request",
            authorization_kind="owner_action",
            authorization_ref=str(receipt.id),
            authorization_view={"action": "follow", "handle": handle, "profile_id": selected},
            dispatch_constraint=BrowserDispatchConstraint(
                grant_kind="follow",
                origins=("https://x.com",),
                path_prefix=f"/{handle}",
                not_after=min(action_deadline, seed.created_at + timedelta(minutes=30)),
                consequence_ceiling="unknown",
                max_text_characters=None,
                follow_handle=handle,
            ),
        )
