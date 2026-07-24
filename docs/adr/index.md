---
title: Architecture Decisions
---

# Architecture decision records

ADRs record deliberate architectural decisions. They are canonical Markdown. A
material architectural change requires a new ADR; requirements in the
[engineering plan](../plan/engineering-plan.md) must not be weakened to match an
implementation without one.

- [ADR-0007 — Provider-neutral reasoning state](0007-provider-neutral-reasoning-state.md)
- [ADR-0008 — Sandbox isolation](0008-sandbox-isolation.md)
- [ADR-0009 — Run, turn, and session model](0009-run-turn-session-model.md)
- [ADR-0010 — Live event transport](0010-live-event-transport.md)
- [ADR-0011 — Multi-device operation and the shared core](0011-multi-device-shared-core.md)
- [ADR-0012 — Open and self-hosted model support](0012-open-and-self-hosted-models.md)
- [ADR-0013 — Self-improving skills](0013-self-improving-skills.md)
- [ADR-0014 — Memory surface and external providers](0014-memory-surface-and-external-providers.md)
- [ADR-0015 — Programmatic tool orchestration](0015-programmatic-tool-orchestration.md)
- [ADR-0016 — Trajectory capture and export](0016-trajectory-capture-and-export.md)
- [ADR-0017 — Layered approval and inbound-surface security](0017-layered-approval-and-inbound-surface-security.md)
- [ADR-0018 — Memory formation and consolidation](0018-memory-formation-and-consolidation.md)
- [ADR-0019 — Memory retrieval and ranking](0019-memory-retrieval-and-ranking.md)

ADRs 0001–0006 are referenced by the engineering plan as foundational decisions
(modular monolith, provider-neutral model protocol, event log and projections,
Postgres run queue, deterministic policy engine, no private reasoning storage);
their records will be added when the repository foundation is created.
