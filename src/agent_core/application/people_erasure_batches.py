"""Bounded durable erasure manifests; no source content is copied into receipts."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID, uuid5

from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import PeopleErasure
from agent_core.ports.people import PeopleStore

BATCH_SIZE = 256


@dataclass
class ErasureManifest:
    expected_revisions: dict[UUID, int] = field(default_factory=dict)
    belief_positions: dict[UUID, int] = field(default_factory=dict)
    source_sessions: list[UUID] = field(default_factory=list)
    blocked_source_ids: list[UUID] = field(default_factory=list)
    blocked_record_ids: list[UUID] = field(default_factory=list)
    blocked_belief_ids: list[UUID] = field(default_factory=list)
    pending_belief_ids: list[UUID] = field(default_factory=list)
    pending_copy_ids: list[UUID] = field(default_factory=list)
    pending_run_ids: list[UUID] = field(default_factory=list)
    pending_artifact_ids: list[UUID] = field(default_factory=list)

    def page(self, index: int, size: int) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in vars(self).items():
            if isinstance(value, dict):
                result[key] = dict(sorted(value.items())[index * size : (index + 1) * size])
            else:
                result[key] = sorted(set(value))[index * size : (index + 1) * size]
        return result


def batch_id(root_id: UUID, index: int) -> UUID:
    return uuid5(root_id, f"erasure-batch:{index}")


async def read_manifest(
    store: PeopleStore, principal: Principal, root: PeopleErasure
) -> ErasureManifest:
    manifest = ErasureManifest()
    for index in range(root.batch_count or 1):
        part = (
            root
            if not root.batch_count
            else await store.get(
                principal, batch_id(root.id, index), ceiling=Sensitivity.RESTRICTED
            )
        )
        if not isinstance(part, PeopleErasure) or (
            root.batch_count and (part.parent_id != root.id or part.state != root.state)
        ):
            raise ConflictError("People erasure manifest is incomplete")
        for key, value in vars(manifest).items():
            entries = getattr(part, key)
            if isinstance(value, dict):
                value.update(entries)
            else:
                value.extend(entries)
    return manifest


async def write_manifest(
    store: PeopleStore,
    root: PeopleErasure,
    manifest: ErasureManifest,
    *,
    now: datetime,
    expected_revision: int,
) -> PeopleErasure:
    largest = max(len(value) for value in vars(manifest).values())
    count = max(root.batch_count, (largest + BATCH_SIZE - 1) // BATCH_SIZE)
    if count <= 1 and not root.batch_count:
        root = root.model_copy(update=manifest.page(0, BATCH_SIZE))
    else:
        owner = Principal(
            tenant_id=root.tenant_id, principal_id=root.principal_id, roles=set(), scopes=set()
        )
        pending = 0
        for index in range(count):
            old = await store.get(owner, batch_id(root.id, index), ceiling=Sensitivity.RESTRICTED)
            if old is not None and (not isinstance(old, PeopleErasure) or old.parent_id != root.id):
                raise ConflictError("People erasure batch identity changed")
            values = manifest.page(index, BATCH_SIZE)
            state = root.state
            if state != "preview":
                state = (
                    "cleanup_pending"
                    if (
                        values["pending_belief_ids"]
                        or values["pending_copy_ids"]
                        or values["pending_run_ids"]
                        or values["pending_artifact_ids"]
                    )
                    else "completed"
                )
            pending += int(state == "cleanup_pending")
            revision = 1 if old is None else old.revision + 1
            part = root.model_copy(
                update={
                    **values,
                    "id": batch_id(root.id, index),
                    "parent_id": root.id,
                    "pending_people": False,
                    "pending_generated": False,
                    "pending_email": False,
                    "pending_episodes": False,
                    "batch_count": 0,
                    "cleanup_remaining": 0,
                    "cleanup_after_batch": 0,
                    "state": state,
                    "counts": {},
                    "revision": revision,
                    "created_at": root.created_at if old is None else old.created_at,
                    "updated_at": now
                    if old is None
                    else max(now, old.updated_at + timedelta(microseconds=1)),
                }
            )
            await store.put(part, expected_revision=revision - 1)
        root = root.model_copy(
            update={
                **ErasureManifest().page(0, BATCH_SIZE),
                "batch_count": count,
                "cleanup_remaining": pending,
                "cleanup_after_batch": 0,
            }
        )
    return await _put_root(store, root, expected_revision)


async def _put_root(store: PeopleStore, root: PeopleErasure, expected: int) -> PeopleErasure:
    stored = await store.put(root, expected_revision=expected)
    assert isinstance(stored, PeopleErasure)
    return stored
