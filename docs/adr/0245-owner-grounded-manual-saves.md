# ADR-0245: Owner-grounded manual saves after external reads

- Status: Implementation decision under the owner's memory retrieval repair plan (2026-10-24)
- Date: 2026-10-25
- Related: ADR-0002, ADR-0018, ADR-0129, ADR-0213, ADR-0233; Milestones 9 and 21
- User authorization: the approved retrieval repair plan, revision 3, "ADR-C +
  PR-4", owner-save half (2026-10-24)

## Problem

In the 2026-10-25 garden-calendar chat the owner asked, after a web search,
to remember that pruning schedules for four plants belong on the calendar.
`memory.remember` refused the save (`tool.memory.manual_trust_rejected`,
run 724c26cf): once a turn has read external content, `_turn_origin_trust`
taints the turn, and `_argument_trust` lifted a statement to `USER` only when
it was a sixteen-character verbatim span of the owner's message. The model's
paraphrase was not such a span. An identical save committed in run 7e373f3d
only because the model proposed it in the same step as the web search, before
the result existed, so the outcome depended on step order. The model also
proposed a 1,500-character calendar it had derived from the web (correctly
refused) and then told the owner "the separate long-term memory save failed"
although nothing had been asked.

## Decision

1. A `memory.remember` statement is raised to `USER` trust when it is
   owner-grounded: within the active turn (the newest owner-authored message
   and everything after it), the statement's content words are the owner's
   own, none came only from an external or knowledge tool result of that
   turn, at least sixty percent of them (and at least two) appear in the
   owner's message, at most three are the model's own phrasing, and its
   negations and modals, counted, are exactly those of the owner sentences
   it restates: none added, none dropped, none carried over from another
   sentence (request framing such as "could you" or "don't forget" is set
   aside only where it opens an owner sentence). These marker
   words are judged apart from recall's function-word list, which drops the
   modals, so ranking changes cannot relax provenance. The
   sixteen-character verbatim rule remains as it was. A message the owner did
   not author grounds nothing. The rule only raises and the turn's origin
   taint stands; the governed service's trust check is unchanged and refuses
   the write a second time if a label were wrong.
2. The outcome is the same whether the save is proposed in the step that
   read the web or a later one.
3. After a tainted turn, an ungrounded statement is refused with the closed
   reason `tool.memory.statement_not_owner_grounded` and guidance to save
   only what the owner stated and to keep derived plans in the reply or an
   artifact. `tool.memory.manual_trust_rejected` stays for the service-level
   refusal.
4. The default agent instructions say that `memory.remember` holds only what
   the owner stated, in their words, when they asked to remember it; that a
   plan, schedule or calendar derived from web or document content belongs in
   the reply or an artifact; and that a save, or a failed save, the owner did
   not ask for is never mentioned. The deployed agent row carries the new
   text with the release.

## Consequences

The untrusted-content write ban of ADR-0018 is unchanged: web, document and
knowledge content still cannot write persistent memory, directly or through a
statement that quotes it, and only the test for "the owner said this" moves
from verbatim bytes to grounding in the owner's words. The remaining exposure
is the one the verbatim rule already carried: a statement assembled only from
the owner's own words. The grounding thresholds are code constants in the
executor; moving them is a security change that needs this ADR revisited.

Red-first coverage: the owner recall probe contract (verbatim, paraphrase,
same-step paraphrase commit; a web-derived statement is refused with the
closed reason) failed on the previous code with the paraphrase refused as
`manual_trust_rejected`; unit tests pin grounding, taint, step independence,
non-owner messages, phrasing limits, other tools' unchanged rule, a
property test over web words in any position, the guidance text and the
instruction text.
