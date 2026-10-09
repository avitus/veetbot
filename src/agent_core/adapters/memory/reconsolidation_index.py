"""Insertion and change journals, updated under the original memory lock."""

from collections.abc import Callable, MutableMapping
from datetime import datetime
from uuid import UUID

from agent_core.adapters.memory.transactions import MemoryTransaction
from agent_core.domain.derived_memory import SummaryWriteReceipt
from agent_core.domain.memory import MemoryRecord
from agent_core.domain.reconsolidation import SourceChange, SourceVersion, content_signature
from agent_core.domain.reconsolidation_merge import MergeUndoReceipt
from agent_core.domain.reconsolidation_operations import StoredOperation
from agent_core.domain.reconsolidation_summary import SummaryMemory
from agent_core.ports.determinism import Clock


class ReconsolidationIndex:
    def __init__(self, transaction: MemoryTransaction) -> None:
        self.transaction = transaction
        self._clock: Clock | None = None
        self._position: Callable[[], int] | None = None
        self.operations: MutableMapping[UUID, StoredOperation] = transaction.mapping()
        self.summaries: MutableMapping[UUID, SummaryMemory] = transaction.mapping()
        self.dependencies: MutableMapping[tuple[str, str, UUID], frozenset[UUID]] = (
            transaction.mapping()
        )
        self.members: MutableMapping[tuple[str, str, UUID], UUID] = transaction.mapping()
        self.blocks: MutableMapping[tuple[str, str, str], datetime] = transaction.mapping()
        self.receipts: MutableMapping[tuple[str, str, str], MergeUndoReceipt] = (
            transaction.mapping()
        )
        self.summary_receipts: MutableMapping[tuple[str, str, str], SummaryWriteReceipt] = (
            transaction.mapping()
        )
        self.history: MutableMapping[tuple[UUID, int], StoredOperation] = transaction.mapping()
        self.historical_members: MutableMapping[tuple[str, str, UUID], frozenset[UUID]] = (
            transaction.mapping()
        )
        self.versions: MutableMapping[UUID, SourceVersion] = transaction.mapping()
        self.version_history: MutableMapping[UUID, list[tuple[datetime, SourceVersion]]] = (
            transaction.mapping()
        )
        self.owners: MutableMapping[UUID, tuple[str, str]] = transaction.mapping()
        self.events: MutableMapping[tuple[str, str, UUID, int], set[UUID]] = transaction.mapping()
        self.source_keys: MutableMapping[UUID, frozenset[tuple[str, str, UUID, int]]] = (
            transaction.mapping()
        )
        self.signatures: MutableMapping[UUID, str] = transaction.mapping()
        self.creations: MutableMapping[tuple[str, str], list[UUID]] = transaction.mapping()
        self.changes: MutableMapping[tuple[str, str], list[SourceChange]] = transaction.mapping()

    def record(self, record: MemoryRecord, *, erased: bool = False) -> SourceVersion:
        owner = (record.tenant_id, record.principal_id)
        previous = self.versions.get(record.id)
        signature = "erased" if erased else content_signature(record)
        if previous is not None and self.signatures[record.id] == signature:
            return previous
        self.invalidate(owner, record.id)
        keys = (
            frozenset()
            if erased
            else frozenset((*owner, record.source_session_id, n) for n in record.source_event_ids)
        )
        old_keys = self.source_keys.get(record.id, frozenset())
        for key in old_keys - keys:
            self.transaction.discard(self.events[key], record.id)
        for key in keys - old_keys:
            self.transaction.add(self.events.setdefault(key, set()), record.id)
        self.source_keys[record.id] = keys
        self.owners[record.id] = owner
        if previous is None:
            self.transaction.append(self.creations.setdefault(owner, []), record.id)
        version = SourceVersion(
            belief_id=record.id,
            content_revision=1 if previous is None else previous.content_revision + 1,
            creation_sequence=(
                len(self.creations[owner]) if previous is None else previous.creation_sequence
            ),
        )
        self.versions[record.id] = version
        self._remember_version(version)
        self.signatures[record.id] = signature
        self.transaction.append(
            self.changes.setdefault(owner, []),
            SourceChange(
                sequence=len(self.changes.get(owner, [])) + 1,
                source=version,
                reason="erased" if erased else "created" if previous is None else "changed",
            ),
        )
        return version

    def attribution_changed(
        self, owner: tuple[str, str], belief_ids: set[UUID], events: set[tuple[UUID, int]]
    ) -> None:
        affected = set(belief_ids)
        for session_id, sequence in events:
            affected.update(self.events.get((*owner, session_id, sequence), set()))
        for key in sorted(affected):
            if self.owners.get(key) != owner or self.signatures.get(key) == "erased":
                continue
            self.invalidate(owner, key)
            previous = self.versions[key]
            version = previous.model_copy(
                update={"content_revision": previous.content_revision + 1}
            )
            self.versions[key] = version
            self._remember_version(version)
            self.transaction.append(
                self.changes.setdefault(owner, []),
                SourceChange(
                    sequence=len(self.changes.get(owner, [])) + 1,
                    source=version,
                    reason="changed",
                ),
            )

    def bind_memory(self, clock: Clock, position: Callable[[], int]) -> None:
        """Use the injected memory repository's clock and existing allocator."""
        self._clock = clock
        self._position = position

    def _remember_version(self, version: SourceVersion) -> None:
        assert self._clock is not None
        self.transaction.append(
            self.version_history.setdefault(version.belief_id, []), (self._clock.now(), version)
        )

    def version_at(self, belief_id: UUID, known_at: datetime | None) -> SourceVersion | None:
        if known_at is None:
            return self.versions.get(belief_id)
        return next(
            (
                value
                for at, value in reversed(self.version_history.get(belief_id, []))
                if at <= known_at
            ),
            None,
        )

    def save(self, operation: StoredOperation) -> None:
        self.operations[operation.id] = operation
        self.history[operation.id, operation.revision] = operation
        owner = (operation.plan.tenant_id, operation.plan.principal_id)
        if operation.kind in {"summary", "hypothesis"} and operation.state != "committed":
            self.summaries.pop(operation.id, None)
        for member in operation.plan.member_ids:
            dependency_key = (*owner, member)
            if operation.state == "committed":
                self.dependencies[dependency_key] = self.dependencies.get(
                    dependency_key, frozenset()
                ) | {operation.id}
            else:
                self.dependencies[dependency_key] = self.dependencies.get(
                    dependency_key, frozenset()
                ) - {operation.id}
            if operation.kind != "merge":
                continue
            key = (*owner, member)
            self.historical_members[key] = self.historical_members.get(key, frozenset()) | {
                operation.id
            }
            if operation.state == "committed":
                self.members[*owner, member] = operation.id
            elif self.members.get((*owner, member)) == operation.id:
                del self.members[*owner, member]

    def invalidate(self, owner: tuple[str, str], belief_id: UUID) -> None:
        for operation_id in sorted(self.dependencies.get((*owner, belief_id), frozenset())):
            assert self._clock is not None and self._position is not None
            operation = self.operations[operation_id]
            self.save(
                operation.model_copy(
                    update={
                        "state": "invalidated",
                        "revision": operation.revision + 1,
                        "invalidated_at": max(self._clock.now(), operation.committed_at),
                        "store_position": self._position(),
                        "reason": "source_changed",
                    }
                )
            )
