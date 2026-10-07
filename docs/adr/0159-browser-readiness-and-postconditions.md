# ADR-0159: Bounded browser readiness and visible postconditions

- Status: Accepted under the owner's ADR-0156 implementation instruction
- Date: 2026-10-01
- Related: ADR-0058, ADR-0130, ADR-0156, ADR-0157, ADR-0158
- Amends: ADR-0130 network-idle-first settling mechanism
- Detailed design: `docs/plan/browser-automation.md`

## Decision

Retain Playwright actionability and all live authorization guards. After a
navigation or dispatch, wait for document readiness, the finite application
requests already in flight at settling entry, and 300 ms of DOM quiet, within
the existing two-second budget. Snapshot at most 256 pending document, script,
stylesheet, fetch and XHR requests; later polling does not extend that snapshot.
Completed or failed requests leave it, and a successful event stream leaves at
its headers without reading its body. Overflow or deadline expiry reports
`bound_expired`; a new document clears the old request set. Background polling
need not stop. Report
`dom_quiet` or `bound_expired` on the returned observation and in phase metadata.
Neither means that a business task completed or that the website is trustworthy.
Ordinary explicit observation does not claim it waited for stability.

Version the three browser tools to 1.2.0 for this additive result contract.
`browser.observe` additionally accepts `wait_for`, mutually exclusive with
continuation. `browser.act` optionally accepts `postcondition`. Both use the
same declarative predicate: an exact, bounded nonempty control role and name,
optional disabled/checked state, and a 0–5,000 ms wait budget (default 2,000).
There is no selector, regular expression, script, origin or profile parameter.

The trusted tool checks only the current bounded observation window. Report
`satisfied` for exactly one matching visible control with the requested state,
`ambiguous` for multiple same-role/name controls, otherwise `not_observed`.
Results disclose this window scope, the number of observations and elapsed time.
A match is evidence of a visible predicate, not proof of a causal write effect,
account identity, global uniqueness, or completion outside the observed window.
Existing model projection must retain the condition result or explicitly omit
the whole observation; it must never manufacture completion by clipping it.

After the initial observation, poll reads at most every 250 ms, at most twenty
times, within the predicate deadline and the existing tool/run deadline.
Expansion is never implicit. No action is retried, re-targeted, re-approved or
charged a second grant use. If a capture times out mid-flight, cancel and join it
and return no earlier references: capture failure may invalidate them. A read
then fails `page_changed`; loss of usable evidence after an action yields the
existing `outcome_unknown`. The outer tool deadline also settles a browser
action with a committed effect watermark as uncertain and non-retryable,
including a deadline that expires during its postcondition reads. Successful reads with an unmet predicate return an
ordinary observation with `not_observed`, not a claim that the action was unsent.

The predicate stays in the approved tool arguments. Only the original single
action goes to the provider, preserving old provider protocol compatibility;
the trusted tool evaluates the predicate through ordinary read operations.
Read-only recovery remains a fresh observation and decision. This does not add
workflow replay, a second effect ledger, or automatic recovery from writes.

## Verification

Use delayed SPA and continuous polling fixtures; verify truthful bound expiry,
uniqueness and state checks, finite read counts/time, cancellation invalidation,
no duplicate server effects, unchanged approval classification, and preserved
condition evidence in small model budgets. Run existing hostile-page, grant,
lease and real Chromium suites. W4 workflow-specific recovery remains separate.
