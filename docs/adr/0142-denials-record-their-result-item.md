# ADR-0142: Denials record their result item, and old denials project from what they recorded

- Status: Accepted by owner request on 2026-09-29 (runtime repair)
- Date: 2026-09-29
- Related: ADR-0003 (amended: payload versions and upcasters), Milestone 4
  approvals
- Detailed design: `docs/plan/event-log-and-persistence.md`

## Evidence

On 2026-09-29, on `main` at 344103c8, a chat broke after the owner denied an
approval: the next message posted to that session returned HTTP 500
`internal_error`. Since Milestone 4 (dc95722c) the approval and policy denial
path appended `tool.call.denied` with the call's name, id and reason code but
no `result_item`. Session history requires a result item for every tool result
event, because the provider must see a result for every call it replays. The
projection therefore raised on every later submission in that session,
including messages from paired surfaces. Trajectory export, skill review and
the in-memory history read the same projection function and failed the same
way. Refusals, which also append `tool.call.denied`, always carried the item.
Production sessions already hold the itemless events.

## Decision

1. `tool.call.denied` payload version 2 requires `result_item`: the item the
   denial gave the model, which the invocation row already stores. The tool
   pipeline's one event writer stamps version 2 on every denial and refuses
   to append a denial without its item.
2. A pure upcaster takes version 1 to version 2 by making a missing
   `result_item` an explicit `None`. A refusal's recorded item passes through
   unchanged. No stored payload is rewritten.
3. Session history renders an explicit-`None` denial from the recorded facts
   alone: an error result for the recorded call id whose single text part is
   `{"status":"denied","action":<name>,"reason_code":<reason code>}`. The
   narration and the tool's output trust were never recorded, so the item has
   no message and is labelled `external_untrusted`, the level the platform
   gives content whose provenance it did not record. A denial whose item is
   absent rather than `None`, or whose name, call id or reason code is
   missing, still fails the projection, as does any other tool result event
   without an item.

## Alternatives rejected

- **Rewriting the stored events.** Stored payloads are immutable; schema
  evolution is expressed only as upcasters.
- **Upcasting to the full original item.** It needs the tool's output trust
  and the narration catalog as it stood when the event was written. An
  upcaster may neither look them up nor invent them.
- **Projecting the invocation row's stored item.** It is exact, but it makes a
  projection read another table and repairs only the PostgreSQL session
  history, while trajectory export and skill review decode the same events
  without that table.
- **Dropping the denied call from history.** The call would be orphaned, and
  context assembly rejects an orphaned call.

## Consequences

- Affected sessions accept messages again once the release is deployed,
  without a migration or a projection rebuild. The failed projection never
  advanced its watermark past the denial, so catch-up resumes there, and no
  stored history row holds an itemless denial, so the builder version is
  unchanged.
- Replayed old turns show the model a terser denial marked untrusted. The
  label only lowers trust, and the write-taint rule reads the active turn
  alone, so an old denial in an earlier turn does not taint a new request.
- Code older than this release fails loudly on a version 2 denial, as the rule
  against partial decoding of newer payloads requires. The API and the workers
  run the same release.

## Verification

- `tests/integration/test_approval_denial_history_postgres.py`: the HTTP
  reproduction and a stored version 1 denial. On `main` 344103c8 both failed
  with the 500 (`ValueError: tool.call.denied has no result item`); both pass.
- `tests/unit/test_denied_tool_history.py`: the in-memory journey, which failed
  the same way on the unrepaired executor, and the projection's accepted and
  rejected shapes.
- `tests/contract/test_upcaster_contract.py`, the check for
  `gate.event.upcaster_totality`: recorded version 1 approval denials and
  refusals, version 2 pass-through, and rejection of version 3.
