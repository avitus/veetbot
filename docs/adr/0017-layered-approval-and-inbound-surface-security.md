# ADR-0017: Layered approval and inbound-surface security

- Status: Accepted
- Date: 2026-07-20
- Related: ADR-0005 (deterministic policy engine), ADR-0011 (multi-device), Sections 9, 22, 29

## Context

The plan's approval flow is a single deterministic policy gate. Hermes adds
patterns worth borrowing: **hardline rules that are never bypassable** (even in
its "YOLO" mode) and **frozen at import** so in-process code cannot flip the
mode; an **LLM-assisted approval** as a secondary signal (injection-hardened by
stripping comments and XML-delimiting the untrusted command); **tiered credential
scrubbing** with **fail-closed env passthrough** (which closed a real CVE); and
**default-deny pairing** for untrusted inbound messaging. Hermes is candid that
only OS-level isolation is a true boundary — so these are defense-in-depth, not
replacements for isolation (ADR-0008).

## Decision

1. Keep the **deterministic policy engine as the primary, authoritative gate**.
2. Add **non-bypassable hardline rules, frozen at load**, that no mode, config,
   or in-process code can disable (e.g. destructive-command and secret-exfil
   patterns).
3. Add an **optional LLM-assisted approval as a SECONDARY signal** that can only
   make a decision **more** restrictive (never override a deny), injection-
   hardened (strip comments; XML-delimit untrusted input). It never replaces
   deterministic policy or human approval.
4. Adopt **tiered credential scrubbing** for child runs and subprocesses, with
   env passthrough that **fails closed** on platform/provider credentials.
5. **Default-deny untrusted inbound surfaces** (messaging channels) with explicit
   **pairing** (one-time code, expiry, rate-limit, lockout) before any run is
   created on a sender's behalf (Section 29).

## Consequences

- Defense-in-depth without weakening the deterministic core.
- The LLM-assisted layer adds latency/cost and must never be load-bearing for
  safety; hardline rules must be scoped carefully to avoid false blocks.

## Alternatives considered

- **Deterministic-only**: kept as the primary gate; these additions are additive.
- **LLM-as-primary-gate**: rejected; nondeterministic and injectable.
