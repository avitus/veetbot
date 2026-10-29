# ADR-0267: The deferred tool index holds fifty entries and 3,000 tokens

- Status: Accepted (the owner asked for it on 2026-10-29)
- Date: 2026-10-29
- Related: ADR-0079, ADR-0105, ADR-0123, ADR-0253, ADR-0255, ADR-0262
- Amends: ADR-0123 decision 1 (the forty-entry cap) and ADR-0262 (the
  2,500-token cap)
- Detailed design: `docs/plan/context-engine.md`

## Context

ADR-0262 raised the deferred tool index to 2,500 tokens and kept forty
entries, because nothing exceeded the item cap then. It warned that the item
cap was the next limit. On `main` on 2026-10-29, the production-shaped roster
gate's fullest chat, bound to a website profile with every production flag
on, lists forty entries at 2,088 estimated tokens. That is the whole item cap,
so the next tool that defers would be skipped in that chat, silently for the
owner. The same roster without a website profile lists thirty-six entries at
1,909 tokens.

Each limit has bound in turn, each found after a chat lost a capability: the
token cap of definitions (ADR-0105), the item cap of definitions (ADR-0124),
the configured definitions (ADR-0253), and the index's tokens (ADR-0262).

## Decision

- The index's item cap rises from forty to fifty entries, and its token cap
  from 2,500 to 3,000. An entry costs about fifty-five tokens, so the two caps
  fill at about the same time.
- The prefix ceiling rises by exactly 500, to 25,000, and remains the sum of
  the Region A class caps (ADR-0079). The owner tool index (ADR-0255) is
  unchanged.
- The production-shaped roster gate asserts that its fullest chat leaves at
  least ten entries and 500 tokens of the index free. A change that eats into
  that margin fails the gate, and raising the caps again or trimming entries
  is the owner's call.
- Only `context/plan.yaml` changes. The planner, the index rendering and the
  rank order are unchanged.

## Consequences

- Only a plan with more than forty index candidates, or more than 2,500
  tokens of them, is selected differently. Every other plan is selected and
  rendered exactly as before, and recorded plans are unchanged.
- The fullest chat keeps ten entries and 912 tokens free, about ten more
  deferred tools.
- The cap is a ceiling, not a charge. No chat's prefix grows today, and a
  chat that later fills the new room pays up to 500 more Region A tokens per
  request, the cached part of the prompt.
- Selection accuracy degrades with the number of candidates
  (`context-engine.md`). An index entry is one line, not a definition, and the
  thirty-definition cap does not change.

## Alternatives considered

- **Keep forty entries.** The next deferred tool in the fullest chat is
  skipped, as the Gmail writes were before ADR-0262.
- **Raise only the item cap.** The 2,500-token cap would bind after about
  seven more entries.
- **Shorten the entries.** It buys tens of tokens once, and the next feature
  takes them back.
- **No headroom gate.** Every earlier limit was found in production, after a
  chat had already lost a tool.

## Verification

- `tests/gates/test_deferred_tools_adr0123.py` plans the website-bound chat
  with every production flag on and asserts ten free entries and 500 free
  tokens. It failed against the forty-entry cap, with no entry free.
- `tests/gates/test_persona_m22.py` asserts the 25,000 ceiling and that it
  equals the sum of the Region A class caps.
