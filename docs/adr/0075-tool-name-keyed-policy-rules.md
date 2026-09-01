# ADR-0075: Tool-name-keyed policy rules and declared human confirmation

- Status: Accepted
- Date: 2026-09-01
- Related: engineering plan Sections 9.2, 9.3, and 11.2; ADR-0017, ADR-0073
- Detailed design: `docs/plan/policy-and-approvals.md`,
  `docs/plan/device-channel-and-sms.md`

## Context

ADR-0073 decided that `device.sms.send` classifies `ALLOW`, because iOS makes
the owner's Send tap non-bypassable and a second in-app approval would duplicate
rather than add control. Implementing that decision exposed three things the
ADR did not settle, all of which are authorization mechanics and therefore
belong in an ADR of their own.

**First, there was no seam.** The deterministic layer is a total matrix keyed on
`SideEffectClass`. `device.sms.send` is `EXTERNAL_MESSAGE`, whose row requires
approval, and the loader's profile keys and rule keys are closed sets with no
per-tool override. The only way to reach `ALLOW` without a new seam was to relax
the `EXTERNAL_MESSAGE` row, which would have changed the decision for every
messaging tool in the system to buy one tool's exemption.

**Second, `ALLOW` alone was unreachable anyway.** The trust overlay raises any
non-read `ALLOW` to `REQUIRE_APPROVAL` when an argument carries
`EXTERNAL_UNTRUSTED`, and `_argument_trust()` labels every model-authored
argument exactly that. A matrix seam without an overlay change would have
produced an entry that never fires.

**Third, the first narrowing suppressed the wrong half.** The initial
implementation kept the overlay's origin test and dropped its argument test,
and described that as "untrusted input can never drive one of these entries to a
plain allow". A security review falsified the claim in three ways:

- `_turn_origin_trust` (tools/executor.py) walks back only to the most recent
  `USER`-trust user message. Origin trust is therefore scoped to the active
  turn. Turn one ingests attacker content and is tainted; the owner then types
  "ok"; turn two carries a `USER` origin and an attacker-chosen recipient and
  body reaches a plain `ALLOW`.
- Probes reached `ALLOW` with `origin_trust` of `MEMORY` and of `KNOWLEDGE`.
  The trust table in policy-and-approvals.md marks both "May authorize: No".
  They passed only because the overlay tested equality against one label, and
  `KNOWLEDGE` collapses into `EXTERNAL_UNTRUSTED` today by accident of
  `_turn_origin_trust` rather than by rule.
- `device.sms.send` was absent from `FORBIDDEN_CHILD_TOOLS` while delegated
  child runs seed at `USER` trust, giving a third laundering path.

## Decisions

1. **A `tool_rules` section, keyed on the exact tool name.** A profile may map a
   tool name to a row of the same shape as a side-effect row. When an action's
   name matches, that row decides for that one tool. The side-effect matrix
   stays total and every other tool in the class keeps the class's decision.
   This is the seam a milestone uses instead of editing a matrix row, and it
   keeps the normative Section 9.2 table intact and readable as written.
   Loader validation is closed-key and strict, matching the rest of the profile:
   a malformed section, an invalid tool name, an unknown field, a missing or
   unrecognized decision, or an unrecognized condition all refuse to load. Two
   entries for one tool name are unclassifiable and deny.

2. **Overlay suppression is opt-in through `human_confirms_arguments: true`.**
   A tool rule narrows nothing by itself; `decision: allow` without the flag
   gets no suppression and the argument half of the trust overlay applies as
   usual. The flag is a specific, checkable claim about the named tool: it shows
   the arguments to the human who completes the action. Making it explicit
   rather than implied by the entry's existence means a future entry cannot
   inherit an exemption it has not earned, and it gives review something to
   argue with.

3. **The origin half follows the trust table's "May authorize" column.** For a
   tool-ruled action, only `PLATFORM`, `TRUSTED_CONFIGURATION`, and `USER`
   origins reach a plain `ALLOW`; `INTERNAL_TOOL`, `MEMORY`, `KNOWLEDGE`,
   `EXTERNAL_UNTRUSTED`, and any future non-authorizing label escalate to
   `REQUIRE_APPROVAL`. This replaces an equality test against one label with the
   rule Section 11.2 already states, and it stops depending on `KNOWLEDGE`
   collapsing into `EXTERNAL_UNTRUSTED` by accident. The matrix path's overlay
   is left exactly as it was; this narrowing applies only where a tool rule
   fired.

4. **The real trust model is the compose sheet, and the ADR says so.** The
   primary, non-bypassable control for `device.sms.send` is the platform's own
   confirmation: iOS shows the owner the recipient and the body and does not
   send without a tap (ADR-0073). Policy's origin escalation is defense in
   depth layered on top of it, not the thing that makes the tool safe.

5. **The cross-turn residual is accepted and named.** Because origin trust is
   turn-scoped, an injection in one turn followed by any owner message produces
   an authorizing origin on the next. Policy does not catch this and no
   turn-scoped signal can. The accepted residual is that the owner sees the
   attacker-chosen recipient and body in the compose sheet and can decline. This
   is written into `policy-and-approvals.md` so no future reader mistakes the
   origin check for containment. Closing it properly needs a durable per-session
   taint that survives user messages, which is a context-engine change and is
   not in scope here.

6. **`device.sms.send` joins `FORBIDDEN_CHILD_TOOLS`.** A delegated child seeds
   at `USER` trust, so any tool a child may call is reachable from whatever
   authored the brief. Drafting a reply is a triage-session behavior under the
   owner's eye, never a child-run behavior. This is a rule about the tool, not
   about trust labels, so it holds regardless of how child seeding evolves.

7. **Ingest-path turns are untrusted by construction.** The SMS ingest path
   seeds its triage turns with device-originated untrusted content and stamps
   that trust on the seed, so a triage turn's origin is non-authorizing and its
   `device.sms.send` proposals escalate to approval. That is what
   `device-channel-and-sms.md`'s statement about untrusted turns rests on, and
   it is a gate-10 obligation on the ingest work rather than something the
   policy layer can assert on its own.

## Consequences

- The profile schema grows one optional section and one rule property. Existing
  profiles without `tool_rules` load unchanged.
- `PolicyDecision.reason_code` gains the `policy.tool.{name}` form alongside
  `policy.matrix.{class}`, so an audit trail says which layer decided.
- `LoadedRuleset` gains `tool_rules`; the ruleset stays frozen and hashable, and
  `policy_version` still covers the whole document.
- A reviewer auditing exemptions can list them: every entry in `tool_rules` with
  `human_confirms_arguments: true` is a claim that a human sees the arguments,
  and there is exactly one today.

## Alternatives considered

- **Relax the `EXTERNAL_MESSAGE` row with a condition.** Rejected: the row is
  normative in Section 9.2 and shared by every messaging tool; a per-tool
  predicate hidden in a class row is harder to audit than a per-tool entry.
- **Suppress the overlay entirely for a tool rule.** Rejected: it discards the
  defense-in-depth layer for no gain, and it was the shape the security review
  falsified.
- **Model the compose-sheet tap as standing authorization.** Rejected: it keeps
  the decision at `REQUIRE_APPROVAL` and satisfies it with evidence, which
  contradicts ADR-0073's plain statement that policy classifies the tool
  `ALLOW`, and it would create an approval record for an approval that never
  happened in the product.
- **A durable per-session taint instead of turn-scoped origin trust.**
  Deferred, not rejected: it is the real fix for the cross-turn residual, it
  changes context-engine behavior for every tool rather than one, and it wants
  its own design and its own gates.
