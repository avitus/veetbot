"""Generate the ADR-0222 owner packet from hash-bound retained inputs, offline."""

import argparse
import hashlib
import html
import json
from pathlib import Path
from typing import Any

from agent_core.memory.bounded_recall_admission import (
    APPROVED_ITEMS_SHA256,
    APPROVED_OBSERVATIONS_SHA256,
    APPROVED_SCOPE_SHA256,
)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def finding(key: str) -> str:
    return (
        f'<fieldset data-finding="{escape(key)}"><legend>Your finding</legend>'
        '<label>Assessment <select class="verdict" required>'
        '<option value="">Choose after reviewing</option>'
        '<option value="acceptable">Acceptable for the bounded rollout</option>'
        '<option value="concern">Concern or required mitigation</option>'
        '<option value="unacceptable">Unacceptable</option></select></label>'
        '<label>Notes <textarea class="notes" rows="2" '
        'placeholder="Required for a concern or unacceptable result."></textarea></label>'
        "</fieldset>"
    )


def render(data: dict[str, Any], scope: dict[str, Any]) -> str:
    """Render original evidence and empty finding controls; never infer verdicts."""
    cases = {g["case"]["id"]: g for g in data["cases"]}
    selected = scope["proposed_individual_scenarios"]
    groups = scope["interruption_summary_groups"]
    ordered = list(scope["actual_obsolete_answer_scenarios"])
    ordered += sorted(set(scope["original_strict_regression_scenarios"]) - set(ordered))
    ordered += sorted(set(selected) - set(ordered))
    parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>37-case recall review</title><style>"
        "body{font:16px/1.55 system-ui;max-width:1080px;margin:32px auto;padding:0 20px;"
        "background:#f5f7fa;color:#172b3d}h1{font-size:32px}details,section{background:white;"
        "border:1px solid #cbd5df;border-radius:10px;margin:14px 0;padding:18px}"
        "summary{cursor:pointer;font-weight:650}table{border-collapse:collapse;width:100%}"
        "td,th{border:1px solid #d2dce5;padding:10px;text-align:left;vertical-align:top}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.5 monospace}"
        "label{display:block;margin:12px 0}textarea,input,select{box-sizing:border-box;"
        "font:inherit;max-width:100%;padding:8px}textarea{width:100%}fieldset{margin-top:18px;"
        "border:1px solid #9eb7ca;border-radius:8px}button{font:inherit;padding:10px 16px;"
        "margin:6px;cursor:pointer}#progress{position:sticky;top:0;background:#172b3d;"
        "color:white;padding:12px;z-index:1}.notice{border-left:5px solid #ab6427}"
        ".muted{color:#52677a}a{color:#075da8}</style></head><body>"
        "<h1>Your shorter recall review</h1>"
        '<p id="progress" role="status">0 of 42 assessments recorded</p>'
        '<section class="notice"><strong>Scope approved; your findings are still needed.</strong>'
        "<p>Review 37 cases, one cost summary and four history summaries. Choose an assessment "
        "for each. Add notes for concerns or unacceptable results, then give your overall "
        "decision and residual-risk finding. No answer or attestation is prefilled.</p>"
        "<p>Recall remains disabled. These requirements are prepared locally and still need "
        "their own release review and CI before activation.</p></section>"
        "<section><h2>What this review covers</h2>"
        "<p>All nine obsolete-answer cases, all 20 strict sparse-to-hybrid regressions, "
        "and eight category spot checks. The 270 omitted difficult cases remain individually "
        "unreviewed. All 400 scenarios and 1,600 observations remain unchanged.</p>"
        "<p>The original quality gates failed. This small purposive subset is not statistically "
        "representative, and omitted cases may contain unacceptable behavior. Exact-output "
        "failures receive no automatic semantic credit. The two unpublished development "
        "repairs have no new answer-quality evidence and receive no credit here.</p>"
        "<p>Access, erasure, lifecycle, runtime budgets, owner-local interactive use, "
        "seven-day maximum expiry, rollback and fresh production compatibility remain required."
        "</p></section>",
    ]
    for index, cid in enumerate(ordered, 1):
        group = cases[cid]
        case = group["case"]
        reason = (
            "Obsolete final answer"
            if cid in scope["actual_obsolete_answer_scenarios"]
            else "Strict sparse-to-hybrid regression"
            if cid in scope["original_strict_regression_scenarios"]
            else "Deterministic category spot check"
        )
        parts.append(
            f'<details id="{escape(cid)}"><summary>{index}/37 · {escape(cid)}'
            f" · {escape(reason)}</summary>"
            f"<p><strong>Question:</strong> {escape(case['task']['question'])}</p><ul>"
        )
        for event in case["history"]:
            parts.append(f"<li>{escape(event['at'])} — {escape(event['content'])}</li>")
        parts.append(
            "</ul><p><strong>Original expected action and exact values</strong></p><pre>"
            + escape(
                json.dumps(
                    {"action": case["expected_action"], "values": case["expected_values"]},
                    indent=2,
                    ensure_ascii=False,
                )
            )
            + "</pre>"
        )
        for row in group["rows"]:
            parts.append(
                "<section><h3>"
                + escape(row["path"] + " / " + row["channel"])
                + "</h3><pre>"
                + escape(row["answer"])
                + "</pre><p>"
                + "Exact value match: "
                + escape(row["answer_value_match"])
                + " · Obsolete final values: "
                + escape(row["obsolete_final_value_matches"] or "None")
                + " · Invalid-current flags: "
                + escape(row["invalid_current_flags"] or "None")
                + " · Attributed cost: $"
                + escape(row["cost_usd"])
                + "</p><details><summary>Original rendered memory and observation</summary><pre>"
                + escape(
                    json.dumps(
                        {
                            "rendered_memory": row["rendered_memory"],
                            "observation": row["observation"],
                            "raw_result_sha256": row["raw_result_sha256"],
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
                + "</pre></details></section>"
            )
        parts.append(
            '<p class="muted">Value match alone is not a strict pass: the original evidence, '
            "authority and output requirements still apply.</p>"
            + finding("case:" + cid)
            + "</details>"
        )
    parts.append(
        "<section><h2>Cost summary · all 40 over-ceiling rows</h2>"
        "<p>The original ceiling is $0.13937625 per attributed case. These 13 scenarios "
        "retain every attributed cost exception. Formation costs occur "
        "in multiple variants; do not sum them as additional provider charges. Total retained "
        "exposure remains $194.92785075, including $12.21728 uncertain. Indexing stays capped at "
        "$2/day, with a prepared $0.10 profile. No new paid work is authorized.</p>"
        "<table><tr><th>Scenario</th><th>Path / arm</th><th>Attributed cost</th></tr>"
    )
    for case_cost in scope["cost_summary"]:
        for row in case_cost["rows"]:
            parts.append(
                f"<tr><td>{escape(case_cost['scenario_id'])}</td>"
                f"<td>{escape(row['path'])} / {escape(row['channel'])}</td>"
                f"<td>${escape(row['cost_usd'])}</td></tr>"
            )
    parts.append("</table>" + finding("summary:cost") + "</section>")
    items = {item["original_item_sha256"]: item["item"] for item in data["review_items"]}
    history_notes = {
        "Recovery and deadline changes": (
            "The three original changes cover the formation deadline repair, bounded "
            "recovery and a preparation replacement. Their authorization and evidence links "
            "remain attached; no new retry or successful quality gate is inferred."
        ),
        "Interrupted and reconciled attempts": (
            "Three interrupted attempts and one reconciled collector segment remain visible. "
            "Reconciliation does not turn incomplete or failed outcomes into successes."
        ),
        "Uncertain reservations": (
            "Five uncertainty records remain counted. The retained $12.21728 uncertain "
            "amount is included in $194.92785075 total exposure; no reservation is forgiven."
        ),
        "Unobserved completed charges": (
            "Six records retain charges for completed calls without the expected observation. "
            "Those costs remain counted without adding successful quality observations."
        ),
    }
    for label, hashes in groups.items():
        parts.append(
            f"<section><h2>{escape(label)} · {len(hashes)} original items</h2>"
            "<p>"
            + escape(history_notes.get(label, "All linked original records remain retained."))
            + "</p><details><summary>Inspect every linked original record</summary><pre>"
            + escape(
                json.dumps(
                    [{"sha256": h, "item": items[h]} for h in hashes], indent=2, ensure_ascii=False
                )
            )
            + "</pre></details>"
            + finding("summary:" + label)
            + "</section>"
        )
    payload = {
        "scope_proposal_sha256": APPROVED_SCOPE_SHA256,
        "observations_sha256": data["observations_sha256"],
        "review_items_sha256": data["original_review_items_sha256"],
        "selected": selected,
        "omitted": scope["omitted_required_scenarios"],
        "groups": groups,
    }
    parts.append(
        "<section><h2>Coverage left unreviewed</h2>"
        "<p>These 270 cases receive no individual review attestation. A cost summary does "
        "not turn an omitted case into an individually reviewed case.</p>"
        "<details><summary>Show all omitted IDs</summary><pre>"
        + escape("\n".join(payload["omitted"]))
        + "</pre></details>"
        '<p><a href="scope-proposal.json">Immutable scope proposal</a> · '
        '<a href="../owner-review-20261019-v3/review.html">Full original packet</a></p></section>'
        "<section><h2>Your overall decision</h2>"
        '<label>Your name <input id="reviewer" autocomplete="name" required></label>'
        '<label>Decision <select id="decision" required><option value="">Choose a decision</option>'
        '<option value="accept_bounded_rollout">Accept the bounded rollout</option>'
        '<option value="changes_required">Changes required</option>'
        '<option value="reject">Reject</option></select></label>'
        '<label>Overall findings and required mitigations <textarea id="findings" rows="4" '
        'placeholder="At least 20 characters."></textarea></label>'
        '<label>Residual risk of the 270 omitted cases <textarea id="risk" rows="4" '
        'placeholder="State whether that remaining risk is acceptable, and why.">'
        "</textarea></label>"
        '<label><input type="checkbox" id="human"> I am the human reviewer and these are my '
        "own findings after reviewing the displayed evidence.</label>"
        '<label><input type="checkbox" id="independent"> My review is independent of the '
        "implementation.</label>"
        '<p id="storage" role="status"></p><p id="error" role="alert"></p>'
        '<button id="save" type="button">Download draft</button>'
        '<label>Restore your draft <input id="restore" type="file" accept=".json"></label>'
        '<button id="export" type="button">Export completed review</button>'
        "<p>Save the exported JSON and return it in this chat. A rejection or request for "
        "changes can be exported but cannot authorize admission. No export is uploaded "
        "automatically. Scope approval alone does not enable recall.</p></section>"
        '<script type="application/json" id="review-data">'
        + json.dumps(payload).replace("<", "\\u003c")
        + "</script><script>"
        + Path(__file__).with_name("recall_owner_review.js").read_text()
        + "</script></body></html>"
    )
    return "\n".join(parts)


def build(proposal_path: Path, data_path: Path, output: Path) -> None:
    raw_scope = proposal_path.read_bytes()
    if digest(raw_scope) != APPROVED_SCOPE_SHA256:
        raise ValueError("Scope differs from the owner-approved proposal")
    scope = json.loads(raw_scope)
    if digest(data_path.read_bytes()) not in scope["source_inputs"].values():
        raise ValueError("Review data differs from the approved input")
    data = json.loads(data_path.read_bytes())
    if (
        data["observations_sha256"] != APPROVED_OBSERVATIONS_SHA256
        or data["original_review_items_sha256"] != APPROVED_ITEMS_SHA256
    ):
        raise ValueError("Original observation or history identity differs")
    page = render(data, scope)
    output.mkdir()  # Never overwrite an existing packet or the owner's draft.
    (output / "review.html").write_text(page)
    (output / "scope-proposal.json").write_bytes(raw_scope)
    (output / "packet-receipt.json").write_text(
        json.dumps(
            {
                "version": "recall-scoped-review-packet@1",
                "scope_proposal_sha256": digest(raw_scope),
                "review_data_sha256": digest(data_path.read_bytes()),
                "review_html_sha256": digest(page.encode()),
                "individual_cases": len(scope["proposed_individual_scenarios"]),
                "omitted_cases": len(scope["omitted_required_scenarios"]),
                "history_groups": len(scope["interruption_summary_groups"]),
                "human_findings_supplied": False,
                "production_changed": False,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal", required=True, type=Path)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    build(args.proposal, args.data, args.output)
