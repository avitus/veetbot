---
title: Architecture Decisions
---

# Architecture decision records

ADRs record deliberate architectural decisions. They are canonical Markdown. A
material architectural change requires a new ADR; requirements in the
[engineering plan](../plan/engineering-plan.md) must not be weakened to match an
implementation without one.

- [ADR-0001 — The modular monolith and enforced boundaries](0001-modular-monolith.md)
- [ADR-0002 — The provider-neutral model protocol](0002-provider-neutral-model-protocol.md)
- [ADR-0003 — Event log, projections, and checkpoints](0003-event-log-and-projections.md)
- [ADR-0004 — The Postgres run queue, leases, and recovery](0004-postgres-run-queue.md)
- [ADR-0005 — The deterministic policy engine](0005-deterministic-policy-engine.md)
- [ADR-0006 — No private reasoning storage](0006-no-private-reasoning-storage.md)
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
- [ADR-0020 — Context engine](0020-context-engine.md)
- [ADR-0021 — Tool execution pipeline, effect watermarking, and MCP](0021-tool-execution-pipeline-and-mcp.md)
- [ADR-0022 — The gate registry, evaluation identity, and the capability track](0022-evaluation-harness.md)
- [ADR-0023 — The run loop, the step, and the single terminal writer](0023-runtime-loop.md)
- [ADR-0024 — The composition root and the configuration layers](0024-bootstrap-and-composition.md)
- [ADR-0025 — The development toolchain and the meaning of `make check`](0025-development-toolchain.md)
- [ADR-0026 — The builtin tool roster and the two Milestone 1 tools](0026-builtin-tools.md)
- [ADR-0027 — Gate milestones, the declaration form, and the milestone map](0027-milestone-map.md)
- [ADR-0028 — The HTTP API surface, the error vocabulary, and the event stream](0028-http-api-and-streaming.md)
- [ADR-0029 — Isolated execution, the egress boundary, and the artifact store](0029-sandbox-isolation-and-artifacts.md)
- [ADR-0030 — The skill package, the pinned catalog, and the authoring loop](0030-skills-and-the-authoring-loop.md)
- [ADR-0031 — The ORM surface and the migration conventions](0031-persistence-authoring.md)
- [ADR-0032 — Trajectory export, redaction, and consent](0032-trajectory-export-redaction-and-consent.md)
- [ADR-0033 — The knowledge document, its corpus, and passage retrieval](0033-knowledge-documents.md)
- [ADR-0034 — Section 29 as an audited seam rather than a design](0034-multi-device-and-surfaces-seam.md)
- [ADR-0035 — CircleCI as the hosted CI provider](0035-circleci-hosted-ci.md)
- [ADR-0036 — The executable 106-knob configuration inventory](0036-configuration-default-inventory.md)
- [ADR-0037 — Milestone 1 in-memory seam decisions](0037-milestone-1-in-memory-seams.md)
- [ADR-0038 — Milestone 2 durable-runtime seam decisions](0038-milestone-2-durable-runtime-seams.md)
- [ADR-0039 — Milestone 3 provider and trajectory-export seams](0039-milestone-3-provider-and-export-seams.md)
