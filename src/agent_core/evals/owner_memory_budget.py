"""Content-free, process-shared experiment reservations (ADR-0171)."""

import json
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from agent_core.domain.reconsolidation_execution import ReconsolidationCall


class ExperimentBudget:
    """Keep the whole 25-cent reservation, including after failures or crashes.

    No refund is needed for this small experiment. Eight admissions per UTC day
    conservatively enforce USD 2 even when the provider reports no usage.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self._connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS experiments (id TEXT PRIMARY KEY, "
                "owner TEXT NOT NULL, day TEXT NOT NULL, packet TEXT NOT NULL, "
                "cents INTEGER NOT NULL, identity TEXT NOT NULL, receipt TEXT)"
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            yield db

    def reserve(
        self,
        experiment_id: UUID,
        owner_digest: str,
        packet_digest: str,
        now: datetime,
        *,
        identities: tuple[str, str, str] = ("0" * 64, "0" * 64, "0" * 64),
    ) -> None:
        if now.tzinfo is None or any(
            re.fullmatch(r"[0-9a-f]{64}", v) is None
            for v in (owner_digest, packet_digest, *identities)
        ):
            raise ValueError("invalid reservation identity")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM experiments WHERE id=?", (str(experiment_id),)).fetchone():
                raise ValueError("experiment already attempted")
            day = now.astimezone(UTC).date().isoformat()
            spent = db.execute(
                "SELECT COALESCE(SUM(cents),0) FROM experiments WHERE owner=? AND day=?",
                (owner_digest, day),
            ).fetchone()[0]
            if spent + 25 > 200:
                raise ValueError("daily experiment ceiling exhausted")
            db.execute(
                "INSERT INTO experiments VALUES (?,?,?,?,25,?,NULL)",
                (str(experiment_id), owner_digest, day, packet_digest, json.dumps(identities)),
            )

    def reserved_cents(self, owner_digest: str, now: datetime) -> int:
        with self._connection() as db:
            return int(
                db.execute(
                    "SELECT COALESCE(SUM(cents),0) FROM experiments WHERE owner=? AND day=?",
                    (owner_digest, now.astimezone(UTC).date().isoformat()),
                ).fetchone()[0]
            )

    def finish(
        self,
        experiment_id: UUID,
        *,
        outcome: str,
        calls: int,
        receipt_digest: str,
        model_digest: str,
        implementation_digest: str,
        consent_digest: str,
        call_receipts: tuple[ReconsolidationCall, ...] = (),
    ) -> None:
        if (
            outcome not in {"complete", "failed", "cancelled", "timeout"}
            or not 0 <= len(call_receipts) <= calls <= 2
            or any(
                re.fullmatch(r"[0-9a-f]{64}", v) is None
                for v in (receipt_digest, model_digest, implementation_digest, consent_digest)
            )
        ):
            raise ValueError("invalid content-free outcome")
        receipt = json.dumps(
            {
                "outcome": outcome,
                "calls": calls,
                "receipt_digest": receipt_digest,
                "model_digest": model_digest,
                "implementation_digest": implementation_digest,
                "consent_digest": consent_digest,
                "usage": [c.model_dump(mode="json") for c in call_receipts],
                "missing_call_receipts": calls - len(call_receipts),
            }
        )
        with self._connection() as db:
            changed = db.execute(
                "UPDATE experiments SET receipt=? WHERE id=? AND receipt IS NULL",
                (receipt, str(experiment_id)),
            ).rowcount
            if changed != 1:
                raise ValueError("experiment is absent or already finished")
