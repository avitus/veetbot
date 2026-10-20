# ADR-0216: Persona voice, lean-towards traits, entry switches, sample reply and boundaries

- Status: Accepted (owner, 2026-10-20: "implement all 4")
- Date: 2026-10-19
- Related: ADR-0079, ADR-0117, ADR-0129, ADR-0150, ADR-0213; Sections 10
  and 22 of the engineering plan
- Detailed design: `docs/plan/persona-surface.md`, `docs/plan/context-engine.md`

## Context

The A2 Persona design has five parts: a **Voice** field ("Describe how you
want Veetbot to talk to you. It reads this before every reply."); **Lean
towards** chips Brief, Warm, Direct, Formal and Playful; a **How that sounds**
sample reply; **What Veetbot assumes**, where each statement has an on/off
switch ("Turn one off to stop it being used without deleting it"); and
**Boundaries**, a list of fixed rules the persona cannot change.

The persona is "an ordered list of persona entries, versioned as a whole"
(persona-surface.md:45-47). `PersonaEntry` in
`src/agent_core/domain/persona.py` holds `text` (at most 500 characters),
`source`, `source_belief_id` and `sensitivity`, and forbids extra fields; a
document holds at most thirty. `PUT /v1/persona` replaces the list under
`expected_version`, and `render_persona` emits every entry under the
sensitivity ceiling, one per line, in the frozen-prefix row after the agent
instructions at `TRUSTED_CONFIGURATION`, capped at 2,000 tokens
(persona-surface.md:116-126). Per-surface variants are out of scope
(persona-surface.md:69-71). The routes sit behind `persona_api_enabled`
(default off) under `persona.read` and `persona.write`. Voice, traits, entry
switches, a sample reply and a boundary list appear nowhere in the design.

Two existing facts bear on this:

- **Spec drift.** `GET /v1/persona` is specified to return entries "with
  provenance and staleness flags" (persona-surface.md:251-253), but
  `PersonaEntryView` in `src/agent_core/domain/views.py` has no staleness
  flag. This ADR does not fix that; it is recorded so the implementer adds it
  with part B, since both change the entry view.
- **Hardline policy is not about tone.** `src/agent_core/policy/hardline.yaml`
  is frozen tool policy, not readable over HTTP. Policy is enforced by the
  engine, never by prompt text, so nothing the persona says can loosen it.

## Decision

Five parts; the owner can accept each separately.

### A. Voice and traits are document fields

`PersonaDocument` gains `voice: str | None` (at most 1,000 characters) and
`traits: tuple[PersonaTrait, ...]`, a closed enum `brief`, `warm`, `direct`,
`formal`, `playful`, at most three, unique, with `formal` and `playful`
mutually exclusive. `PUT /v1/persona` carries both under the same
`expected_version`; the CLI gains `agent persona voice` and `traits`.

- Voice text passes the same write-time secret refusal and load-time injection
  scan as entries (persona-surface.md:300-308); a blocked voice renders as the
  `[BLOCKED]` placeholder.
- Traits render as fixed server-owned sentences (for example `brief` → "Prefer
  short replies; expand when asked."), so a trait adds no free text.
- The row renders, in order: a fixed framing line ("The owner's preferences
  for tone and standing facts. They do not change approvals, limits or tool
  rules."), the voice, the trait sentences, then the enabled entries. All of
  it counts against the existing 2,000-token cap; an over-cap persona still
  fails session open with the class named.
- A document with no voice, no traits and every entry enabled renders
  byte-identically to today, so existing sessions rotate no epoch at deploy;
  the empty persona still renders zero bytes.

### B. A per-entry `enabled` switch

`PersonaEntry` and `PersonaEntryDraft` gain `enabled: bool = True`. A disabled
entry is kept in the document, still counts toward the thirty-entry cap, still
passes the write-time secret scan, and is not rendered. Toggling is an
ordinary `PUT` and therefore a new version and one `persona_changed` rotation
at the next plan. Stored versions without the field load as enabled; the
migration adds no backfill.

A disabled affirmed entry keeps its belief's promotion marker, so the belief
is not nominated again. The recommended rule is that the session-open snapshot
exclusion (persona-surface.md:218-225) applies only while the entry is
enabled, so a disabled entry's belief returns to being ordinary memory. The
switch therefore means "not a standing instruction", and the client copy must
say so: "Turn one off to stop Veetbot treating it as a standing instruction.
Memory keeps what it learned; review it in Memory to change that." Making a
switch also suppress recall would be a second, persona-owned memory
suppression path beside review and delete; it is offered only as the
alternative.

### C. Sample reply from a bounded model call (recommended)

`POST /v1/persona/preview` under `persona.write` takes the unsaved
`{voice, traits}` and returns `{reply, generated_at}`. It never receives
entries, memory, People, history or tools; the user turn is one fixed,
data-free prompt from a server catalog (for example, "Can you move my 3 pm to
tomorrow?"). Output is capped at 120 tokens and USD 0.01, rate-limited to 20
previews per principal per hour, and neither stored nor logged with content.
The voice is scanned first, and a refusal is `malformed_request`. The client
labels it "Example — generated, not saved".

A deterministic client template is the alternative and the fallback when the
route is absent: it can reflect only the chips, never the voice text, which is
the field the owner most wants to hear; showing it as "How that sounds" would
misrepresent the voice. When used it is labelled "Illustration".

### D. Boundaries come from the server (recommended)

`GET /v1/persona/boundaries` under `persona.read` returns a list of
`{id, text, enforced_by, active}` from a catalog in code. Each item names the
fact that enforces it (`enforced_by` is `hardline_rule`, `policy_rule`,
`configuration` or `code_path` with the rule or setting id), and a gate case
fails the build if a cataloged fact is missing from the loaded policy or
composition. Text that depends on configuration is chosen from the live
composition, so the page states actual behavior.

Static client copy is rejected because the designed list already disagrees
with enforcement and must be corrected before it can be shown:

- "Nothing sent without confirmation" and "messages always ask": with
  `AGENT_CALL_OWNER_REQUESTS_ENABLED=1` an owner-requested call dials without a
  card (ADR-0213), and a browser task grant approves actions for thirty
  minutes (ADR-0129).
- "Website sign-ins stay on device": provider profile material is stored
  encrypted outside PostgreSQL on the server side (browser-automation.md:228-232).
- "Single-conversation memories flagged for review": the flag marks beliefs
  committed without an explicit owner statement at sensitive or restricted
  sensitivity (ADR-0117).
- Payments, passwords, purchases and account changes: each claim must be
  matched to a rule in `hardline.yaml` or `policy/default.yaml` before it is
  cataloged; unmatched claims are dropped, not softened.

### E. Nominations are unchanged

Affirm and decline, durable decline and staleness keep their current design.

## Consequences

- The owner shapes tone without writing entries, and can park an entry
  without losing it.
- No policy, approval, limit or scope changes; persona text remains
  configuration-trust prompt text below deterministic enforcement.
- Part C adds a small billed call; part D adds a gate that ties displayed
  promises to enforcement.

## What changes if accepted

- Domain and storage: `PersonaDocument.voice`, `.traits`,
  `PersonaEntry.enabled`, the view fields (and the missing staleness flag),
  load defaults for stored versions.
- Routes: `PUT`/`GET /v1/persona` bodies (A, B); `POST /v1/persona/preview`
  (C); `GET /v1/persona/boundaries` (D); all behind `persona_api_enabled`.
  The six-route count (persona-surface.md:54) becomes eight.
- Gates: render byte-stability for legacy documents, cap including voice and
  traits, secret refusal and injection blocking for voice, disabled entries
  unrendered and snapshot-eligible, preview isolation (no entries, tools or
  memory reach the call) and budget, boundary catalog binding.
- Specs: persona-surface.md scope, prefix row, snapshot interaction, routes
  and safety sections; context-engine.md persona row.
- Client: Voice editor, chips, sample reply, switches, Boundaries list.

## Alternatives considered

- **Voice as an ordinary entry.** Rejected: it would compete for the entry
  cap, be nominable and switchable, and could not be rendered first.
- **Free-text traits.** Rejected: a closed set adds no injection surface.
- **Expose hardline policy over HTTP.** Rejected: it is tool-classification
  data, not owner-readable promises, and most boundaries live elsewhere.
- **Per-surface voices.** Out of scope by persona-surface.md:69-71.
