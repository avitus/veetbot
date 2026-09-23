# ADR-0116: Bulk mail never forms communication memory

- Status: Accepted (authorized by the repository owner, 2026-09-22)
- Date: 2026-09-22
- Related: ADR-0090, ADR-0096, ADR-0101, ADR-0112, ADR-0117; Sections 6 and 7
  of `docs/plan/email-experience.md`; Section 7 of
  `docs/plan/people-and-relationships.md`
- Detailed design: `docs/plan/email-experience.md`,
  `docs/plan/people-and-relationships.md`

## Context

On 2026-09-22 the automatic email refresh formed two People facts about a
founder named in a newsletter from The Information, and created a provisional
person from that passing editorial mention. The thread had already been
assessed as low priority, its stored reason called it a newsletter, and its
sender was already recorded in the unsubscribe census as an active one-click
subscription. None of those signals reached formation. The email-semantic
policies admit any grounded fact from any received message inside the
ninety-day window, and the model's `bulk` verdict only scales the priority
score. Of the newest two hundred memories in production, thirteen of the
forty-four email-derived memories came from senders in the census.

ADR-0101 made `email-semantic@2` available without its precision evaluation,
so no measurement stood between bulk mail and memory. Every email-derived
memory is also committed at sensitive sensitivity and therefore flagged for
review, which is why the review queue filled with newsletters.

## Decisions

1. **Bulk mail is not a memory source.** Two deterministic signals decide it.
   Before any provider work, a thread indexed by the unsubscribe census in any
   state is bulk. After the assessment, a `bulk` verdict is bulk. A bulk
   message registers no semantic source, forms no semantic or People memory,
   projects no correspondence, and creates no provisional person.
2. **The rule lives in source validation, not in one caller.** The formation
   service's source validation refuses a census-indexed thread and a thread
   whose persisted assessment carries a `bulk` verdict, so the refresh, the
   historical import, and any replay meet the same rule. The refresh
   additionally skips the formation step as soon as the fresh verdict is known
   and records a content-free `email.semantic.skipped` event naming the reason,
   so the memory diagnose command can explain why nothing formed.
3. **The assessment prompt is unchanged.** The prompt revision is part of the
   assessment identity; changing it would re-assess every retained thread at
   provider cost. The People design also wants attributed third-party reports
   from genuine correspondence, so no correspondent-only restriction is added.
4. **Existing bulk-derived memories leave through source exclusion.** The
   census names the affected threads; the existing exclusion path erases
   derived influence and blocks re-formation. The operator command
   `agent email exclude-bulk` applies it in one pass.
5. **No new registered hard gate.** As with ADR-0113, the evidence is unit and
   contract tests; the milestone gate counts do not change.

## Consequences

- A personal sender whose mail carries list headers is treated as bulk only
  when the census admits and indexes the thread: the census already leaves out
  owner-sent, Spam, and Trash mail and protects senders the owner marked
  Important or wrote to. The owner's manual paths remain: excluding a thread
  from learning, forgetting a person, and the memory review actions of
  ADR-0117.
- A subscription the owner chose to keep still counts as bulk for memory.
- A historical import skips a census-indexed retained source and counts it as
  excluded rather than failing the slice.
- The census exists only when `AGENT_EMAIL_UNSUBSCRIBE_ENABLED` is set; without
  it, only the `bulk` verdict gates formation.
- Importance ranking is untouched: bulk threads are still assessed so the
  attention list and the census keep working.

## Alternatives considered

- **Tighten the People clause of the assessment prompt:** rejected because the
  prompt is versioned into the assessment identity and a revision re-spends on
  every retained thread, and because the design admits attributed third-party
  reports on purpose.
- **Gate on the priority score:** rejected because priority is a profile-bound
  ranking signal; a low score on genuine correspondence is not evidence that
  its facts are worthless.
- **Gate only in the refresh runner:** rejected because the historical import
  forms from retained sources without passing through the runner.
