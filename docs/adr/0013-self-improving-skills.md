# ADR-0013: Self-improving skills (agent-authored procedural memory)

- Status: Accepted
- Date: 2026-07-20
- Related: ADR-0008 (sandbox isolation), Milestone 8 (skills), Section 30

## Context

Skills in the plan are static, human-authored packages (Milestone 8). Nous
Research's Hermes Agent ships **agent-authored procedural memory**: the agent
creates and edits `SKILL.md` files (instructions, not new code tools), both in
the foreground and via an autonomous post-run "background review" pass. Hermes
deliberately does **not** govern this (its own threat model notes the agent can
already run code via the terminal). Our architecture — versioned config, a
deterministic policy engine, approvals, an event-sourced audit trail, and real
sandbox isolation — lets us offer the same self-improvement **with governance**.

## Decision

1. Treat skills as **procedural memory** the agent may author and refine, via a
   `skill_manage` control tool and an optional post-run **background-review child
   run**.
2. Governance: every agent-authored skill version is **pinned per run** (like
   AgentSpec) and **provenance-linked** to the source events that produced it;
   authoring is a **consequential action** gated by policy/scope/approval; the
   background-review pass runs as a **restricted child run** (whitelisted tools
   `{memory, skills}`, read-before-write, edits only skills it created); any
   executable script a skill carries runs in the sandbox (ADR-0008) under normal
   policy.
3. Load only skill **metadata** into ordinary context; load full instructions on
   selection. Adopt the **agentskills.io** format for interoperability.
4. Gate rollout behind evaluation evidence that self-authored skills improve
   defined eval cases without increasing policy failures.

## Consequences

- A genuine self-improvement loop with **audit and rollback** (via
  versioning/provenance) that a local-first agent cannot match.
- Added policy surface and review cost.
- Must prevent prompt-injection-driven skill writes: authoring is restricted to
  trusted turns and scanned at load.

## Alternatives considered

- **Static skills only**: rejected; misses the core "learns from experience"
  capability that defines a modern general-purpose agent.
- **Ungoverned self-editing (Hermes-style)**: rejected; unacceptable for a
  multi-tenant, production system.
