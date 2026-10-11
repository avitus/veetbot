"""Task-owned memory lock with write-proportional rollback journals."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator, MutableMapping
from contextlib import asynccontextmanager


class _OwnerLock:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.task: asyncio.Task[object] | None = None
        self.depth = 0

    async def acquire(self) -> None:
        task = asyncio.current_task()
        if self.task is not task:
            await self.lock.acquire()
            self.task = task
        self.depth += 1

    def release(self) -> None:
        if self.task is not asyncio.current_task():
            raise RuntimeError("memory lock exited by a different task")
        self.depth -= 1
        if not self.depth:
            self.task = None
            self.lock.release()


class _TransactionState:
    def __init__(self) -> None:
        self.journal: list[Callable[[], object]] = []
        self.savepoints: list[int] = []
        self.held: dict[tuple[str, str], _OwnerLock] = {}


class MemoryTransaction:
    """Journal touched values and retain only locks for owners actually accessed.

    Transaction entry does not lock memory or block unrelated repositories.
    Ownership uses task identity; child tasks never inherit a parent's journal.
    Nested units of work are savepoints, reversible by their parent.
    """

    def __init__(self) -> None:
        self._locks: dict[tuple[str, str], _OwnerLock] = {}
        self._states: dict[asyncio.Task[object], _TransactionState] = {}

    @asynccontextmanager
    async def owner(self, key: tuple[str, str]) -> AsyncIterator[None]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("memory access requires an asyncio task")
        lock = self._locks.setdefault(key, _OwnerLock())
        state = self._states.get(task)
        if state is not None and key not in state.held:
            await lock.acquire()
            state.held[key] = lock
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("memory transactions require an asyncio task")
        state = self._states.setdefault(task, _TransactionState())
        start = len(state.journal)
        state.savepoints.append(start)
        try:
            yield
        except BaseException:
            for undo in reversed(state.journal[start:]):
                undo()
            del state.journal[start:]
            raise
        finally:
            state.savepoints.pop()
            if not state.savepoints:
                del self._states[task]
                for lock in reversed(state.held.values()):
                    lock.release()

    def remember(self, undo: Callable[[], object]) -> None:
        task = asyncio.current_task()
        state = None if task is None else self._states.get(task)
        if state is not None:
            state.journal.append(undo)

    def mapping[K, V](self) -> MutableMapping[K, V]:
        return _JournalMap(self)

    def append[V](self, values: list[V], value: V) -> None:
        # Audits are shared across owners. Removing precisely this append must
        # preserve a different owner's later committed entry.
        def undo() -> None:
            for index in range(len(values) - 1, -1, -1):
                if values[index] is value:
                    del values[index]
                    return

        self.remember(undo)
        values.append(value)

    def add[V](self, values: set[V], value: V) -> None:
        if value not in values:
            self.remember(lambda: values.discard(value))
            values.add(value)

    def discard[V](self, values: set[V], value: V) -> None:
        if value in values:
            self.remember(lambda: values.add(value))
            values.discard(value)


class _JournalMap[K, V](MutableMapping[K, V]):
    def __init__(self, transaction: MemoryTransaction) -> None:
        self._values: dict[K, V] = {}
        self._transaction = transaction

    def __getitem__(self, key: K) -> V:
        return self._values[key]

    def __setitem__(self, key: K, value: V) -> None:
        if key in self._values:
            previous = self._values[key]
            self._transaction.remember(lambda: self._values.__setitem__(key, previous))
        else:
            self._transaction.remember(lambda: self._values.pop(key, None))
        self._values[key] = value

    def __delitem__(self, key: K) -> None:
        previous = self._values[key]
        self._transaction.remember(lambda: self._values.__setitem__(key, previous))
        del self._values[key]

    def __iter__(self) -> Iterator[K]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)
