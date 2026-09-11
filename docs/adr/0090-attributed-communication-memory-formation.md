# ADR-0090: Attributed communication may form memory without becoming owner speech

- Status: Proposed
- Date: 2026-09-10
- Related: Milestones 10, 18, 21, 24, and 25; ADR-0018, ADR-0051,
  ADR-0064, ADR-0071, ADR-0077, ADR-0081, ADR-0082, ADR-0085
- Detailed design: `docs/plan/memory-formation-and-consolidation.md`,
  `docs/plan/adaptive-memory-distillation.md`
- User authorization: all communication channels are viable memory-formation
  sources, with dramatically higher memory volume (2026-09-10)

## Context

A production run asked for the five most important recent work emails, read the
mail successfully, completed formation, and formed no belief. The formation
watermark advanced and the provider extraction completed with zero grounded
candidates: the service admitted only `user.message.created` events authored by
the principal, while every Gmail result was excluded solely because it was a
tool event. Replaying the session under the same policy would therefore repeat
the loss.

The exclusion was an intentionally conservative injection defense, but it
collapsed two different questions. Communication content must remain
`EXTERNAL_UNTRUSTED` for policy, credentials, instructions, and consequential
actions; that does not require it to be invisible to a personal memory system.
SMS already promises that worthwhile inbound content is remembered, and paired
surfaces authenticate the owner while preserving channel attribution. The owner
explicitly chose recall here: every authenticated communication channel should
be eligible to contribute memories, with attribution and uncertainty carrying
the safety burden.

## Decision

1. **Formation source admission is typed.** An owner assertion is either a
   principal-authored user event or an authenticated paired-surface user event
   bound to that principal. An attributed communication is a recognized,
   principal-scoped communication event: initially first-party Gmail read
   results and device-ingested SMS. Arbitrary MCP, web, browser, file, assistant,
   model, foreign-principal, and foreign-tenant content remains ineligible.
2. **Trust is not upgraded.** Gmail and SMS remain `EXTERNAL_UNTRUSTED` for
   context and policy. Their automatic memories use inferred authority,
   hypothesis derivation, tentative longevity, local portability, at least
   sensitive classification, and explicit channel attribution in the statement.
   They cannot retract, supersede, or promote an owner-authority belief.
3. **The adapter is deterministic and separately versioned.**
   `communication-attribution-v1` wraps whichever evaluated semantic extractor
   composition selected. It does not change `formation@8`, `formation@9`, or
   `formation@10` provider prompts, schemas, calls, evidence, or precedence. It
   parses only the repository-owned Gmail and device event contracts and emits
   bounded candidates through the ordinary provenance, hazard, conflict,
   lifecycle, and audit gates.
4. **Store useful summaries, not raw payloads.** One bounded memory is proposed
   per distinct Gmail thread or SMS receipt, preferring a read thread over its
   earlier search preview. Gmail statements identify the channel,
   correspondent, subject where present, and a bounded excerpt. SMS version 1
   records the channel and correspondent but not the body, preserving Milestone
   24's one-content-event rule. The source event remains the complete auditable
   evidence; raw tool JSON and device framing are not copied wholesale into the
   belief.
5. **Communication volume has an explicit ceiling.** The existing semantic
   candidate ceiling is preserved. The adapter may additionally propose at most
   twenty attributed communication candidates, and the formation@9 global
   ceiling remains thirty-two. Duplicated Gmail search/read evidence is folded
   by thread before proposals are ranked.
6. **The existing source-grounding gate widens, not the gate census.** It now
   proves both sides of the boundary: recognized communications form only under
   the restrictions above, while lookalike arbitrary tools, unpaired surfaces,
   malformed payloads, injection-shaped text, credentials, and foreign content
   still form nothing.

## Consequences

- The motivating work-email run can form several provisional, inspectable
  memories without treating a correspondent as the owner or waiting for a new
  provider evaluation artifact.
- Gmail search and thread reads, incoming SMS, Telegram, and WhatsApp all have a
  defined path: Gmail and SMS are attributed correspondence; paired Telegram and
  WhatsApp messages are authenticated owner assertions.
- Communication memories expire after thirty days without later evidence and
  are flagged for review because they are sensitive. The user can inspect,
  correct, promote through a later owner statement, or delete them through the
  existing surfaces.
- Semantic distillation over correspondent prose remains a later evaluated
  policy. Version 1 intentionally records a bounded communication summary rather
  than asking the existing provider extractor to reinterpret third-party text.

## Alternatives considered

- **Globally trust tool output:** rejected; it would let arbitrary web and MCP
  content write memory and erase the injection boundary.
- **Require the user to restate every email or text:** rejected; that is the
  production failure this change corrects and cannot produce dramatically more
  memory.
- **Change `formation@10` in place:** rejected; its extractor and release
  evidence are frozen together. The deterministic adapter composes outside it.
- **Introduce a new provider policy immediately:** deferred. Correspondent-aware
  semantic extraction needs its own corpus and activation evidence; it is not
  necessary to stop dropping every communication today.
