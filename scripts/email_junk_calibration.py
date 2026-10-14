"""Bounded offline shadow replay and independent calibration validation for email junk.

Inputs and outputs are private files outside the repository. Replay sends only
eligible mail through the existing judgment adapter. It never connects to Gmail
or a database, modifies mail, or treats model agreement as a human label.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from agent_core.adapters.credentials import MappingCredentialResolver
from agent_core.adapters.determinism import SystemClock
from agent_core.adapters.judgment import TypeSafeJudgmentProvider
from agent_core.domain.email import EmailThread
from agent_core.domain.email_junk import (
    JUNK_MODEL,
    JUNK_POLICY,
    JUNK_RESERVATION,
    JUNK_THRESHOLD,
    JunkCalibration,
    junk_fingerprint,
    junk_request,
    junk_result,
)
from agent_core.domain.judgment import JudgmentProviderError


def private_json(path: Path, value: object) -> None:
    """Never overwrite a previous experiment or expose mail through default permissions."""
    with open(path, "x", encoding="utf-8", opener=lambda p, flags: os.open(p, flags, 0o600)) as out:
        json.dump(value, out, indent=2, default=str)


async def replay(source: Path, output: Path, maximum: int, budget: Decimal) -> dict[str, Any]:
    if not budget.is_finite() or not Decimal("0.001") <= budget <= Decimal("1"):
        raise ValueError("budget must be between USD 0.001 and USD 1")
    if not 1 <= maximum <= 1000 or source.stat().st_size > 20_000_000:
        raise ValueError("bounded replay requires 1-1000 rows and an input below 20 MB")
    if output.exists():
        raise ValueError("output already exists; preserve the earlier experiment")
    data = json.loads(source.read_text())
    rows = data["items"]
    clock = SystemClock()
    provider = TypeSafeJudgmentProvider(
        credentials=MappingCredentialResolver({"typesafe": os.environ["TYPESAFE_API_KEY"]}),
        clock=clock,
    )
    report: dict[str, Any] = {
        "policy": JUNK_POLICY,
        "model": JUNK_MODEL,
        "created_at": datetime.now(UTC).isoformat(),
        "cost": "0",
        "skipped": 0,
        "observations": [],
    }
    spent = Decimal(0)
    # A separate append-only attempt journal survives interrupts and uncertain calls.
    journal_path = output.with_suffix(output.suffix + ".attempts.jsonl")
    try:
        with open(
            journal_path, "x", encoding="utf-8", opener=lambda p, flags: os.open(p, flags, 0o600)
        ) as journal:
            for item in rows[:maximum]:
                thread = EmailThread.model_validate(item["thread"])
                learning = item.get("learning", {})
                request = junk_request(thread, learning, clock.now())
                if request is None:
                    report["skipped"] += 1
                    continue
                if spent + JUNK_RESERVATION > budget:
                    break
                fingerprint = junk_fingerprint(request, thread, learning)
                journal.write(
                    json.dumps(
                        {
                            "fingerprint": fingerprint,
                            "status": "started",
                            "reservation": str(JUNK_RESERVATION),
                        }
                    )
                    + "\n"
                )
                journal.flush()
                os.fsync(journal.fileno())
                spent += JUNK_RESERVATION
                report["cost"] = str(spent)
                observed: dict[str, Any] = {
                    "fingerprint": fingerprint,
                    "category": "keep",
                    "probability": 0,
                    "label": None,
                    "source": "owner_review",
                    "stratum": "random",
                    "subject": thread.subject,
                    "sender": thread.senders,
                    "body": "\n\n".join(m.body for m in thread.messages),
                    "baseline_bulk": item.get("baseline_bulk"),
                    "baseline_spam": item.get("baseline_spam"),
                }
                try:
                    result = await provider.judge(request)
                    if result.usage.cost.is_finite() and result.usage.cost >= 0:
                        spent += result.usage.cost - JUNK_RESERVATION
                    observed.update(junk_result(request, result), resolved_model=result.usage.model)
                except JudgmentProviderError as exc:
                    observed["error"] = exc.reason.value
                journal.write(
                    json.dumps(
                        {
                            "fingerprint": fingerprint,
                            "status": "completed",
                            "cumulative_cost": str(spent),
                        }
                    )
                    + "\n"
                )
                journal.flush()
                os.fsync(journal.fileno())
                report["observations"].append(observed)
                report["cost"] = str(spent)
    finally:
        try:
            private_json(output, report)
        finally:
            await provider.close()
    predicted = [
        r
        for r in report["observations"]
        if r["category"] != "keep"
        and r["probability"] >= JUNK_THRESHOLD
        and r.get("resolved_model") == JUNK_MODEL
    ]
    return {
        "screened": len(report["observations"]),
        "skipped": report["skipped"],
        "candidate_junk": len(predicted),
        "cost": report["cost"],
        "admitted": False,
        "reason": "Independent review labels are still required.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="Recompute readiness from a private reviewed packet")
    check.add_argument("packet", type=Path)
    run = sub.add_parser("replay", help="Send a bounded private snapshot to Jev only")
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--max-threads", type=int, default=100)
    run.add_argument("--max-cost-usd", type=Decimal, default=Decimal("0.05"))
    args = parser.parse_args()
    try:
        if args.command == "check":
            packet = JunkCalibration.model_validate_json(args.packet.read_bytes())
            print(json.dumps({"admitted": packet.admitted(), "reviewed": len(packet.observations)}))
        else:
            print(
                json.dumps(
                    asyncio.run(
                        replay(args.input, args.output, args.max_threads, args.max_cost_usd)
                    )
                )
            )
    except (ValueError, OSError, KeyError):
        # Validation errors can quote private source mail; never echo their details.
        parser.exit(2, "Invalid replay inputs, credentials, or private output path.\n")


if __name__ == "__main__":
    main()
