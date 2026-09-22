# ADR-0110: A typed-judgment port, with TypeSafe Jev as its first provider

- Status: Accepted — owner authorized the port, judgment folder matching, and the offline email evaluation on 2026-09-19
- Date: 2026-09-19
- Related: ADR-0002, ADR-0045, ADR-0054, ADR-0057, ADR-0076, ADR-0092, ADR-0102
- Design: [Typed judgment](../plan/typed-judgment.md), [Chat thread folders](../plan/thread-folders.md)
- Amends: ADR-0102

## Context

The platform makes narrow judgments in many places: which existing folder a
conversation belongs in, whether two memories state one claim, whether a
message matters. Each is decided today either by string arithmetic — token
overlap, a hand-kept synonym table, a regular expression — or by a generative
model call whose prose must then be parsed, grounded, and replaced by a
fallback when it is malformed. The generative path is slow, costs orders of
magnitude more than the decision is worth, and has failed in production on
exactly that parsing step.

TypeSafe's Jev is a different kind of model. A caller sends structured state
and a closed set of typed questions and receives a probability that a
statement holds, a choice among offered options with its distribution, or a
position on an ordered scale. It generates no text, answers in roughly a tenth
of a second, and is priced at USD 0.042 per million input tokens. Its
documented weaknesses are literal reading of negation and scope, counting,
ordering dates, multi-step indirection, distraction by irrelevant state, and
steering by instructions planted in the state it judges.

The owner obtained an API key and asked where the platform could use it. An
assessment of every model call and every heuristic judgment in the code found
thread-to-folder matching the best first use: it is already in Milestone 29's
scope, its inputs are short, it runs in the background, and its output is
only ever a proposal the owner accepts or declines.

ADR-0102 admitted Milestone 29 with the sentence "It admits no new
dependency" and bound model-assisted grouping to "the existing provider
egress policy". A new vendor behind a new port changes both, so it is
recorded here rather than absorbed silently.

## Decisions

1. **Add a provider-neutral typed-judgment port.** `JudgmentProvider` exposes
   one `judge` call over frozen domain types for the three question kinds —
   Noul, Choice, and Score — and returns typed answers with usage. It has one
   contract suite, run over every shipped provider and the fake.
2. **It is not the model gateway.** The gateway's provider protocol streams
   generative events under a routed model policy and its wire shapes are a
   closed set. No model profile is registered, no model policy resolves to
   the port, and roadmap item B2 — dynamic model routing and a second provider
   adapter — stays unauthorized and unamended.
3. **TypeSafe Jev is the first provider, over raw HTTP.** The adapter fixes
   egress to the vendor's one documented HTTPS endpoint, follows no redirect,
   resolves the `typesafe` credential at call time, and sends it only as that
   request's authorization header, the arrangement ADR-0076 set for Keenable.
   No vendor SDK is admitted. The egress policy gains no entry, because it
   governs sandbox traffic and tenant-supplied URLs, not an endpoint the
   composition root selects and an adapter hard-codes.
4. **Default off, twice.** `JUDGMENT_PROVIDER` defaults to `disabled`, and a
   credential alone enables nothing, ADR-0076's rule. Each consumer adds its
   own default-off switch in its own checked-in profile. A selector with no
   credential degrades — one warning naming variables only, and every call
   failing without dialing — and never refuses startup.
5. **Failures are typed and every consumer names a deterministic fallback.**
   One error type with six reason codes leaves the port; its message is the
   code alone, response bodies are never read on failure, and exception chains
   are suppressed because validation errors quote their input. No run, pass,
   or startup may fail because the port failed.
6. **Cost is computed locally at a pinned, dated price.** The vendor returns
   token counts and no cost. Judgment calls write no model-ledger row and draw
   on no run budget; each consumer owns a ceiling and a content-free audit.
7. **The first consumer is add-to-folder matching under Milestone 29.** For
   each unfiled chat conversation, one Choice over the existing folders plus
   an explicit no-match option; a match is accepted only above a configured
   probability threshold; the output is only ever an add-to-folder candidate
   that the maintenance pass re-checks against every eligibility rule. New
   folders are still discovered and named by the lexical and generative
   groupers, because a judgment provider names nothing. Any provider error,
   deadline, or budget breach discards every judgment and leaves the existing
   grouping unchanged. ADR-0102's decisions 2, 3, and 4 otherwise stand:
   nothing files a conversation without the owner's acceptance, no embedding
   is admitted, and session metadata never enters a grouping input.
8. **An offline email-importance evaluation is admitted as an evaluation-only
   consumer.** It builds the replay runner Milestone 26's open evaluation item
   still lacks, with the production assessor as one arm and the judgment port
   as the other, scored by the unchanged label-only quality scorer. It is
   non-activating: no production module imports it, it cannot satisfy or waive
   a Milestone 26 gate, and Milestone 26's deferral of model routing changes
   stands. Any production use of the port for email needs a later ADR amending
   ADR-0092.
9. **Any further consumer needs its own authorization.** The policy advisory
   layer in particular is roadmap item B8 and enters only through its own
   policy ADR.

### Amendment, 2026-09-21: a seventh reason code

Decision 5 counted six reason codes. The first production day added
`judgment.payment_required` for status 402, because an account without credits
read as a malformed request and its remedy is the owner's billing page. The
rule is unchanged: status alone classifies a failure and no body is read.

## Data the vendor may receive

Admitted:

- For folder matching: chat conversation titles, first-message snippets of at
  most 400 characters, folder names, and up to five member titles per folder —
  the inputs ADR-0102 decision 4 already fixed for the generative grouper.
- For the offline evaluation only: the owner's frozen evaluation corpus, with
  the evidence the production assessor sees — subjects, bounded passages,
  sender addresses, the owner's feedback, decayed reply counts, and the shared
  memory statements that path already caps at `INTERNAL`.

Never sent, by any consumer: credentials and credential-shaped values; memory
statements above `INTERNAL` sensitivity; hardline rules and policy profile
contents; drafts and style examples; session metadata; attachments; and
principal or tenant identifiers. Hazard scans run before egress.

## Risk acceptance

The vendor's published terms, read on 2026-09-19, commit that it does not
train or fine-tune on customer inputs and that it notifies a breach within 72
hours. They state no deletion period — data is kept as long as reasonably
necessary — and zero data retention is offered to enterprise customers only,
on request.

The owner accepted those terms on 2026-09-19 for every data class admitted
above, including the mail in the frozen evaluation corpus, which carries third
parties' words. This is an explicit risk acceptance, not an assumption of zero
retention. ADR-0057's rule still holds in the other direction: evaluation
evidence, not this acceptance, decides whether any consumer is worth
activating.

## Scope admission and consequences

Milestone 29 gains a `gate.judgment.*` area of four gates and one further
`gate.folder.*` gate, all registered pending; registration is not passing
evidence. The design admits one port with its contract suite, one adapter, one
fake, one environment selector, one credential reference, and two tuning knobs
in the folder profile. It admits no new Python dependency and no migration.

The platform gains a second kind of external model dependency, with its own
availability and drift. The mitigations are structural: every consumer works
without it, no consumer acts on an answer without an owner's decision or a
more restrictive outcome, and a steered answer costs a bad proposal at worst.

Production activation is the owner's act: the key, the selector, and the
consumer switch are added to the root-owned environment file, and no secret
value appears in a template, a commit, or a pull request.

## Alternatives considered

- **Register Jev as a model-gateway provider profile.** The profile's wire
  shape is a closed set of three generative protocols, and doing so would make
  unauthorized model routing the mechanism.
- **Use the vendor's Python SDK.** One POST does not justify a third vendor
  SDK against the house rule, and owning the request keeps the bounds, the
  retry budget, and the error sanitization in code the contract suite sees.
- **Replace the generative grouper outright.** A judgment provider cannot name
  a new folder, and the lexical floor is what the spec requires the pass to
  fall back to.
- **Start with email importance.** The shape fits, but mail is the most
  adversarial and most private input, importance leans on date reasoning the
  vendor is weak at, and Milestone 26 defers model routing. An offline
  evaluation answers the question first.
- **Wait for zero data retention.** It is enterprise-only and may never be
  offered on this account; the owner chose to accept the published terms
  instead.

## Acceptance status

The owner approved the implementation plan in chat on 2026-09-19. Implementation
may proceed through the designs' build sequences under the repository's
red-green rule. Pull request creation, merge, and production activation retain
their explicit boundaries.
