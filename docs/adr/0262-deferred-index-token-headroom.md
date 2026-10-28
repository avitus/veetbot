# ADR-0262: The deferred tool index holds 2,500 tokens

- Status: Accepted (the owner asked for it on 2026-10-27)
- Date: 2026-10-27
- Related: ADR-0079, ADR-0105, ADR-0253, ADR-0255, ADR-0258
- Amends: ADR-0123 decision 1, which capped the deferred tool index at 2,000
  tokens
- Detailed design: `docs/plan/context-engine.md`

## Context

With every production flag on and a website profile bound, a chat's deferred
tool index reached its 2,000-token cap. ADR-0253 recorded the personal Gmail
account's `trash_thread` and `untrash_thread` in `skipped_tool_names`. On
`main`, `web.fetch_json`, which pull request 204 added, moved that account's
`modify_labels` out beside them. Neither change raised the cap; both left it
to the owner. Skipped
tools are neither defined nor listed, so in such a chat the model could not
label, trash or restore a personal-account thread. A chat without a website
profile skipped nothing.

Measured on the production-shaped roster gate, the complete index for that
chat is 39 entries and 2,088 estimated tokens on `main`, and 39 entries and
2,059 on `dev`. The two lists differ by one entry each: `main` has
`web.fetch_json`, and `dev` has `artifact.read_capture` (ADR-0261). The three
Gmail writes add 45, 29 and 31 tokens.

## Decision

The deferred tool index's token cap rises from 2,000 to 2,500. Its item cap
stays at forty entries, and the owner tool index (ADR-0255) is unchanged. The
prefix ceiling rises by exactly 500, to 24,500, so it remains the sum of the
Region A class caps (ADR-0079), and the persona gate now checks that sum
rather than only the number. Only `context/plan.yaml` changes. The planner,
the index rendering and the rank order are unchanged.

## Consequences

- A website-bound chat with every production flag on now lists every entry,
  and its `skipped_tool_names` is empty.
- Only a plan whose index would pass 2,000 tokens is selected differently.
  Every other plan is selected and rendered exactly as before, so no other
  chat's prefix changes. The cap applies when a chat is planned; plans
  already recorded are unchanged.
- The larger index costs up to 500 more Region A tokens per request in the
  chats that use it. Region A is the cached prefix.
- That chat leaves about 400 tokens of headroom. Once `main` and `dev` are
  merged it holds forty entries, exactly the forty-item cap, so the item cap
  is the next limit: any new deferred entry there skips the lowest-ranked one
  again.
- The planner multiplies the estimate, three UTF-8 bytes per token, by the
  model's learned correction factor, which never falls below one
  (`context-engine.md`). The headroom absorbs a factor of up to about 1.19 for
  this roster. Three bytes per token over-counts English and JSON for current
  tokenizers, so the factor is expected to stay at one.

## Alternatives considered

- **Leave the cap at 2,000.** The personal account's label, trash and
  restore tools stay unreachable in every website-bound chat.
- **Raise it only to what fits today, about 2,100.** The next index entry in
  that chat would push a Gmail write out again.
- **Shorten the entries.** ADR-0258 already kept `web.fetch_json`'s first
  sentence short. The Gmail entries render text the server supplies, and
  shortening them buys tens of tokens, not a lasting margin.
- **Raise the item cap as well.** Nothing exceeds it today, and the owner
  asked for the token cap.

## Verification

- `tests/gates/test_deferred_tools_adr0123.py` plans the website-bound chat
  with every production flag on. It now asserts that nothing is skipped and
  that the three personal-account Gmail writes are deferred. It failed
  against the 2,000-token cap.
- `tests/gates/test_persona_m22.py` asserts the 24,500 ceiling and that it
  equals the sum of the Region A class caps.
