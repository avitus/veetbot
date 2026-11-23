"""Current owner and reader authority for offline queries; no egress classification."""

import hashlib
import json
import logging
import re

from agent_core.domain.agents import Principal
from agent_core.domain.memory import Sensitivity
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import Run, RunCheckpoint, RunKind
from agent_core.domain.sessions import Session
from agent_core.memory.semantic_runtime import SemanticQueryAuthority
from agent_core.ports.persistence import UnitOfWorkFactory

logger = logging.getLogger(__name__)
# Distinct readers one authority names in its warning before it stops adding.
_REPORTED_READERS = 16


class LocalQueryAuthority:
    """Admit a run's local query when the owner, run, surface and reader all hold.

    `reader_sha256` is the one reader that quality evidence was measured with;
    a run on any other reader is withheld, and the first such run per reader
    says so in the log. `None` binds no reader: the owner's standing decision
    (ADR-0290) certifies no measurement, so it follows the owner to whichever
    chat model a run selected (ADR-0343).
    """

    def __init__(
        self,
        factory: UnitOfWorkFactory,
        principal: Principal,
        *,
        policy_version: str,
        retrieval_policy: str,
        reader_sha256: str | None,
        interactive_only: bool = False,
    ) -> None:
        if reader_sha256 is not None and re.fullmatch(r"[a-f0-9]{64}", reader_sha256) is None:
            raise ValueError("Local query authority needs a reviewed reader or none at all")
        self._factory, self._principal = factory, principal.model_copy(deep=True)
        self._policy_version, self._retrieval_policy = policy_version, retrieval_policy
        self._reader_sha256 = reader_sha256
        self._interactive_only = interactive_only
        self._reported: set[object] = set()

    async def __call__(
        self, run: Run, session: Session, checkpoint: RunCheckpoint
    ) -> SemanticQueryAuthority | None:
        if (
            (
                self._interactive_only
                and (run.kind is not RunKind.INTERACTIVE or run.parent_run_id is not None)
            )
            or (session.tenant_id, session.principal_id)
            != (self._principal.tenant_id, self._principal.principal_id)
            or run.tenant_id != self._principal.tenant_id
            or run.session_id != session.id
            or checkpoint.run_id != run.id
            or not run.principal_scopes <= self._principal.scopes
        ):
            return None
        selected = checkpoint.budget_state.get("context_reader_sha256")
        if self._reader_sha256 is not None and selected != self._reader_sha256:
            # Otherwise eligible, and withheld only because the chat model, its
            # effort or the model registry is no longer the reviewed one. That
            # used to pass in silence; neither digest is content.
            marker = selected if isinstance(selected, str) else None
            if marker not in self._reported and len(self._reported) < _REPORTED_READERS:
                self._reported.add(marker)
                logger.warning(
                    "semantic_recall_withheld_reader_changed",
                    extra={"reviewed_reader": self._reader_sha256, "run_reader": marker},
                )
            return None
        transport: dict[str, object] = {"kind": "private"}
        async with self._factory() as uow:
            seeds = await uow.events.list_after(
                session.id, run.seed_event_sequence - 1, self._principal, limit=1
            )
            if not seeds:
                return None
            seed = seeds[0]
            if (
                seed.sequence != run.seed_event_sequence
                or seed.run_id != run.id
                or seed.event_type != "user.message.created"
                or seed.actor_type != "principal"
                or seed.actor_id != self._principal.principal_id
                or seed.payload.get("trust", TrustLevel.USER.value) != TrustLevel.USER.value
            ):
                return None
            mapping = await uow.surfaces.sessions.for_session(session.id)
            origin = seed.payload.get("origin")
            if origin is None:
                if mapping is not None:
                    return None
            else:
                if (
                    not isinstance(origin, dict)
                    or origin.get("kind") not in {"telegram", "whatsapp"}
                    or mapping is None
                    or mapping.rotated_at is not None
                    or str(mapping.surface_id) != origin.get("surface_id")
                    or (mapping.tenant_id, mapping.principal_id)
                    != (self._principal.tenant_id, self._principal.principal_id)
                ):
                    return None
                pairing = await uow.surfaces.pairings.live_pairing(
                    mapping.surface_id, mapping.external_key.removeprefix("dm:")
                )
                if (
                    pairing is None
                    or pairing.revoked_at is not None
                    or (pairing.tenant_id, pairing.principal_id)
                    != (self._principal.tenant_id, self._principal.principal_id)
                    or not run.principal_scopes <= pairing.granted_scopes
                ):
                    return None
                transport = {
                    "kind": origin["kind"],
                    "mapping": mapping.model_dump(mode="json", exclude={"last_inbound_at"}),
                    "pairing": pairing.model_dump(
                        mode="json", exclude={"last_message_at", "sender_label"}
                    ),
                }
        # A bound authority keeps its `@1` revision. One bound to no reader
        # names that instead, so a model change leaves prepared queries bound.
        revision = hashlib.sha256(
            json.dumps(
                ["local-query-authority@1", self._retrieval_policy, self._reader_sha256, transport]
                if self._reader_sha256 is not None
                else ["local-query-authority@2", self._retrieval_policy, "any-reader", transport],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return SemanticQueryAuthority(
            policy_version=self._policy_version,
            retrieval_policy=revision,
            # The private recall view covers authenticated owner chats and DMs.
            # Its actual transport/pairing is independently bound above.
            surface_id="private",
            sensitivity_floor=Sensitivity.RESTRICTED,
        )
