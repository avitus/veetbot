"""Bounded first-pass scheduling signals from Gmail history (ADR-0355)."""

from datetime import datetime

SIGNALS = ("IMPORTANT", "INBOX", "SENT", "SPAM", "TRASH")


def merge_history_signal(
    winner: dict[str, str],
    previous: dict[str, str] | None,
    incoming: dict[str, str],
    label_ids: list[str],
    now: datetime,
) -> dict[str, str]:
    """Fold each label separately. A later edit can arrive before an older addition."""
    prior = previous or {}
    result = {**prior, **winner}
    revision = incoming.get("history_id") or incoming["sequence"]
    if not revision.isdigit():
        revision = incoming["sequence"]
    kind = incoming["kind"]
    delta = set(label_ids)
    for label in (*SIGNALS, "received"):
        if kind not in {"message_added", "message_deleted"} and label not in delta:
            continue
        # A delete wins at the same revision. A label delta is more specific
        # than the label snapshot in an addition at that revision.
        precedence = {
            "message_added": 0,
            "labels_added": 1,
            "labels_removed": 2,
            "message_deleted": 3,
        }[kind]
        mark = f"{revision}:{precedence}"
        key = f"label_{label}"
        before, _, _ = prior.get(key, "").rpartition("=")
        if before and tuple(map(int, before.split(":"))) > (int(revision), precedence):
            continue
        present = kind == "labels_added" or (
            kind == "message_added" and (label in delta or label == "received")
        )
        result[key] = f"{mark}={int(present)}"
    result["labels"] = " ".join(
        label for label in SIGNALS if result.get("label_" + label, "").endswith("=1")
    )
    result["inbound"] = "1" if result.get("label_received", "").endswith("=1") else "0"
    result["removed_inbox"] = "1" if result.get("label_INBOX", "").endswith(":2=0") else "0"
    result["first_sequence"] = prior.get(
        "first_sequence", prior.get("sequence", incoming["sequence"])
    )
    result["first_seen"] = prior.get("first_seen", now.isoformat())
    return result
