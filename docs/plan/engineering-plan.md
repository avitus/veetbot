---
title: Modular General-Purpose AI Agent Engineering Plan
status: normative
canonical: true
source_document: archive/Modular_General_Purpose_AI_Agent_Engineering_Plan.docx
version: "2.3"
---

# Modular General-Purpose AI Agent Engineering Plan

*Comprehensive Engineering Brief and Implementation Plan*

**Purpose.** This document defines the architecture, interfaces, security model, persistence model, API contracts, testing strategy, implementation sequence, and acceptance criteria for a modern, modular, general-purpose AI agent platform.

## Revision summary (version 2.0)

Version 2.0 incorporates a production-readiness design review of version 1.0. The core architecture is unchanged: the modular monolith, ports and adapters with enforced import boundaries, the append-only event log with projections, the PostgreSQL-backed durable worker, the deterministic policy engine, trust-labeled context, idempotent tool execution, and the hard milestone gates all stand. The changes below sharpen the model-provider abstraction for reasoning models, resolve the run/turn/session model, specify sandbox isolation, and close several production lifecycle gaps.

Substantive changes:

- Co-equal first providers. The OpenAI Responses adapter and the Anthropic Messages adapter are now both first-class real adapters, so the normalized protocol is designed against two materially different provider shapes from the start (Sections 2.3, 10.4; ADR-0007).
- Reasoning as a first-class concern. New handling for provider-opaque reasoning state: transient display events, verbatim in-loop passback (Anthropic thinking blocks with signatures; OpenAI reasoning items and reasoning.encrypted_content), reasoning-token accounting, and provider pinning (Sections 6.6, 6.9, 10.1, 10.2, 10.6; amends ADR-0006 via ADR-0007).
- Prompt-cache intent in the request. ModelRequest can express cache breakpoints, reconciling Anthropic explicit cache_control with OpenAI automatic prefix caching (Section 10.1).
- Usage and cost by token class. Uncached input, cached input, output, and reasoning tokens are tracked and priced separately (Section 6.5).
- Live event transport. The event broadcaster uses PostgreSQL LISTEN/NOTIFY for worker wakeup (lower claim latency) and for delivering transient stream events; the claim query gains priority ordering so asynchronous jobs cannot head-of-line-block interactive turns (Sections 14, 16; ADR-0010).
- Run, turn, and session model. New Section 27 defines run == turn, cross-run continuity from the session log, mid-run user input via WAITING_FOR_USER and POST /runs/{id}/input, and single-active-run-per-session concurrency (ADR-0009).
- Sandbox isolation architecture. New Section 28 selects a kernel-isolating runtime (microVM or gVisor) over a bare Docker socket, and defines a least-privileged execution-service topology (ADR-0008).
- Lifecycle edges closed. An approval-expiry reaper and cancellation of QUEUED and WAITING runs are handled by the application service (Section 9.3).
- Event schema versioning. Persisted event payloads carry a schema version with an upcasting read path (Section 6.8; amends ADR-0003).
- Capability evaluations. A non-deterministic, live, out-of-CI evaluation track complements the deterministic suite (Section 20).
- Per-tenant fairness. Per-tenant concurrency and rate limits are added to the security baseline (Section 22).

Version 2.1 additions:

- Copy-ready streaming mappings. Section 10.2 now includes exact Anthropic and OpenAI streaming-event to normalized-event tables, tool-call argument assembly, and a usage-field mapping.
- Multi-device shared core. New Section 29 identifies every cloud-shared component (not only memory), introduces device-scoped capabilities and the Device concept, and defines cross-device continuity and approvals (ADR-0011).
- ADR files drafted. ADR-0007 through ADR-0011 are written as markdown under docs/adr/.

Version 2.2 additions (informed by a review of Nous Research’s Hermes Agent):

- Open and self-hosted models. A co-equal OpenAI-compatible chat_completions adapter and declarative provider plugins bring vLLM/Ollama/LM Studio/OpenRouter and self-hosted models into the same normalized protocol (Sections 2.3, 10.5, 10.7; ADR-0012).
- Per-provider reasoning matrix and in-band think handling. Section 10.6 adds a reasoning-handling table and a streaming think-scrubber for open models.
- Prompt-stability cache invariant. The cacheable prefix is built once per session and kept byte-stable; volatile context goes in the user turn (Sections 10.1, 11).
- Cost by source precedence and additive fan-out usage (Section 6.5).
- Self-improving skills. New Section 30 adds governed, agent-authored procedural memory with versioning, provenance, and policy gating (ADR-0013).
- Human-editable, injection-scanned memory with a frozen per-session snapshot and pluggable external providers (Milestone 9; ADR-0014).
- Programmatic tool orchestration. New Section 8.5: the model writes code that calls tools via an in-sandbox RPC bridge, still policy-gated per call (ADR-0015).
- Trajectory capture and export. New Section 31 turns real runs into eval fixtures and fine-tuning data (ADR-0016).
- Layered approval and inbound-surface security. Hardline non-bypassable rules, optional LLM-assisted approval as a secondary signal, credential scrubbing, and pairing for untrusted surfaces (Sections 9, 22, 29; ADR-0017).

Version 2.3 (build-sequencing pass):

- Section 21.1 sequences the version 2.2 additions across milestones by cost-to-defer, with a summary table.
- Milestone 3 is rescoped to model adapters (OpenAI, Anthropic, OpenAI-compatible); the chat_completions adapter and a minimal trajectory export move here, adding a no-cost local live-test path.
- Cheap data-model decisions (reasoning event, usage token classes, prompt-stability, event versioning) are pulled into Milestones 1-2; programmatic orchestration and credential scrubbing into Milestone 6.
- Self-improving skills and inbound messaging surfaces are kept deliberately late.

## 1. Mission

Build a modern, general-purpose AI agent platform with a small, durable core and replaceable modules for:

- Model providers
- Context construction
- Tools
- Execution environments
- Policies and approvals
- Session persistence
- Memory
- Skills
- Artifacts
- Observability
- Evaluations
- Scheduling
- Subagents

Begin with a **single-agent modular monolith**. Do not begin with microservices or a multi-agent architecture.

The first usable version must support a complete vertical slice:

```text
User request
    -> durable run
    -> context construction
    -> model call
    -> optional tool call
    -> policy evaluation
    -> tool execution
    -> checkpoint
    -> final response
    -> replayable trace
```
The design must make every major component replaceable without changing the central domain model or event format.

The platform is a shared cloud core with many thin device clients: PostgreSQL holds the single source of truth, and any device attaches to it. The components that must be cloud-shared, and the device-scoped exception, are enumerated in Section 29.

## 2. Fixed architectural decisions

### 2.1 Architecture

Use a modular monolith with explicit interfaces between modules.

ADR-0001 records this decision, defines replaceability as a port with a contract suite attached to it rather than as a service boundary, and names the mechanism that enforces the Section 5 dependency rules.

```text
+------------------------------------------------------------+
|                    Entry Points                            |
|             FastAPI / CLI / Worker                         |
+-------------------------+----------------------------------+
                          |
+-------------------------v----------------------------------+
|                  Application Layer                         |
|       Agent Runtime / Run Service / Approval Service       |
+----------+----------+----------+----------+----------------+
           |          |          |          |
+----------v---+ +----v-----+ +--v------+ +-v--------------+
|Model Gateway | |Tool Layer| | Context | | Policy Engine |
+----------+---+ +----+-----+ +--+------+ +-+--------------+
           |          |          |          |
+----------v----------v----------v----------v---------------+
|                         Ports                              |
| Repositories / Artifact Store / Sandbox / Event Bus / OTel|
+----------+----------+----------+----------+---------------+
           |          |          |          |
+----------v---+ +----v-----+ +--v------+ +-v--------------+
|  PostgreSQL  | | Providers| |Container| |Object Storage |
+--------------+ +----------+ +---------+ +---------------+
```
### 2.2 Technology choices

Use:

- Python 3.12 or newer
- `uv` with `pyproject.toml`
- FastAPI for the HTTP API
- Server-Sent Events for run streaming
- Pydantic for domain and transport schemas
- SQLAlchemy async plus Alembic
- PostgreSQL as the authoritative database
- A local filesystem artifact adapter initially
- S3-compatible object storage later
- Docker-compatible containers for sandbox execution
- OpenTelemetry for tracing, metrics, and log correlation
- `pytest`, `pytest-asyncio`, `ruff`, and `mypy`
- A Typer-based CLI
- GitHub Actions or the existing CI system

FastAPI supports asynchronous endpoints and streaming responses, making it appropriate for model and tool workloads that spend substantial time awaiting external systems. See the [FastAPI documentation](https://fastapi.tiangolo.com/).

Use SQLAlchemy’s async interface, but never share one `AsyncSession` across concurrent tasks. Each request, worker operation, or parallel tool invocation must receive its own unit of work and database session. See the [SQLAlchemy asyncio documentation](https://docs.sqlalchemy.org/en/latest/orm/extensions/asyncio.html).

Use OpenTelemetry-compatible instrumentation so traces, metrics, and logs can be correlated without making the domain layer depend on a particular observability vendor. See the [OpenTelemetry Python documentation](https://opentelemetry.io/docs/languages/python/getting-started/).

### 2.3 First model adapter

Implement:

1.  A deterministic fake model adapter
2.  An OpenAI Responses API adapter

- An Anthropic Messages API adapter
- An OpenAI-compatible (chat_completions) adapter for open-weights and self-hosted models

Use the Responses API rather than an agent framework for the first real adapter because the application must own the model/tool loop, state, routing, and approval lifecycle. See the [OpenAI agents guide](https://developers.openai.com/api/docs/guides/agents).

Version 2.0 decision: implement the OpenAI Responses adapter and the Anthropic Messages adapter as co-equal first real adapters, not OpenAI alone. Designing the normalized model protocol against two materially different provider shapes at once is the strongest available test of provider-neutrality and prevents the abstraction from ossifying around a single vendor. See ADR-0007.

Both first adapters target reasoning-capable models. The normalized protocol must therefore treat provider-opaque reasoning state as a first-class concern (Section 10.6 and ADR-0007): reasoning tokens are billed, and during tool use some providers require the reasoning to be returned verbatim on the next request or they reject it.

Open and self-hosted models are first-class, not an afterthought: the OpenAI-compatible chat_completions adapter reaches vLLM, Ollama, LM Studio, OpenRouter, and any BYO endpoint (including self-hosted Hermes/Llama/Qwen). This is what makes the platform genuinely provider-neutral and gives cost, residency, offline, and fine-tuning control. See Section 10.7 and ADR-0012.

Do not import OpenAI SDK types outside the OpenAI adapter.

Do not hard-code a model name. Read it from configuration.

### 2.4 Persistence

PostgreSQL is the source of truth for:

- Sessions
- Runs
- Events
- Checkpoints
- Tool invocations
- Approvals
- Artifact metadata
- Usage records
- Later, memory metadata

Use an append-only event log plus normalized projection tables.

Do not depend exclusively on provider-managed conversation state.

### 2.5 Security

The model may propose actions, but deterministic application code must decide whether those actions are permitted.

```text
Model proposes
    -> schema validation
    -> authorization
    -> policy evaluation
    -> optional human approval
    -> controlled execution
```
A prompt instruction is not an authorization mechanism.

### 2.6 Scope discipline

Do not implement the following in the initial version:

- Multiple cooperating agents
- Long-term autonomous memory
- A vector database
- A browser automation system
- Computer-use automation
- Email or calendar integrations
- Scheduled autonomous jobs
- Voice interaction
- Kubernetes
- Microservices
- Redis or a separate queue unless PostgreSQL becomes demonstrably inadequate
- A visual workflow builder

Create interfaces for later additions, but do not build speculative implementations.

## 3. Version 0.1 definition of done

Version 0.1 is complete when all of the following work:

1.  A user can create a session through the CLI or API.
2.  A user can submit a message and receive a run ID.
3.  A durable worker claims and executes the run.
4.  The runtime can call either the fake model or OpenAI.
5.  The model can return text or request one or more tools.
6.  Tool arguments are validated before execution.
7.  Tool policy is evaluated deterministically.
8.  Sensitive actions can pause for approval.
9.  An approved run can resume from its checkpoint.
10. A denied action produces a structured tool result and allows the model to respond appropriately.
11. A run can be cancelled.
12. Run events can be streamed over SSE.
13. A disconnected SSE client can reconnect and replay missed persisted events.
14. A worker can restart without losing the run.
15. Tool execution is protected against accidental duplicate execution.
16. Files created in a sandbox can be exported as artifacts.
17. Every model call and tool call has trace, latency, status, and usage metadata.
18. A deterministic evaluation suite runs in CI without requiring an API key.
19. Live provider tests are available behind an explicit environment flag.
20. The code passes formatting, linting, typing, unit tests, and integration tests.

## 4. Repository structure

The detailed design - the settings object and the eight environment values that survive the test "differs between two deployments of the same revision and cannot be committed"; the three configuration layers and why the environment is interpolated into files at named points rather than allowed to override them; the six configuration files, the operator overlay directory, and the one file the overlay may not touch; `.env.example` reconciled with the 106 knobs the corpus declares; the composition root's five startup phases and where each of the seventeen stated startup constraints lands; the shape of `build` and what a `Composition` may expose; the three entry points and the deployment role each passes; the eleven files this tree gains and the one name it retires; the Milestone 1 in-memory repositories, the `RunDispatcher` Protocol, and the minimal context builder; the CLI's arguments, output streams, reserved words, and exit codes; and the secret scanner's five rule families - is specified in [bootstrap-and-composition.md](bootstrap-and-composition.md) and ADR-0024. That document expands Sections 4, 5, 7, 10.5, 10.7, 11.1, 15, 16, 17, 25, 26, and 28 and Milestones 0, 1, 2, and 3; it does not replace the requirements below, and it removes no file this tree names.

Use a `src` layout.

```text
agent-core/
|-- pyproject.toml
|-- uv.lock
|-- README.md
|-- Makefile
|-- .env.example
|-- .gitignore
|-- docker-compose.yml
|-- alembic.ini
|-- migrations/
|-- docs/
|   |-- architecture.md
|   |-- security.md
|   |-- events.md
|   `-- adr/
|       |-- 0001-modular-monolith.md
|       |-- 0002-provider-neutral-model-protocol.md
|       |-- 0003-event-log-and-projections.md
|       |-- 0004-postgres-run-queue.md
|       |-- 0005-deterministic-policy-engine.md
|       `-- 0006-no-private-reasoning-storage.md
|-- src/
|   `-- agent_core/
|       |-- domain/
|       |   |-- agents.py
|       |   |-- sessions.py
|       |   |-- runs.py
|       |   |-- events.py
|       |   |-- messages.py
|       |   |-- tools.py
|       |   |-- approvals.py
|       |   |-- artifacts.py
|       |   |-- policies.py
|       |   |-- errors.py
|       |   `-- identifiers.py
|       |-- ports/
|       |   |-- models.py
|       |   |-- repositories.py
|       |   |-- tools.py
|       |   |-- policies.py
|       |   |-- artifacts.py
|       |   |-- execution.py
|       |   |-- dispatch.py
|       |   |-- events.py
|       |   `-- telemetry.py
|       |-- application/
|       |   |-- run_service.py
|       |   |-- session_service.py
|       |   |-- approval_service.py
|       |   |-- artifact_service.py
|       |   `-- commands.py
|       |-- runtime/
|       |   |-- engine.py
|       |   |-- state_machine.py
|       |   |-- budgets.py
|       |   |-- retries.py
|       |   |-- checkpoints.py
|       |   `-- worker.py
|       |-- context/
|       |   |-- builder.py
|       |   |-- budget.py
|       |   |-- history.py
|       |   |-- compaction.py
|       |   |-- trust.py
|       |   `-- prompts.py
|       |-- models/
|       |   |-- registry.py
|       |   |-- routing.py
|       |   `-- capabilities.py
|       |-- tools/
|       |   |-- registry.py
|       |   |-- executor.py
|       |   |-- validation.py
|       |   |-- calculator.py
|       |   |-- current_time.py
|       |   |-- workspace.py
|       |   `-- sandbox.py
|       |-- policy/
|       |   |-- engine.py
|       |   |-- rules.py
|       |   |-- authorization.py
|       |   `-- redaction.py
|       |-- adapters/
|       |   |-- models/
|       |   |   |-- fake.py
|       |   |   `-- openai_responses.py
|       |   |-- persistence/
|       |   |   |-- sqlalchemy_models.py
|       |   |   |-- repositories.py
|       |   |   |-- unit_of_work.py
|       |   |   `-- database.py
|       |   |-- artifacts/
|       |   |   |-- filesystem.py
|       |   |   `-- s3.py
|       |   |-- execution/
|       |   |   |-- fake.py
|       |   |   `-- container.py
|       |   |-- dispatch/
|       |   |   |-- inline.py
|       |   |   `-- postgres.py
|       |   `-- telemetry/
|       |       `-- opentelemetry.py
|       |-- api/
|       |   |-- main.py
|       |   |-- dependencies.py
|       |   |-- auth.py
|       |   |-- errors.py
|       |   |-- schemas.py
|       |   `-- routes/
|       |       |-- sessions.py
|       |       |-- runs.py
|       |       |-- events.py
|       |       |-- approvals.py
|       |       `-- artifacts.py
|       |-- cli/
|       |   |-- main.py
|       |   |-- chat.py
|       |   |-- runs.py
|       |   |-- approvals.py
|       |   `-- evals.py
|       |-- evals/
|       |   |-- runner.py
|       |   |-- cases.py
|       |   |-- assertions.py
|       |   `-- fixtures.py
|       |-- config.py
|       `-- bootstrap.py
`-- tests/
    |-- unit/
    |-- contract/
    |-- integration/
    |-- resilience/
    |-- security/
    |-- live/
    `-- eval_cases/
```
Version 2.0 adds these ADRs (create the files as the milestones reach them):

- 0007-provider-neutral-reasoning-state.md - co-equal OpenAI and Anthropic adapters, and how provider-opaque reasoning is carried in-loop, excluded from logs and memory, and pinned per run.
- 0008-sandbox-isolation.md - the isolation mechanism and execution-service topology (Section 28).
- 0009-run-turn-session-model.md - run/turn/session boundaries and cross-run continuity (Section 27).
- 0010-live-event-transport.md - LISTEN/NOTIFY worker wakeup and live stream transport (Sections 14, 16).
- 0011-multi-device-shared-core.md - one shared cloud core with many thin device clients; cloud-shared vs device-local vs device-scoped components (Section 29).
- 0012-open-and-self-hosted-models.md - OpenAI-compatible chat_completions mode, provider plugins, in-band reasoning, cost-source precedence (Sections 10.5-10.7).
- 0013-self-improving-skills.md - governed agent-authored procedural memory (Section 30).
- 0014-memory-surface-and-external-providers.md - human-editable memory, frozen snapshot, external providers (Milestone 9).
- 0015-programmatic-tool-orchestration.md - model-written code calling tools via an in-sandbox RPC bridge (Section 8.5).
- 0016-trajectory-capture-and-export.md - real runs to eval fixtures and training data (Section 31).
- 0017-layered-approval-and-inbound-surface-security.md - hardline rules, LLM-assisted approval, pairing (Sections 9, 22, 29).

Do not create empty placeholder modules for every future feature. Create directories as the implementation reaches them.

## 5. Dependency rules

Enforce these rules:

1.  `domain` may depend only on the Python standard library and Pydantic.

    Note: allowing Pydantic in the domain is a deliberate pragmatic tradeoff (validation and serialization ergonomics) that does let an external library shape core types. Keep domain models free of Pydantic-only behavior that would be costly to replace - custom validators that perform I/O, settings loading, ORM modes - and treat them as plain value objects that happen to use BaseModel.

2.  `ports` may depend on `domain`.
3.  `runtime` and `application` may depend on `domain` and `ports`.
4.  Adapters implement ports and may depend on external SDKs.
5.  API and CLI modules call application services.
6.  Provider SDK objects must never cross adapter boundaries.
7.  SQLAlchemy ORM objects must never be returned from repositories.
8.  FastAPI request or response objects must never enter the application layer.
9.  Tool implementations must not call the model gateway.
10. The model gateway must not directly execute tools.
11. The policy engine must not depend on prompts or model judgment.
12. Secrets must never be stored in domain events.
13. No global singleton database sessions.
14. Use explicit dependency construction in `bootstrap.py`; do not add a dependency-injection framework.

Add an import-boundary test or static rule that verifies these constraints where practical.

[evaluation-harness.md](evaluation-harness.md) and ADR-0001 resolve "where practical" rule by rule rather than leaving it to judgement: eight of the fourteen rules above are decidable by walking the import graph, four need a different static check (signature resolution for rules 6 and 7, a module-scope check for rule 13, a dependency-manifest check for rule 14), rule 12 is the secret scanner, and rule 4 is the adapter-registration check. Two residues - the run-time half of rule 6 and "must not depend on model judgment" in rule 11 - are recorded as not mechanically checkable, with the contract suite and ADR-0005's determinism gate named as their compensating controls. The walk is a registered structural gate and a Milestone 0 deliverable. The fourteen rules themselves are unchanged.

[bootstrap-and-composition.md](bootstrap-and-composition.md) and ADR-0024 give rule 14 the module it names. That document states the rule as a property - the composition root is the only module that knows both a port and its adapter - and adds four static checks that make the property testable: `bootstrap` is imported only by the three entry points, no module outside `bootstrap.py` instantiates an adapter class, no module outside `adapters/determinism.py` reads ambient time or generates an identifier, and no `AsyncSession` exists at module scope. The second check is what rules 4 and 13 look like once there is a construction site to check against. All four run against an almost-empty repository. No rule above is changed, reordered, or relaxed.

## 6. Core domain objects

### 6.1 AgentSpec

An agent is configuration, not a complex subclass.

```python
class AgentSpec(BaseModel):
    id: str
    version: int
    name: str
    instructions: str
    model_policy: str
    enabled_tools: list[str]
    enabled_skills: list[str] = []
    policy_profile: str
    limits: "RunLimits"
    metadata: dict[str, Any] = {}
```
The initial repository may load one default agent from configuration. Persist agent versions before allowing users to edit them dynamically.

### 6.2 Principal

Every run must have an authenticated principal, even in a single-user deployment.

```python
class Principal(BaseModel):
    tenant_id: str
    principal_id: str
    roles: set[str]
    scopes: set[str]
```
All repository methods that read user-owned data must require tenant and principal context.

### 6.3 Session

```python
class Session(BaseModel):
    id: UUID
    tenant_id: str
    principal_id: str
    agent_id: str
    agent_version: int
    status: SessionStatus
    title: str | None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
```
### 6.4 Run

```python
class Run(BaseModel):
    id: UUID
    session_id: UUID
    parent_run_id: UUID | None
    status: RunStatus
    step_count: int
    model_call_count: int
    tool_call_count: int
    limits: RunLimits
    usage: RunUsage
    lease_owner: str | None
    lease_expires_at: datetime | None
    cancel_requested_at: datetime | None
    created_at: datetime
    updated_at: datetime
```
Use this state machine:

```text
QUEUED
  -> RUNNING
  -> WAITING_FOR_APPROVAL
  -> WAITING_FOR_USER
  -> COMPLETED
  -> FAILED
  -> CANCELLED
```
Only explicit transition functions may modify run status.

### 6.5 Run limits

```python
class RunLimits(BaseModel):
    max_steps: int
    max_model_calls: int
    max_tool_calls: int
    max_input_tokens: int | None
    max_output_tokens: int | None
    max_cost: Decimal | None
    deadline_at: datetime | None
```
Check limits before and after every model or tool operation.

Provider prices must come from configuration. Do not hard-code current pricing into application code.

Usage accounting must distinguish token classes. Per-call and per-run usage must track uncached input, cached input, output, and reasoning tokens separately:

```python
class RunUsage(BaseModel):
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    model_calls: int
    tool_calls: int
    cost: Decimal
```
Pricing configuration must provide per-model, per-class rates (uncached input, cached input, output, reasoning). Cached input is materially cheaper than uncached (roughly a quarter of the price on current OpenAI pricing) and reasoning tokens are billed as output; a cost model that ignores these classes will both misreport spend and mis-enforce max_cost.

Cost must also be sourced by a defined precedence, because providers report it inconsistently:

- Precedence: a provider cost field on the response \> the provider generation/usage API \> the model catalog price table \> a documented pricing snapshot \> an explicit configuration override.
- Record which source produced each cost (a typed cost_source) so spend can be audited and reconciled.
- Usage is additive across subagent fan-out: a parent run’s usage aggregates its children’s (Section 27.6), so budgets and max_cost bound the whole tree, not each run in isolation.

### 6.6 Conversation items

Create a provider-neutral conversation representation.

```python
ConversationItem = (
    SystemMessage
    | UserMessage
    | AssistantMessage
    | ToolCallItem
    | ToolResultItem
)
```
Content should support:

```python
ContentPart = TextPart | ImageReferencePart | FileReferencePart
```
The first version only needs text and artifact references, but the data model should be extensible.

Conversation items must also be able to carry provider-opaque continuation items - content the runtime stores and replays but never authors, interprets, or edits.

```python
ConversationItem = (
    SystemMessage
    | UserMessage
    | AssistantMessage
    | ToolCallItem
    | ToolResultItem
    | ProviderReasoningItem   # opaque, provider-tagged
)

class ProviderReasoningItem(BaseModel):
    provider: str                    # "openai" | "anthropic"
    opaque_payload: dict[str, Any]   # Anthropic thinking+signature or
                                     # redacted_thinking; OpenAI reasoning
                                     # item or reasoning.encrypted_content
    trust_level: TrustLevel = TrustLevel.PLATFORM
```
Rules for provider-opaque items: store them verbatim; never log, summarize, or place them in long-term memory; carry them in the checkpoint only for the life of the active tool loop; tag them with the originating provider; and drop or ignore them when a run is routed to a different provider, because one provider’s reasoning state is meaningless to (and rejected by) another. See Section 10.6 and ADR-0007.

### 6.7 Model capabilities

```python
class ModelCapabilities(BaseModel):
    tool_calling: bool
    parallel_tool_calls: bool
    structured_output: bool
    streaming: bool
    vision: bool
    audio: bool
    provider_managed_state: bool
    max_context_tokens: int | None
```
The model router must select only a model capable of satisfying the request.

### 6.8 Event envelope

Events are append-only.

```python
class EventEnvelope(BaseModel):
    id: int
    session_id: UUID
    run_id: UUID | None
    sequence: int
    event_type: str
    actor_type: str
    actor_id: str | None
    payload: dict[str, Any]
    trace_id: str | None
    created_at: datetime
```
Use a per-session monotonically increasing `sequence`.

Every persisted event payload must carry a schema version. Because the event log is the authoritative, replayable source of truth, payload shapes will change over time; add a payload_schema_version field to the envelope and an explicit upcasting step in the read path so historical events remain decodable after the code evolves. See ADR-0003 (amended).

Suggested persisted event types:

```text
session.created
user.message.created
run.queued
run.claimed
run.started
run.checkpointed
model.request.started
model.response.completed
assistant.message.completed
tool.call.proposed
tool.call.authorized
tool.call.denied
tool.call.started
tool.call.completed
tool.call.failed
tool.call.uncertain
approval.requested
approval.resolved
artifact.created
run.waiting_for_approval
run.resumed
run.completed
run.failed
run.cancelled
```
Token deltas may be streamed as transient transport events. Do not persist every token delta by default.

Reasoning and thinking deltas are likewise transient transport events. Never persist raw reasoning text (ADR-0006 as amended). Persist only that reasoning occurred and its token count; the provider-opaque continuation payload required to resume an active tool loop lives in the run checkpoint, not in the event log.

### 6.9 Checkpoint

```python
class RunCheckpoint(BaseModel):
    run_id: UUID
    version: int
    status: RunStatus
    conversation: list[ConversationItem]
    pending_tool_calls: list["PendingToolCall"]
    pending_approval_ids: list[UUID]
    working_state: dict[str, Any]
    compacted_summary: str | None
    budget_state: dict[str, Any]
    last_event_sequence: int
    created_at: datetime
```
Create a checkpoint after:

- A completed model response
- Every completed or failed tool call
- An approval request
- An approval resolution
- Context compaction
- Final completion or failure

The checkpoint is also where provider-opaque reasoning continuation lives for the duration of an active tool loop. Amend RunCheckpoint and add a continuation type:

```python
class RunCheckpoint(BaseModel):
    ...
    conversation: list[ConversationItem]      # may include ProviderReasoningItem
    provider_continuation: ProviderContinuation | None
    ...

class ProviderContinuation(BaseModel):
    provider: str
    previous_response_id: str | None   # OpenAI, when store=True
    opaque_items: list[dict[str, Any]] = []  # replayed verbatim next request
    valid_for_provider_only: bool = True
```
Checkpoint storage note: a checkpoint is written after every model response and every tool call, and each currently stores the full conversation. For long runs this is superlinear. Store the conversation as references to event IDs (or as deltas since the previous checkpoint) and reserve full inline snapshots for compaction boundaries; keep provider-opaque items inline, because they cannot be reconstructed from the event log.

## 7. Port interfaces

Define these interfaces before implementing adapters.

```python
class ModelProvider(Protocol):
    provider_name: str
    capabilities: ModelCapabilities

    async def stream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelEvent]:
        ...
```
The iterator must end with exactly one completed or failed event.

The `ModelProvider` signature above is superseded. [model-gateway.md](model-gateway.md) and ADR-0002 declare the canonical port: `provider_name` becomes `name` and matches the adapter key on `ResolvedModel`; `stream` gains the `ResolvedModel` and `ModelAttempt` the router has already produced, so no adapter resolves a model twice; `close` is added because a pooled HTTP client needs an owner and `bootstrap.py` is where ownership ends; and `capabilities` moves off the adapter onto `ModelRouter`, because a capability belongs to a resolved model rather than to the provider serving it - one provider serves models that differ in context window and in tool support. Implement the model-gateway signature; the one above remains here as the record of what it replaced. The rule stated immediately above is unchanged, and that document restates it as a contract-suite assertion.

```python
class ContextBuilder(Protocol):
    async def build(
        self,
        run: Run,
        checkpoint: RunCheckpoint,
        agent: AgentSpec,
        principal: Principal,
    ) -> ModelRequest:
        ...
class Tool(Protocol):
    spec: ToolSpec

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        ...
class PolicyEngine(Protocol):
    async def evaluate(
        self,
        proposed_action: ProposedAction,
        principal: Principal,
        run: Run,
    ) -> PolicyDecision:
        ...
class ArtifactStore(Protocol):
    async def put(
        self,
        stream: AsyncIterator[bytes],
        metadata: ArtifactMetadata,
    ) -> ArtifactRef:
        ...

    async def open(self, ref: ArtifactRef) -> AsyncIterator[bytes]:
        ...
class ExecutionEnvironment(Protocol):
    async def provision(
        self,
        specification: EnvironmentSpec,
    ) -> EnvironmentHandle:
        ...

    async def execute(
        self,
        environment: EnvironmentHandle,
        command: ExecutionCommand,
    ) -> ExecutionResult:
        ...

    async def destroy(self, environment: EnvironmentHandle) -> None:
        ...
class RunRepository(Protocol):
    async def create(self, run: Run) -> None: ...
    async def get(self, run_id: UUID, principal: Principal) -> Run: ...
    async def claim_next(self, worker_id: str) -> Run | None: ...
    async def heartbeat(self, run_id: UUID, worker_id: str) -> None: ...
    async def transition(
        self,
        run_id: UUID,
        expected_status: RunStatus,
        new_status: RunStatus,
    ) -> Run:
        ...
class EventRepository(Protocol):
    async def append(self, event: NewEvent) -> EventEnvelope: ...

    async def list_after(
        self,
        session_id: UUID,
        sequence: int,
        principal: Principal,
    ) -> list[EventEnvelope]:
        ...
```
Two methods above are superseded. `RunRepository.claim_next` and `RunRepository.heartbeat` were written before the queue had leases; [event-log-and-persistence.md](event-log-and-persistence.md) replaces them with `RunQueue.claim`, which returns a lease epoch, and `RunQueue.heartbeat`, which returns a boolean so a fenced worker learns it has been fenced. [runtime-loop.md](runtime-loop.md) and ADR-0023 make `RunQueue` the canonical port for both operations and restrict `RunRepository.transition` to a single caller. Implement `RunQueue`; the two signatures above remain here as the record of what they replaced.

Also define ports for:

- Session repository
- Checkpoint repository
- Tool invocation repository
- Approval repository
- Agent repository
- Usage repository
- Run dispatcher
- Event broadcaster
- Telemetry recorder
- Authentication provider

- Device registry and presence, device channel, and notification (multi-device; see Section 29)

## 8. Tool system

The detailed design - definitions for `ToolResult` and `ToolExecutionContext`, the two types named by the Section 7 `Tool` port; the completed `ToolSpec` with its tool kind, source, and required output trust label; the registry name grammar and the reserved-domain partition that lets an MCP tool be a known tool; the fourteen-step execution pipeline with its four persistence points; the `effect_sent_at` watermark that makes crash recovery decidable rather than pessimistic; the derivation of `argument_trust` and `origin_trust`; output excerpting and artifactization; the single outcome shape shared by every non-success result; the unified repeated-call breaker; the batch step boundary for parallel calls; control-tool suspension; and the MCP adapter, its operator-owned classification, and its resource, prompt, sampling, and roots mappings - is specified in [tool-system.md](tool-system.md) and ADR-0021. That document expands Sections 7, 8, 9.2's unknown-tool row, 11.1, 12.4, 12.5, 13, 15, 18.3, 18.4, 19, 22, 26, 27.3, 29.4, and 30.4 and Milestones 1, 4, 6, and 8; it does not replace the requirements below, and it reorders none of the Section 8.3 steps.

The builtin tools that pipeline runs - the roster reconciled where 8.1's seven names and 8.2's six specifications disagree; every `ToolSpec` field value for all eight, including the trust label, idempotency class, timeout, output ceiling, and scopes that registration validation refuses to start without; the complete design of `math.calculate` and `system.current_time`; and the seven ordered refusals that make up the startup registration check - is specified in [builtin-tools.md](builtin-tools.md) and ADR-0026. That document expands Sections 8.1, 8.2, 9.2, 11.2, and 26 and Milestones 1, 4, and 6; it does not replace the requirements below, and it changes no behaviour any tool below is given.

### 8.1 Tool specification

```python
class ToolSpec(BaseModel):
    name: str
    version: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    side_effect: SideEffectClass
    risk: RiskLevel
    idempotency: IdempotencyClass
    required_scopes: set[str]
    timeout_seconds: int
    maximum_output_bytes: int
    allow_parallel: bool
```
Use namespaced names:

```text
system.current_time
math.calculate
workspace.read_text
workspace.write_text
workspace.list_files
sandbox.run_command
artifact.export
```
### 8.2 Initial tools

[builtin-tools.md](builtin-tools.md) and ADR-0026 complete every tool named below and place the two this section leaves unassigned. `math.calculate` gets its grammar, its `Decimal` numeric model, its operator and function sets, the four bounds that make `9**9**9` a failure rather than an outage, and its eight reason codes; `system.current_time` gets its IANA-only timezone argument, its aware-UTC `Clock` contract, and the output fields that make Section 8.2's determinism claim testable. `demo.external_write`, which 8.1 omits, and `artifact.export`, which this section omits, are both in the roster; `artifact.export` is placed at Milestone 6. Nothing stated below changes.

#### `math.calculate`

- Read-only
- Deterministic
- No approval
- Strictly parse supported mathematical expressions
- Do not use unrestricted `eval`

#### `system.current_time`

- Read-only
- Deterministic
- No approval
- Accept an explicit timezone

#### `workspace.read_text`

- Read-only
- Restricted to the current isolated workspace
- Reject absolute paths and path traversal

#### `workspace.write_text`

- Local workspace write
- Restricted to the current isolated workspace
- Return file metadata and checksum

#### `workspace.list_files`

- Read-only
- Restricted to the current workspace
- Enforce result limits

#### `demo.external_write`

Implement a fake external side-effect tool solely to test approvals.

- Always requires approval
- Records what would have been written
- Does not call an actual external service

Add `sandbox.run_command` only after the sandbox milestone.

### 8.3 Validation

Tool execution must proceed in this order:

```text
Resolve registered tool
    -> check tool enabled for agent
    -> check principal scopes
    -> validate JSON arguments
    -> normalize arguments
    -> evaluate policy
    -> request approval if needed
    -> execute with timeout
    -> validate result
    -> truncate or artifactize large output
    -> persist result
```
Unknown arguments must be rejected unless the schema explicitly permits them.

Tool descriptions are not security boundaries.

### 8.4 Idempotency

Every tool invocation must have an application-generated idempotency key.

Derive it from stable inputs such as:

```text
run ID
run step
provider tool-call ID
tool name
normalized arguments hash
```
Before executing a tool, check whether that idempotency key has already succeeded.

For an external service that supports idempotency keys, pass the application key through.

Tool invocation statuses:

```text
PROPOSED
AUTHORIZED
WAITING_FOR_APPROVAL
RUNNING
SUCCEEDED
FAILED
DENIED
UNCERTAIN
```
After a worker crash:

- Retry read-only tools.
- Retry explicitly idempotent tools.
- Retry conditionally idempotent tools only with an idempotency key.
- Do not automatically retry a non-idempotent tool left in `RUNNING`.
- Mark an ambiguous non-idempotent execution `UNCERTAIN` and require review.

### 8.5 Programmatic tool orchestration

Multi-step tool pipelines cost one model round-trip per step. Allow the model to write code that orchestrates tools in a single turn:

- The model writes a script that runs in the sandbox (Section 28) and calls registered tools through an in-sandbox RPC bridge back to the tool executor.
- Every underlying tool call still passes the full pipeline - validation, scopes, policy, approval, timeout, output limits, tracing, idempotency. The bridge is the enforcement point; sandboxed code cannot bypass it or reach credentials directly.
- Refund the step and model-call budget for orchestration-only turns so pipelines do not exhaust limits; cap the total underlying calls.
- If an underlying call requires approval, the run checkpoints at the bridge and pauses/resumes normally (Sections 9, 27).

This collapses multi-step pipelines into near-zero model round-trips - a large token and latency saving - without weakening policy. Recorded as ADR-0015.

## 9. Policy and approval model

The detailed design - definitions for `ProposedAction`, `ApprovalStatus`, `SideEffectClass`, `RiskLevel`, and `IdempotencyClass`; the mechanizable key for the 9.2 matrix and how its three non-enum decision strings resolve; the restrictiveness ordering the layered model needs; the format, producer, and storage of `policy_version`; the hardline rule format and freeze; revalidation after approval; the denial tool-result shape; and the trust-label-to-tier mapping - is specified in [policy-and-approvals.md](policy-and-approvals.md), ADR-0005, and ADR-0006. That document expands Sections 8.3, 8.4, 9, 11.2, 13, and 22 and Milestone 4; it does not replace the requirements below, and it changes no outcome stated in the 9.2 matrix.

### 9.1 Policy decisions

```python
class PolicyDecisionType(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"
    ALLOW_WITH_MODIFICATIONS = "allow_with_modifications"
class PolicyDecision(BaseModel):
    decision: PolicyDecisionType
    reason_code: str
    explanation: str
    modified_arguments: dict[str, Any] | None
    policy_version: str
```
### 9.2 Default policy matrix

| Action category                | Default decision        |
|--------------------------------|-------------------------|
| Pure computation               | Allow                   |
| Read isolated workspace        | Allow                   |
| Write isolated workspace       | Allow                   |
| Read approved network resource | Allow with restrictions |
| Execute code                   | Allow only in sandbox   |
| Install packages               | Deny initially          |
| Enable network from sandbox    | Deny initially          |
| Send a message                 | Require approval        |
| Modify external data           | Require approval        |
| Delete external data           | Require approval        |
| Spend money                    | Require approval        |
| Publish content                | Require approval        |
| Access raw credentials         | Deny                    |
| Access host filesystem         | Deny                    |
| Privileged container operation | Deny                    |
| Unknown tool                   | Deny                    |

### 9.3 Approval object

```python
class ApprovalRequest(BaseModel):
    id: UUID
    run_id: UUID
    tool_invocation_id: UUID
    status: ApprovalStatus
    action_summary: str
    tool_name: str
    arguments: dict[str, Any]
    policy_reason: str
    expires_at: datetime | None
    created_at: datetime
    resolved_at: datetime | None
    resolved_by: str | None
```
Version 0.1 supports:

```text
APPROVE_ONCE
DENY
```
Do not implement session-wide or permanent approval grants initially.

When approval is required:

1.  Persist the proposed tool invocation.
2.  Persist the approval request.
3.  Checkpoint the run.
4.  Transition the run to `WAITING_FOR_APPROVAL`.
5.  Release the worker lease.
6.  Emit an approval event.
7.  Resume only after an authenticated resolution.
8.  Revalidate policy after approval in case policy or arguments changed.

Two lifecycle edges must be handled by the application service, not the worker loop:

- Approval expiry: a run in WAITING_FOR_APPROVAL has released its lease, so no worker is watching it. A periodic reaper must expire approvals past expires_at, mark the tool invocation DENIED (or fail the run per policy), emit an approval.resolved (expired) event, and complete or fail the run deterministically.
- Cancellation of non-running runs: cancellation is cooperative and checked by the worker, but QUEUED and WAITING_FOR_APPROVAL runs have no worker executing them. The run service must transition these directly to CANCELLED (reaping any pending approval), rather than waiting for a worker to observe the request.

Approval is layered, with the deterministic engine always primary:

- Hardline rules: never-bypassable patterns (destructive commands, secret exfiltration) evaluated first and frozen at load, so no configuration or in-process code can disable them.
- Deterministic policy (Section 9.1) is the authoritative decision.
- Optional LLM-assisted approval is a secondary signal that may only make a decision more restrictive, never override a deny or a hardline block; it is injection-hardened (strip comments, XML-delimit the untrusted input) and never load-bearing for safety.
- Human approval remains the gate for consequential actions (Section 9.3). Recorded as ADR-0017.

## 10. Model gateway

The detailed design - the six invariants of the normalized stream and the shared assembler that folds it into a turn; definitions for the streaming event classes, conversation items, content parts, usage, stop reasons, and the three error classes; resolutions for the nine unfilled cells of the 10.2 mapping table including `server_tool_use`, the in-band `<think>` mapping, and the fifth token class; the `ModelRouter` port that turns a model policy into capabilities, limits, and pricing; how provider pinning and availability routing coexist; the retry ownership split; and the model-call timeouts - is specified in [model-gateway.md](model-gateway.md) and ADR-0002. That document expands this section and Milestones 1 and 3; it does not replace the requirements below, and it changes no mapping stated in the 10.2 table.

### 10.1 Normalized request

```python
class ModelRequest(BaseModel):
    model_policy: str
    conversation: list[ConversationItem]
    tools: list[ToolSpec]
    response_schema: dict[str, Any] | None
    temperature: float | None
    maximum_output_tokens: int | None
    metadata: dict[str, str]
```
The request must be able to express prompt-cache intent, because the two first providers cache with opposite ergonomics: Anthropic uses explicit cache breakpoints (cache_control) while OpenAI Responses caches long prefixes automatically.

```python
class ModelRequest(BaseModel):
    ...
    cache_hints: CacheHints | None

class CacheHints(BaseModel):
    # Stable prefixes the builder considers cacheable, in priority order.
    # Anthropic adapter -> cache_control breakpoints (<= 4).
    # OpenAI adapter    -> may ignore (automatic prefix caching).
    breakpoints: list[CacheBreakpoint]

class CacheBreakpoint(BaseModel):
    boundary: str    # "after_system" | "after_tools" | "after_history_prefix"
    min_tokens: int = 1024   # caching only helps above provider minimums
    ttl: str = "default"     # "default" | "1h" for long tool loops
```
The context builder chooses breakpoints - typically after the platform and agent system content and after the tool definitions, which change rarely. The Anthropic adapter translates them into cache_control and prefers the 1-hour TTL for long thinking or tool sessions; the OpenAI adapter may treat them as advisory. Note that changing thinking parameters or tool definitions invalidates downstream cache, so keep both stable within a run.

Prompt-stability invariant: build the cacheable prefix - platform policy, agent instructions, and tool definitions - once per session and keep it byte-stable. Inject volatile context (current date, retrieved memory, tool output) into the user turn, never into the system prefix, so the provider prompt cache is never invalidated mid-session. Place breakpoints after the system content and after the tool definitions, and cache the last few non-system messages as a rolling window. The context builder enforces this (Section 11).

### 10.2 Normalized events

```python
ModelEvent = (
    TextDeltaEvent
    | ToolCallDeltaEvent
    | UsageEvent
    | ModelCompletedEvent
    | ModelFailedEvent
)
```
Amend the normalized event union to represent reasoning and to define usage authority across providers:

```python
ModelEvent = (
    ReasoningDeltaEvent      # transient; raw text never persisted
    | TextDeltaEvent
    | ToolCallDeltaEvent
    | UsageEvent             # may arrive provisionally mid-stream
    | ModelCompletedEvent    # carries authoritative final usage
    | ModelFailedEvent
)
```
Normalization rules across the two providers: (1) Anthropic emits reasoning as content_block deltas of type thinking_delta followed by a signature_delta; OpenAI surfaces reasoning as items and, optionally, reasoning.encrypted_content. Map both to a ReasoningDeltaEvent for display and to a ProviderReasoningItem for continuity. (2) Usage arrives at different times - Anthropic streams input usage at message_start and output at message_delta, while OpenAI reports totals at completion - so treat only the usage on ModelCompletedEvent as authoritative and earlier usage as provisional. (3) Tool-call arguments stream incrementally on both (Anthropic input_json_delta, OpenAI function-call argument deltas); assemble by item index and validate only after the completed event.

#### Anthropic Messages streaming - exact mapping

SSE sequence for a turn that streams thinking, text, and a tool call:

```text
message_start          # usage.input_tokens, usage.output_tokens (initial)
content_block_start    # index i; content_block.type:
                       #   thinking | text | tool_use | server_tool_use
content_block_delta    # delta.type:
                       #   thinking_delta | signature_delta
                       #   | text_delta | input_json_delta
content_block_stop     # block i complete
  ... repeat content_block_* per block ...
message_delta          # delta.stop_reason; usage (cumulative)
message_stop
ping                   # keep-alive: ignore
error                  # terminal error
```
| **Anthropic source (event / field)**                                     | **Normalized**                           | **Notes**                                                           |
|---|---|---|
| content_block_start type=thinking + content_block_delta.thinking_delta   | ReasoningDeltaEvent                      | Display only; raw text never persisted.                             |
| content_block_delta.signature_delta                                      | ProviderReasoningItem                    | Append to the opaque item; returned verbatim next request.          |
| content_block_start type=text + content_block_delta.text_delta           | TextDeltaEvent                           | Assistant visible text.                                             |
| content_block_start type=tool_use + content_block_delta.input_json_delta | ToolCallDeltaEvent                       | Partial JSON; assemble by block index; parse at content_block_stop. |
| message_delta.delta.stop_reason                                          | ModelCompletedEvent.stop_reason          | end_turn \| tool_use \| max_tokens \| stop_sequence.                |
| message_start.usage / message_delta.usage                                | UsageEvent -\> ModelCompletedEvent.usage | message_delta usage is cumulative and authoritative.                |
| error                                                                    | ModelFailedEvent                         | Map to ModelTransientError / ModelPermanentError by type.           |
| ping                                                                     | (ignored)                                | Keep-alive.                                                         |

Usage: message_start carries initial input_tokens and output_tokens; the final message_delta carries cumulative usage including cache_creation_input_tokens (cache write) and cache_read_input_tokens (cache read). Read authoritative totals from the last message_delta.

#### OpenAI Responses streaming - exact mapping

Item-oriented sequence for reasoning, text, and a function call:

```text
response.created  ->  response.in_progress
response.output_item.added        # item.type:
                                  #   reasoning | message | function_call | ...
  # reasoning item:
  response.reasoning_summary_part.added
  response.reasoning_summary_text.delta ... .done
  response.reasoning_summary_part.done
response.output_item.done
response.output_item.added        # message
  response.content_part.added
  response.output_text.delta ... response.output_text.done
  response.content_part.done
response.output_item.done
response.output_item.added        # function_call  (call_id, name)
  response.function_call_arguments.delta ... .done
response.output_item.done
response.completed                # response.usage (authoritative)
response.incomplete | response.failed | error
```
| **OpenAI source (event)**                                      | **Normalized**                        | **Notes**                                                                             |
|---|---|---|
| output_item.added type=reasoning; reasoning_summary_text.delta | ReasoningDeltaEvent                   | Summary text only; store reasoning item / encrypted_content as ProviderReasoningItem. |
| response.output_text.delta (/.done)                            | TextDeltaEvent                        | Assistant visible text.                                                               |
| response.function_call_arguments.delta (/.done)                | ToolCallDeltaEvent                    | Partial JSON; assemble per output item; final on .done.                               |
| response.output_item.added type=function_call                  | (tool-call start)                     | Carries call_id and name; preserve call_id for the tool result.                       |
| response.completed -\> response.usage                          | ModelCompletedEvent.usage             | Authoritative totals.                                                                 |
| response.incomplete                                            | ModelCompletedEvent (stop=incomplete) | e.g. max_output_tokens; see incomplete_details.reason.                                |
| response.failed / error                                        | ModelFailedEvent                      | Map to internal error taxonomy.                                                       |

Usage: response.completed.response.usage = { input_tokens, input_tokens_details.cached_tokens, output_tokens, output_tokens_details.reasoning_tokens, total_tokens }.

#### Usage field mapping (both providers -\> RunUsage)

| **RunUsage field**      | **Anthropic**                    | **OpenAI**                                        |
|---|---|---|
| input_tokens (uncached) | usage.input_tokens               | input_tokens - input_tokens_details.cached_tokens |
| cached_input_tokens     | usage.cache_read_input_tokens    | input_tokens_details.cached_tokens                |
| output_tokens           | usage.output_tokens (cumulative) | output_tokens                                     |
| reasoning_tokens        | included in output_tokens        | output_tokens_details.reasoning_tokens            |

Anthropic bills thinking within output_tokens and separates cache read from cache write; cache_creation_input_tokens (write) is priced above uncached input, so track it as a fifth class if cache writes are used. OpenAI reports cached_tokens as a discount class within input and reasoning_tokens as a subset of output. Map both onto RunUsage’s classes (uncached input, cached input, output, reasoning) plus, for Anthropic, an optional cache-write counter. Exact event and field names should be re-verified against current provider docs at implementation time.

A completed turn should contain:

```python
class ModelTurn(BaseModel):
    assistant_messages: list[AssistantMessage]
    tool_calls: list[ToolCallItem]
    usage: ModelUsage
    stop_reason: str
    provider_metadata: dict[str, Any]
```
Provider metadata may include response IDs and cache information, but application logic must not rely on provider-specific fields.

### 10.3 Fake provider

The fake provider is mandatory.

It should support scripted responses such as:

```python
FakeModelScript(
    turns=[
        ToolCallTurn(
            tool_name="math.calculate",
            arguments={"expression": "12 * 9"},
        ),
        FinalTurn(text="The result is 108."),
    ]
)
```
It must be able to simulate:

- Direct final answers
- Valid tool calls
- Invalid tool calls
- Multiple tool calls
- Streaming text
- Transient errors
- Permanent errors
- Timeouts
- Malformed provider output

All runtime tests should use the fake provider unless explicitly marked as live.

### 10.4 OpenAI adapter

The OpenAI adapter must:

- Map normalized messages to Responses API input items
- Map `ToolSpec` to provider function definitions
- Parse text and tool-call outputs
- Preserve provider tool-call IDs
- Stream normalized deltas
- Capture usage
- Capture the provider response ID in metadata
- Convert provider exceptions into the internal error taxonomy
- Respect cancellation and deadlines
- Apply provider retries only to transient failures
- Never execute tools itself
- Never store API keys in events or logs

Add recorded adapter fixtures with secrets and sensitive content removed.

#### Anthropic adapter

The Anthropic Messages adapter must:

- Map normalized messages to Messages API content blocks (text, tool_use, tool_result).
- Emit and round-trip thinking and redacted_thinking blocks verbatim, including the opaque signature.
- Never drop, reorder, or modify thinking blocks in the last assistant message during tool use; the API rejects a modified sequence with a 400 error.
- Filter reasoning blocks on type in (thinking, redacted_thinking) so redacted blocks are never silently dropped.
- Translate CacheHints into cache_control breakpoints.
- Stream content_block deltas into normalized events.
- Capture usage: input, cache-read input, cache-write input, and output tokens.
- Convert provider exceptions into the internal error taxonomy.
- Respect cancellation and deadlines.
- Never execute tools itself, and never store API keys in events or logs.

### 10.5 Model routing

For version 0.1, implement a simple configuration-backed router:

```yaml
model_policies:
  balanced:
    provider: openai
    model: ${OPENAI_MODEL}
```
Do not implement dynamic LLM-based routing yet.

Provider-pinning rule (required once more than one provider exists): a routing decision is made once per run, at run start, and applies to every model call in that run. Do not switch providers within a run or an in-flight tool loop, because provider-opaque reasoning and continuation state is not portable across providers. Routing a new run in an existing session to a different provider than the previous run is allowed, but provider-opaque items from the prior run are not replayed to the new provider; only the portable conversation - messages, tool calls, results, and compacted summaries - carries over. See ADR-0007 and Section 27.

Model providers are declarative plugins, not hardcoded adapters. A provider profile - a plugin the registry loads and the user can override without editing core - declares:

- API mode: chat_completions (OpenAI-compatible), responses (OpenAI Responses), anthropic (Messages); add bedrock/converse and others later behind the same ModelProvider port.
- Model aliases, capabilities, context limits, and a price table (feeding the cost-source precedence in Section 6.5).
- Credential pools with round-robin/least-used selection, failover, and cooldowns; OAuth where the provider supports it.
- A model-catalog import so adding a model is configuration, not code.

This keeps the two hand-written adapters (Responses, Anthropic) as reference implementations while making every other provider - including self-hosted and OpenAI-compatible endpoints - a profile. See Section 10.7 and ADR-0012.

### 10.6 Reasoning and provider continuity

Both first providers are reasoning models, and reasoning is where provider-neutrality is hardest. Consolidated requirements:

- Represent reasoning as transient display events (ReasoningDeltaEvent) and as provider-opaque continuity items (ProviderReasoningItem / ProviderContinuation).
- Never persist raw reasoning text to the event log, structured logs, or long-term memory.
- During an active tool loop, return provider reasoning to the provider verbatim: Anthropic requires the unmodified thinking and redacted_thinking blocks with their signatures in the last assistant message or it rejects the request; OpenAI benefits measurably from included reasoning items and supports reasoning.encrypted_content for stateless or ZDR operation with store=False.
- Account reasoning tokens as billed output in usage and cost.
- Pin a run to one provider; do not port reasoning state across providers.
- Keep thinking parameters and tool definitions stable within a run to preserve prompt-cache hits.

This amends ADR-0006: reasoning is never durably stored, but opaque continuation may live in the run checkpoint for the life of a tool loop. Recorded as ADR-0007.

Reasoning is represented differently by each provider class; the adapter layer resolves this with an explicit per-provider handling matrix:

| **Model / provider class**  | **Reasoning representation**                 | **Adapter handling**                                                                                                                |
|---|---|---|
| Anthropic (Claude)          | Signed thinking / redacted_thinking blocks   | Preserve verbatim incl. signature in the last assistant turn during tool use; strip from the user surface.                          |
| OpenAI Responses (o-series) | reasoning items; reasoning.encrypted_content | Replay items; use encrypted_content with store=False for stateless / ZDR.                                                           |
| Google Gemini               | thought_signature                            | Store and replay the signature; never surface it.                                                                                   |
| Open models (in-band)       | `<think>` ... `</think>` text tokens         | Scrub from the user surface with a boundary-gated streaming scrubber; keep for trajectory; echo back only if the endpoint benefits. |
| Strict endpoints            | reasoning echoed back is rejected (400)      | Strip reasoning before the next request (e.g. some OpenAI-compatible hosts).                                                        |

The streaming think-scrubber must be boundary-gated so prose legitimately mentioning "`<think>`" is not swallowed, and must handle tag variants. In-band reasoning is the third representation the normalized event model carries (Section 10.2), alongside signed blocks and encrypted items. See ADR-0012.

### 10.7 Open-weights and self-hosted models

A general-purpose agent must run open and self-hosted models, not only hosted frontier APIs. The chat_completions adapter (Section 2.3) is the vehicle:

- Endpoints: vLLM, Ollama, LM Studio, llama.cpp, LiteLLM, OpenRouter, and any BYO OpenAI-compatible server, including a self-hosted Hermes, Llama, or Qwen.
- Tool calling: use provider-native function calling when the endpoint supports it; otherwise parse an XML `<tool_call>` block from the text and assemble arguments (Section 10.2), converting to the normalized tool-call representation.
- Reasoning: in-band `<think>` text, handled by the matrix and scrubber in Section 10.6.
- Why: cost control, data residency, offline / air-gapped operation, fine-tuning on captured trajectories (Section 31), and no vendor lock-in.

Nothing here changes the domain model: open models sit behind the same ModelProvider port, cache hints, usage accounting, policy, and provider pinning as the hosted adapters. Recorded as ADR-0012.

## 11. Context engine

The detailed design - the two-region cache boundary and its enforcement test, prefix epochs, the budget allocator and its yield order under pressure, compaction reconciled with the prompt-stability invariant, the trust-envelope rendering contract, and the working-state lifecycle - is specified in [context-engine.md](context-engine.md) and ADR-0020. That document expands this section and Milestone 7; it does not replace the requirements below.

### 11.1 Initial context builder

The first context builder should assemble:

1.  Platform policy
2.  Agent instructions
3.  Current goal and constraints
4.  Relevant normalized conversation items
5.  Available tool definitions
6.  The current user message
7.  Runtime metadata such as date and principal scope

The builder enforces the prompt-stability invariant (Section 10.1): platform policy, agent instructions, and tool definitions form a byte-stable prefix built once per session; volatile items - the current date, retrieved memory, and tool results - are placed in the user turn so the cached prefix never changes mid-session.

Do not load every registered tool. Filter tools by:

- Agent configuration
- Principal authorization
- Policy profile
- Runtime environment

[bootstrap-and-composition.md](bootstrap-and-composition.md) and ADR-0024 identify which builder this is: build-sequence step 1 of [context-engine.md](context-engine.md), deterministic assembly with the two regions and `prefix_sha256` recorded from the first commit. That document assigns each of the seven inputs above to Region A or Region B, states the prompt-stability test as two assertions rather than one - the prefix hash is stable *and* the request bytes differ, in Region B only - and requires all four tool filters to exist from the first commit, two of them as identity stages until the milestones that give them data. The seven inputs and the four filters are unchanged.

### 11.2 Trust labels

Every context item must have a trust classification.

```python
class TrustLevel(str, Enum):
    PLATFORM = "platform"
    TRUSTED_CONFIGURATION = "trusted_configuration"
    USER = "user"
    INTERNAL_TOOL = "internal_tool"
    EXTERNAL_UNTRUSTED = "external_untrusted"
    MEMORY = "memory"
    KNOWLEDGE = "knowledge"
```
External content must be clearly represented as data, not instructions.

A tool result must never be able to redefine platform policy, grant itself permissions, or change approval requirements.

### 11.3 Context budget

Implement a budget allocator rather than simple string concatenation.

```python
class ContextBudget(BaseModel):
    total_tokens: int
    platform_tokens: int
    agent_tokens: int
    history_tokens: int
    tool_tokens: int
    retrieved_context_tokens: int
    reserve_output_tokens: int
```
The initial token estimator may be approximate, but it must be behind a replaceable interface.

### 11.4 Compaction

Do not implement sophisticated compaction until the basic loop works.

The first compactor should:

- Retain the current user goal
- Retain explicit constraints
- Retain unresolved questions
- Retain tool results needed for subsequent steps
- Replace older dialogue with a concise structured summary
- Keep source event IDs for provenance
- Preserve the original event log outside the model context

Do not request or store private chain-of-thought. Persist only messages, actions, evidence, concise decision summaries, and structured working state.

Amendment (v2.0): "do not store" refers to durable logs, the event payload, and long-term memory. It does not forbid holding a provider’s opaque reasoning-continuation payload inside the active run checkpoint for the duration of a tool loop, which some providers require (Anthropic thinking blocks with signatures; OpenAI reasoning items or reasoning.encrypted_content). That payload is provider-tagged, replayed verbatim, excluded from logs and memory, and discarded when the loop ends or the provider changes. See Sections 10.6 and 27 and ADR-0007.

## 12. Runtime behavior

The detailed design - the eleven callables 12.1 names and the ports or named functions each becomes; `Step` as a value object with a persisted identity on `model_calls.step_number`; the split between a `run_loop` that returns a typed `RunOutcome` and a `finalize` that performs every terminal transition, lease release, and terminal event exactly once; the six cancellation observation points and the rule that a cancellation observed after an effect watermark does not abandon the call; the three budget scopes and why "after" means "record in the same transaction"; the heartbeat as a supervisor task that also watches the deadline and polls for cancellation; the `build_with_pressure` call site that finally gives compaction one; the six checkpoint triggers and the `full` rule; the resume ladder for all four resumption paths; and the twenty cross-document contradictions the loop is where they meet - is specified in [runtime-loop.md](runtime-loop.md) and ADR-0023. That document expands Sections 6.4, 6.5, 6.9, this section, 13, 14.1, 14.2, 15, 16, 19, 26, and 27 and Milestones 1, 2, 4, 5, and 7; it does not replace the requirements below, and it reorders none of the Section 8.3 pipeline steps.

### 12.1 Main loop

Implement the runtime approximately as follows:

```python
async def execute_run(run_id: UUID) -> None:
    run = await load_and_verify_claim(run_id)
    checkpoint = await checkpoints.load_latest(run_id)
    agent = await agents.get_version(run.agent_id, run.agent_version)
    principal = await principals.for_run(run)

    while run.status == RunStatus.RUNNING:
        await cancellation.raise_if_requested(run.id)
        budgets.check_before_step(run)

        request = await context_builder.build(
            run=run,
            checkpoint=checkpoint,
            agent=agent,
            principal=principal,
        )

        model_turn = await invoke_model_and_persist_events(
            run=run,
            request=request,
        )

        budgets.record_model_usage(run, model_turn.usage)

        if model_turn.tool_calls:
            disposition = await process_tool_calls(
                run=run,
                checkpoint=checkpoint,
                tool_calls=model_turn.tool_calls,
                principal=principal,
            )

            if disposition.paused:
                return

            checkpoint = await checkpoints.save(...)
            continue

        final_message = select_final_message(model_turn)
        await complete_run(run, final_message)
        return
```
### 12.2 Transaction boundaries

Never hold a database transaction open while awaiting:

- A model provider
- A tool
- A sandbox
- An external API
- Human approval

Use short transactions:

1.  Persist intent.
2.  Commit.
3.  Perform external I/O.
4.  Persist result.
5.  Commit.

### 12.3 Model calls

Before a model call:

- Check cancellation.
- Check budget.
- Persist `model.request.started`.
- Include a unique attempt ID.

After a model call:

- Persist normalized final output.
- Persist usage.
- Update counters.
- Create a checkpoint.

A repeated model call after a crash may incur duplicate provider cost, but it must not produce duplicate external side effects.

### 12.4 Multiple tool calls

Default to sequential execution.

Allow parallel tool execution only when all calls:

- Are read-only
- Explicitly permit parallel execution
- Have no dependency on one another
- Use separate database sessions
- Fit within the remaining tool and time budget

Do not infer that two external writes are independent.

### 12.5 Termination

Complete the run when:

- The model returns a final assistant message and no tool calls
- A deterministic workflow explicitly marks completion
- A terminal failure occurs
- A budget is exhausted
- The deadline passes
- Cancellation is requested

Prevent endless loops with:

- Maximum steps
- Maximum model calls
- Maximum tool calls
- Repeated-identical-call detection
- Deadline enforcement

If the same normalized tool call repeats several times without new evidence, fail with a structured loop-detection error.

## 13. Error taxonomy and retries

Create explicit exceptions or error values:

```text
AuthenticationError
AuthorizationError
NotFoundError
ConflictError
InvalidStateTransition
ModelTransientError
ModelPermanentError
ModelProtocolError
ToolNotFoundError
ToolValidationError
ToolPolicyDenied
ToolTimeoutError
ToolExecutionError
ToolResultValidationError
ApprovalRequired
ApprovalDenied
BudgetExceeded
DeadlineExceeded
RunCancelled
SandboxProvisionError
SandboxExecutionError
ArtifactStorageError
ConcurrencyConflict
```
Retry policy:

| Failure                           | Retry                            |
|-----------------------------------|----------------------------------|
| Model rate limit                  | Yes, bounded                     |
| Model temporary server failure    | Yes, bounded                     |
| Model invalid request             | No                               |
| Tool validation failure           | No                               |
| Policy denial                     | No                               |
| Read-only tool timeout            | Possibly                         |
| Idempotent tool temporary failure | Yes, bounded                     |
| Non-idempotent external write     | Only with guaranteed idempotency |
| Database serialization conflict   | Yes, bounded                     |
| Authentication failure            | No                               |

Every retry must fit inside the run deadline.

Use exponential backoff with jitter. Keep retry decisions in application code, not in provider adapters alone.

[runtime-loop.md](runtime-loop.md) and ADR-0023 add the loop-facing half of this taxonomy: `FailureReason`, the fourteen-value enum a terminal `FAILED` run records so an operator can distinguish a provider outage from an exhausted budget from a policy denial; `RunFailure` on the run row; the three-way split of retry ownership between the adapter's transport retries, the gateway's attempt loop, and the runtime's step retry, with `stream_had_output` deciding which owns a given failure; the rule that a failed attempt is still charged to budget at the attempt check rather than after it; and the treatment of an empty terminal model turn as a failed step rather than as a completed run with an empty answer. The retry table above is unchanged; every row keeps its retryability and its owner is now named.

## 14. Durable worker and PostgreSQL queue

The detailed design - the append transaction and why sequence gaps are legal while missing writes are not, projections with watermarks and a rebuild gate, upcasters for payload evolution, delta checkpoints, the claim query with priority classes and reserved capacity, and the `lease_epoch` fencing that makes it safe for lease expiry to guess wrong - is specified in [event-log-and-persistence.md](event-log-and-persistence.md), ADR-0003, and ADR-0004. That document expands Sections 6.8, 6.9, 12.2, 14, and 15 and Milestone 2; it does not replace the requirements below.

### 14.1 Worker model

Run the API and worker as separate processes, even during local development.

The worker should:

1.  Claim a queued run.
2.  Set a lease owner and lease expiration.
3.  Refresh the lease periodically.
4.  Execute until completion or pause.
5.  Release the lease.
6.  Reclaim abandoned runs after their lease expires.

Use a PostgreSQL claim query based on `FOR UPDATE SKIP LOCKED`.

Do not add Celery or Redis for version 0.1.

### 14.2 Run recovery

On reclaim:

1.  Load the latest checkpoint.
2.  Inspect the most recent tool invocation.
3.  Determine whether an operation is safe to retry.
4.  Resume at the first incomplete safe boundary.
5.  Mark ambiguous non-idempotent operations `UNCERTAIN`.

Add a resilience integration test that terminates a worker process after a checkpoint and verifies successful recovery.

## 15. Database schema

Create Alembic migrations for at least these tables.

[event-log-and-persistence.md](event-log-and-persistence.md) adds columns and tables required by Section 6.8, Section 16, Section 27.5, and the Milestone 2 acceptance criteria that are not yet reflected below: `events.payload_schema_version`, the `runs` columns `priority`, `attempts`, `scheduled_for`, and `lease_epoch` with a partial unique index enforcing one non-terminal run per session, and the `idempotency_keys`, `projection_watermarks`, and `derived_event_keys` tables. All are additive to the tables specified here.

That same document and ADR-0031 supply what the sentence above leaves open, which is what a migration looks like when someone writes one: a linear revision graph with exactly one head and no merge revisions, resolved by rebasing `down_revision` rather than by `alembic merge`; `<revision>_<slug>.py` file names that carry no order, because ordering lives in `down_revision` and nowhere else; structural and data changes in separate revisions, with `downgrade()` written for the former and refusing for the latter; `alembic revision --autogenerate` as a draft a person then edits, kept honest by an empty-diff round trip rather than by review; lock-taking DDL split into its own non-transactional revision; and a revision that may add a column or an index to `events` but may never rewrite a payload. Schema in tests is created by running the migrations, never by `metadata.create_all`. It adds no table to the list below.

[model-gateway.md](model-gateway.md) adds the two tables Section 6.5's cost precedence and the Milestone 3 usage criteria require and this section does not yet carry: `model_calls`, one row per model attempt with all five token classes, cost, `cost_source`, and the resolved provider, model, and registry version; and `model_prices`, an append-only price history so a recorded cost stays reproducible after a vendor price change. `runs.usage` is unchanged in shape and becomes a rollup of `model_calls` maintained in the same transaction. Both are additive to the tables specified here.

[policy-and-approvals.md](policy-and-approvals.md) likewise adds what Section 9, Section 11.2, and the Milestone 4 acceptance criteria require and this section does not yet carry: `tenant_id`, `principal_id`, `session_id`, `action_kind`, `action_id`, `risk`, `policy_version`, and `revalidated_policy_version` on `approvals`, with `tool_invocation_id` widened to nullable so non-tool actions can be approved, and the indexes the approval list, the resume path, and the expiry reaper each need; the classification columns `side_effect`, `risk`, `idempotency_class`, `origin_trust`, and `effective_arguments_hash` on `tool_invocations`; and a `policy_profiles` audit table that records which ruleset a `policy_version` refers to. Policy rules themselves are version-controlled files, not rows.

### `agents`

```text
id
version
name
instructions
model_policy
enabled_tools JSONB
policy_profile
limits JSONB
metadata JSONB
created_at
```
Primary key:

```text
(id, version)
```
### `sessions`

```text
id UUID
tenant_id
principal_id
agent_id
agent_version
status
title
metadata JSONB
next_event_sequence BIGINT
created_at
updated_at
```
Indexes:

```text
(tenant_id, principal_id, updated_at)
(agent_id, created_at)
```
### `runs`

```text
id UUID
session_id UUID
parent_run_id UUID nullable
status
step_count
model_call_count
tool_call_count
limits JSONB
usage JSONB
lease_owner nullable
lease_expires_at nullable
cancel_requested_at nullable
failure JSONB nullable
created_at
updated_at
```
Indexes:

```text
(status, created_at)
(lease_expires_at)
(session_id, created_at)
```
### `events`

```text
id BIGSERIAL
session_id UUID
run_id UUID nullable
sequence BIGINT
event_type
actor_type
actor_id nullable
payload JSONB
trace_id nullable
created_at
```
Constraints:

```text
UNIQUE(session_id, sequence)
```
Indexes:

```text
(run_id, id)
(session_id, sequence)
(event_type, created_at)
```
### `checkpoints`

```text
id BIGSERIAL
run_id UUID
version INTEGER
state JSONB
last_event_sequence BIGINT
created_at
```
Constraint:

```text
UNIQUE(run_id, version)
```
### `tool_invocations`

```text
id UUID
run_id UUID
step_number INTEGER
provider_call_id
tool_name
tool_version
arguments JSONB
normalized_arguments_hash
idempotency_key
status
policy_decision JSONB nullable
result JSONB nullable
error JSONB nullable
started_at nullable
completed_at nullable
created_at
```
Constraints:

```text
UNIQUE(idempotency_key)
```
### `approvals`

```text
id UUID
run_id UUID
tool_invocation_id UUID
status
request JSONB
resolution JSONB nullable
expires_at nullable
created_at
resolved_at nullable
resolved_by nullable
```
### `artifacts`

```text
id UUID
tenant_id
principal_id
session_id UUID
run_id UUID
name
media_type
storage_uri
sha256
size_bytes
metadata JSONB
created_at
```
Do not store artifact bytes in PostgreSQL.

## 16. API contract

The detailed design - the thirteen routes and a response body for each of them, twelve of which the corpus had never written down; the wire error vocabulary as the existing error taxonomy snake-cased under one rule, with four API-specific codes and the four classes that deliberately never cross the boundary; the four request-identifier rules, of which the load-bearing one is that an identifier a client supplies is never trusted with anything; what a successful authentication produces, which is the Section 5 `Principal` and nothing else; the closed dotted scope vocabulary, matched exactly with no hierarchy, and the scope each route requires; tenancy as a repository argument rather than a filter applied afterwards, and a resource in another tenant as 404 rather than 403; `SessionStatus`, referenced in Section 5 and declared nowhere; the ten-step handler order for message submission and the deterministic rule that routes text to a `WAITING_FOR_USER` run instead of rejecting it; the two unrelated mechanisms the phrase "idempotency key" names; the consumer side of the event stream, including the rule that transient frames carry no `id` and the subscribe-before-read handoff that makes replay gapless and duplicate-free; the cancel endpoint's two status codes and the column it writes; and the four application service signatures Section 17 makes the CLI a second caller of - is specified in [http-api-and-streaming.md](http-api-and-streaming.md) and ADR-0028. That document expands this section and Sections 5, 13, 17, 19, 22, and 27 and Milestone 5; it does not replace the requirements below. The nine endpoints stay with their methods and paths, and the `202` on submission, the SSE frame format, the `Last-Event-ID` replay rule, the readiness constraint that a probe must not call a provider, and the rule that tracebacks are never exposed are unchanged.

### Authentication

Version 0.1 may support:

- `AUTH_MODE=dev` for localhost-only development
- Static bearer token authentication for non-development use

In non-development mode, startup must fail if authentication is not configured.

### Endpoints

#### Create session

```http
POST /v1/sessions
```
Request:

```json
{
  "agent_id": "general",
  "metadata": {}
}
```
Response:

```json
{
  "id": "uuid",
  "status": "active",
  "agent_id": "general",
  "agent_version": 1
}
```
#### Submit message

```http
POST /v1/sessions/{session_id}/messages
Idempotency-Key: <client-generated-key>
```
Request:

```json
{
  "content": [
    {
      "type": "text",
      "text": "Calculate 12 times 9."
    }
  ]
}
```
Response:

```text
202 Accepted
{
  "run_id": "uuid",
  "status": "queued"
}
```
A repeated request with the same idempotency key must return the original run.

#### Get run

```http
GET /v1/runs/{run_id}
```
#### Stream run events

```http
GET /v1/runs/{run_id}/events
Accept: text/event-stream
Last-Event-ID: 41
```
SSE format:

```text
id: 42
event: tool.call.completed
data: {"run_id":"...","tool_name":"math.calculate","status":"succeeded"}
```
On reconnect, replay persisted events after `Last-Event-ID`, then continue streaming new events.

#### Cancel run

```http
POST /v1/runs/{run_id}/cancel
```
Cancellation is cooperative. The worker must check cancellation:

- Before a model call
- After a model call
- Before each tool call
- After each tool call
- During long-running sandbox execution where possible

#### Resolve approval

```http
POST /v1/approvals/{approval_id}/resolve
```
Request:

```json
{
  "decision": "approve_once"
}
```
or:

```json
{
  "decision": "deny",
  "reason": "Do not perform this action."
}
```
#### Artifact metadata

```http
GET /v1/artifacts/{artifact_id}
```
#### Artifact content

```http
GET /v1/artifacts/{artifact_id}/content
```
Check authorization before returning either metadata or content.

#### Health

```http
GET /health/live
GET /health/ready
```
Readiness should verify the database and critical configuration, but it should not call a model provider on every probe.

### Error envelope

```json
{
  "error": {
    "code": "tool_validation_error",
    "message": "Tool arguments did not match the schema.",
    "details": {},
    "request_id": "uuid"
  }
}
```
Do not expose tracebacks through the API.

## 17. CLI contract

The detailed design - each command's arguments and options, what it writes to stdout versus stderr, the reserved words after `agent run` and the `--` escape that keeps a prompt beginning with one of them runnable, the six exit codes, the milestone at which each command first works, and the application service each of the seven `agent chat` steps below calls - is specified in [bootstrap-and-composition.md](bootstrap-and-composition.md) and ADR-0024. Four options are added where this section names none, each because a command is otherwise unusable rather than merely less convenient. That document adds no command, removes none, and restates the last rule of this section as a structural check rather than a convention.

Provide these commands:

```text
agent session create
agent chat
agent run "Calculate 12 times 9"
agent run get <run-id>
agent run events <run-id>
agent run cancel <run-id>
agent approval list
agent approval approve <approval-id>
agent approval deny <approval-id>
agent eval run
agent worker
agent api
```
`agent chat` should:

1.  Create or resume a session.
2.  Submit a user message.
3.  Stream events.
4.  Display tool activity concisely.
5.  Prompt for approvals.
6.  Render the final assistant message.
7.  Display artifact paths or IDs.

The CLI must call the same application services as the API. Do not implement a second runtime loop inside the CLI.

## 18. Sandbox and artifacts

The detailed design - the eight types Section 7's `ExecutionEnvironment` and `ArtifactStore` ports name and no document defines, namely `EnvironmentSpec`, `ResourceLimits`, `EnvironmentHandle`, `ExecutionCommand`, `ExecutionResult`, `ArtifactMetadata`, and `ArtifactRef`, together with `WorkspaceHandle`, `ArtifactWriter`, and `CredentialResolver` from `ToolExecutionContext`; the eight `KillReason` values that let a caller tell a program that failed from a limit that stopped it; the argument for why the port stays at three methods and why `read_file` beside `execute` would hand arbitrary execution to every tool that only needed to read; the workspace as a cache rather than state, held for a worker's lease rather than a run's logical lifetime, which makes crash-resume a restart rather than a recovery; every restriction in 18.2 as a default and an operator ceiling rather than a word; the four-way minimum that produces an effective timeout, in which the model-supplied `timeout_seconds` is the weakest input and never the only one; the three-tier environment a sandbox sees and the tier that is never present and not configurable; the full `ToolSpec` for `sandbox.run_command`; and the artifact store's derived storage key, its origin vocabulary, and its retention rule - is specified in [sandbox-isolation.md](sandbox-isolation.md) and ADR-0029. That document expands this section and Sections 7, 8.2, 11.2, 20.4, 22, and 28 and Milestones 1, 4, and 6; it does not replace the requirements below. The argument vector, the refusal of a shell string by default, the request and result field names in 18.3, and 18.4's four storage rules are unchanged, and output truncation and artifactization remain [tool-system.md](tool-system.md)'s.

### 18.1 Execution boundary

Model-generated code must never run inside:

- The API process
- The worker process
- The host environment
- A container containing application secrets

Implement a container-backed `ExecutionEnvironment`.

### 18.2 Default restrictions

The initial sandbox must have:

- No network
- A read-write temporary workspace
- No host filesystem mounts except the isolated workspace
- A non-root user
- CPU limits
- Memory limits
- Process limits
- Disk limits
- Execution timeout
- A fixed base image
- No Docker socket
- No cloud credentials
- No application database credentials

### 18.3 Command tool

`sandbox.run_command` request:

```json
{
  "command": ["python", "script.py"],
  "working_directory": ".",
  "timeout_seconds": 30
}
```
Do not accept a shell string by default. Accept an argument vector.

Result:

```json
{
  "exit_code": 0,
  "stdout": "...",
  "stderr": "",
  "timed_out": false,
  "files_changed": [
    {
      "path": "output.csv",
      "size_bytes": 1204,
      "sha256": "..."
    }
  ]
}
```
Large standard output must be truncated and stored as an artifact when useful.

### 18.4 Artifact storage

The local adapter should:

- Store files outside the source tree
- Use opaque artifact IDs
- Compute SHA-256
- Store metadata in PostgreSQL
- Prevent path traversal
- Stream content rather than loading entire files into memory
- Avoid exposing raw storage paths through the API

Large tool results should be converted into artifacts, with only a summary and artifact reference returned to the model.

## 19. Observability

The detailed design of the logging half - the library, where the bootstrap runs, the two renderers and the deployment mode that selects between them, the four points at which the eight fields below are bound as context variables, and the redaction processor that enforces the do-not-record list rather than leaving it to convention - is specified in [development-toolchain.md](development-toolchain.md) and ADR-0025. That document expands this section and Milestone 0; it does not replace the requirements below, and it removes no field from the list.

Create an OpenTelemetry span hierarchy:

```text
agent.run
|-- context.build
|-- model.invoke
|   `-- model.stream
|-- policy.evaluate
|-- tool.execute
|   `-- sandbox.execute
|-- checkpoint.save
`-- artifact.store
```
Record attributes such as:

```text
run.id
session.id
tenant.id
agent.id
agent.version
model.provider
model.name
model.stop_reason
tool.name
tool.version
tool.status
approval.required
run.step_count
usage.input_tokens
usage.output_tokens
error.type
```
Do not record by default:

- API keys
- Authentication tokens
- Raw credentials
- Full artifact content
- Entire prompts
- Entire tool results
- Private reasoning
- Sensitive personal data

Use structured logs with:

```text
timestamp
level
message
request_id
trace_id
session_id
run_id
tool_invocation_id
```
Initial metrics:

```text
agent_runs_total
agent_runs_completed_total
agent_runs_failed_total
agent_run_duration_seconds
model_calls_total
model_call_duration_seconds
model_tokens_total
tool_calls_total
tool_failures_total
tool_duration_seconds
approval_requests_total
run_recoveries_total
budget_exceeded_total
```
## 20. Evaluation framework

Build evaluations before advanced features.

The detailed design - what a hard gate is, and the registry that makes the one hundred and seventy-two gates fifteen specs and this plan declare reconcilable against a test run; the four gate kinds, and why seventy-seven of those gates cannot be expressed as eval cases at all; the seven sources of nondeterminism and their treatments; how `model_fixture` resolves to a file and what validates it; the `interventions` field, without which cases 12 through 18 and 22 are unwritable; the `effect_sent_at` watermark that makes "no unauthorized side effects" decidable; the tenant, principals, and policy profiles an evaluation runs as, and why there is no test mode; contract suites bound to ports rather than implementations; `resilience` as the sixth test category; the milestone at which each of the twenty-five cases becomes writable; judge governance and distribution-based regression rules for the capability track; and the lossy trajectory-to-case conversion Section 31.3 asserts - is specified in [evaluation-harness.md](evaluation-harness.md), ADR-0022, and ADR-0001. That document expands Sections 3, 4, 10.3, 19, this section, 21, 22, and 31 and Milestones 0 through 6; it does not replace the requirements below. The twenty-five cases stay twenty-five - a twenty-sixth is added later by [sandbox-isolation.md](sandbox-isolation.md), for the container-escape test Section 28 demands and Section 20.3 never enumerated, a twenty-seventh by [skills.md](skills.md), for the Section 30.5 evidence gate that had no case behind it, and a twenty-eighth through thirty-first by [evaluation-harness.md](evaluation-harness.md) itself on a later pass, for the long-session, MCP, and memory-recall gates the milestone map's census showed carrying no case, and none of Section 20's own cases change - the sixteen assertion types stay and gain five, the capability track stays non-blocking, and the deterministic suite still runs in CI without an API key.

### 20.1 Evaluation case format

```yaml
name: calculator_tool
agent_id: general
input:
  text: "What is 17 multiplied by 23?"
model_fixture: calculator_then_answer
expected:
  final_text_contains:
    - "391"
  tool_calls:
    - name: math.calculate
      count: 1
  forbidden_tool_calls: []
  terminal_status: completed
  maximum_steps: 3
```
### 20.2 Assertion types

Support deterministic assertions for:

- Terminal run status
- Final text contains or matches
- Exact tool names
- Tool call count
- Tool argument subset
- Forbidden tools
- Approval requested
- Approval not requested
- Artifact existence
- Artifact checksum or content
- Maximum steps
- Maximum model calls
- Maximum tool calls
- Expected error code
- Event ordering
- No unauthorized side effects

### 20.3 Initial evaluation cases

Create at least these cases:

1.  Direct response without tools
2.  One calculator tool call
3.  Two sequential read-only tools
4.  Model returns invalid tool arguments
5.  Unknown tool name
6.  Tool raises a recoverable error
7.  Tool raises a permanent error
8.  Model provider transient failure and retry
9.  Model provider permanent failure
10. Step limit exceeded
11. Repeated identical tool-call loop
12. Approval requested
13. Approval granted and run resumed
14. Approval denied and model informed
15. Run cancellation
16. Worker restart after model checkpoint
17. Worker restart after idempotent tool success
18. Ambiguous non-idempotent tool execution
19. Workspace path traversal attempt
20. Untrusted tool output containing fake instructions
21. Artifact creation
22. SSE replay after disconnect
23. Duplicate message submission with the same idempotency key
24. Parallel read-only tools
25. External write tools are not parallelized

#### Capability evaluations (non-deterministic)

The deterministic suite above uses fake fixtures and belongs in CI; it verifies plumbing - the state machine, idempotency, policy, and ordering - but it cannot tell you the agent got worse at real tasks when a prompt or model changes. Maintain a separate capability-evaluation track:

- Runs against live models, outside the blocking CI gate (nightly or pre-release).
- Uses graded, rubric, or LLM-judge scoring with a fixed judge model and version.
- Tracks score distributions over time to catch quality regressions, not just pass/fail.
- Has strict per-run cost and call ceilings, like live tests.
- Feeds regressions back as new deterministic cases whenever a failure can be pinned to a reproducible plumbing bug.
- Draws eval fixtures from real-run trajectories, and exports trajectories as training data (Section 31).

### 20.4 Test categories

The detailed design - the mapping from each of the six test directories to a pytest marker, the marker declarations and strict-marker settings, the naming convention, which of the four CI jobs runs each category, and the reconciliation of Milestone 1's "Deterministic tests" with the harness's cases 1 through 11 as one deliverable - is specified in [development-toolchain.md](development-toolchain.md) and ADR-0025. That document expands this section and Milestones 0, 1, 2, and 3; it does not replace the requirements below, and it adds no test category.

#### Unit tests

Test pure logic:

- State transitions
- Budget accounting
- Policy rules
- Tool schema validation
- Context ordering
- Retry classification
- Path normalization
- Event serialization

#### Contract tests

Run the same contract suite against:

- Fake model provider
- OpenAI adapter fixtures
- In-memory repositories
- PostgreSQL repositories
- Filesystem artifact store
- Fake artifact store
- Fake sandbox
- Container sandbox

#### Integration tests

Use a disposable PostgreSQL database.

Test:

- Migrations
- Repository transactions
- Worker claims
- Lease expiration
- Checkpoint recovery
- Approval pause and resume
- API idempotency
- SSE replay

#### Security tests

Test:

- Cross-tenant access
- Missing authorization
- Path traversal
- Symlink escape
- Oversized tool output
- Tool argument injection
- Secret redaction
- Host filesystem access
- Sandbox network access
- Privileged sandbox commands

#### Live tests

Mark with `@pytest.mark.live`.

Require an explicit environment variable such as:

```text
RUN_LIVE_MODEL_TESTS=1
```
Live tests should have strict call and cost limits.

## 21. Implementation milestones

Do not work on multiple milestones simultaneously. Complete each milestone’s acceptance criteria before moving to the next.

The milestone each stated requirement must hold at - all one hundred and sixty-six gates the fourteen detailed-design specifications declare, the import-boundary walk and the secret scanner this plan declares in Milestone 0, and seven the map declares over the corpus itself, which is one hundred and seventy-five declarations and one hundred and seventy-two registry entries once three aliases are subtracted; the rule that produced every assignment, which is that a gate lands at the milestone that builds the last thing it observes; the one heading, one form, and one `**M<n>.**` suffix that make Milestone 0's docs check writable at all; the three gates declared twice and which document owns each; and the generated census the written distribution is asserted against - is specified in [milestone-map.md](milestone-map.md) and ADR-0027. That document expands this section and Sections 20 and 26 and Milestones 0 through 10; it decides when each stated requirement must hold and states no requirement of its own, so where a gate's statement is wrong the fix belongs in the spec that declares it. Two findings it reports rather than fixes: forty-one of the one hundred and seventy-two registry entries are green before Milestone 2, thirteen of them against a repository with no agent in it, and no milestone with work in it adds none - the three zeros it first reported, at Milestones 6, 8, and 10, were closed by the specifications later written for them, and Milestone 8's MCP half, which those specifications left at zero, by four gates added on the pass that produced this sentence and three more on the pass that gave its authentication configuration a scheme.

### 21.1 Sequencing of the version 2.2 additions

The version 2.2 additions do not all belong at the milestone their descriptive section implies. Sort them by cost-to-defer, not by topic. Three categories:

Design in early - cheap now, expensive to retrofit. Data-model and builder shapes that later code hardens around: the normalized reasoning event and the usage token classes (Milestone 1 domain), event payload schema versioning (Milestone 2), and the prompt-stability invariant in the context builder (Milestone 1). Build these in even though nothing consumes them yet.

Fast-follow once a dependency lands - high leverage, low marginal cost. The OpenAI-compatible chat_completions adapter arrives with the first real adapters (Milestone 3), not later: it is the simplest API, rides the same normalized protocol, and - pointed at a local Ollama or vLLM - gives a free live-test path that does not burn provider credits. Trajectory export is a thin projection over the event log, so it lands right after durable events (Milestone 3) and immediately turns dev and test runs into eval fixtures. Programmatic tool orchestration lands right after the sandbox (Milestone 6).

Keep late - deliberately deferred. Self-improving skills stay behind the static-skill substrate and evaluation evidence (after Milestone 8); inbound messaging surfaces and pairing stay a post-0.1 concern (Milestone 10 and beyond); LLM-assisted approval stays an optional secondary signal added after the deterministic gate is solid. Pulling these forward would trade discipline for shine.

| **Addition**                                   | **Earliest** | **Why here**                                     | **Key dependency**     |
|---|---|---|---|
| Reasoning event + usage token classes          | M1           | Domain shape; costly to retrofit                 | none                   |
| Prompt-stability invariant                     | M1           | Avoids a later builder refactor                  | context builder        |
| Event payload schema versioning                | M2           | Append-only log; retrofit breaks replay          | event log              |
| Cost-source precedence + token classes         | M2           | Lands with persistence and usage                 | usage schema           |
| OpenAI-compatible (chat_completions) adapter   | M3           | Cheapest API; free local live-tests; self-hosted | normalized protocol    |
| Provider plugins (declarative profiles)        | M3           | Natural once more than one provider              | model registry         |
| Reasoning matrix + think-scrubber              | M3           | Needed as real reasoning models stream           | adapters               |
| Trajectory export (minimal, redacted)          | M3           | Thin projection; compounds into evals            | event log              |
| Layered approval: hardline + deterministic     | M4           | Lands with policy and approval                   | policy engine          |
| Programmatic tool orchestration                | M6           | Needs sandbox + tool pipeline; big savings       | sandbox                |
| Credential scrubbing + fail-closed passthrough | M6           | Env passthrough happens at the sandbox           | sandbox                |
| LLM-assisted approval (secondary)              | after M6     | Optional signal, not needed for correctness      | model gateway          |
| Self-improving skills                          | after M8 \*  | Gated by eval evidence; carries risk             | skills, sandbox, evals |
| Memory surface + injection-scan + external     | M9           | The memory milestone                             | memory store           |
| Additive fan-out usage (activation)            | M10          | Activates with subagents                         | subagents              |
| Inbound surfaces + pairing                     | M10+ \*      | New surface area; v0.1 is API/CLI                | multi-device core      |

\* Self-improving skills and inbound surfaces are the two additions kept deliberately late; everything marked M1-M3 is either a cheap data-model decision or a high-leverage fast-follow that is cheaper to do early than to bolt on. The single most consequential move is bringing the OpenAI-compatible adapter into Milestone 3, which also buys a no-cost local live-test path.

### Milestone 0: Repository and engineering foundation

The detailed design of the toolchain deliverables below - what each of the eight required commands runs, the six targets added so that continuous integration invokes no command the Makefile does not define, the compose file's version, port, volume, credentials, and healthcheck, the workflow file with its four jobs and their triggers, the structured-logging bootstrap, the pinned test-directory markers, the egress block, and what "Initial ADRs" resolves to - is specified in [development-toolchain.md](development-toolchain.md) and ADR-0025. That document expands this milestone and Sections 2.2, 19, 20.4, 22, 24, and 25; it does not replace the requirements below, and it adds no deliverable to this list.

#### Implement

- Python project using `uv`
- `src` layout
- Configuration module
- Makefile
- Docker Compose with PostgreSQL
- CI pipeline
- Formatting, linting, typing, and testing
- README with local setup
- Initial ADRs
- `.env.example`
- Structured logging bootstrap

#### Required commands

```bash
make install
make format
make lint
make typecheck
make test
make check
make db-up
make migrate
```
#### Acceptance criteria

- A fresh checkout can be installed from documented commands.
- PostgreSQL starts locally.
- An empty Alembic migration runs.
- CI executes `make check`.
- No application code exists outside the documented module boundaries.

[evaluation-harness.md](evaluation-harness.md), ADR-0001, and ADR-0022 place two deliverables in this milestone that need no runtime: the gate registry, with the docs check that reconciles each spec's declared gates against it, and the structural gates - the import-boundary walk, the transaction-hygiene check, the secret scanner, and contract-module coverage. Both run against an almost-empty repository and stay correct as it fills; added later, they are added against existing violations, which is the situation in which they get relaxed rather than obeyed. The last acceptance criterion above is what the import-boundary walk turns from a statement into a test.

[bootstrap-and-composition.md](bootstrap-and-composition.md) and ADR-0024 give the "configuration module" and "`.env.example`" items above their bodies: the settings object and its eight fields, the three layers, the six committed YAML files, and the operator overlay. They also specify the secret scanner this milestone owes - five rule families, a report that never prints what it matched, an allowlist whose entries require prose, and `.env.example` scanned rather than exempted - and resolve the transaction-hygiene placement by separating two words: the *check* is a Milestone 0 deliverable, and the *gate* it feeds is a Milestone 2 acceptance criterion, because this milestone has no database code to walk. Four static checks join the import-boundary walk here. No acceptance criterion above is changed.

[milestone-map.md](milestone-map.md) and ADR-0027 name the thirteen registry entries that land in this milestone, every one of them true of a repository with no agent in it: the generic import-boundary walk, which this milestone declares and owns as `gate.structure.import_boundary` and which [tool-system.md](tool-system.md) restates rather than declares a second time; the secret scanner, declared here as `gate.structure.no_committed_secrets` and specified in [bootstrap-and-composition.md](bootstrap-and-composition.md); four the evaluation harness owns over the registry, the contract modules, and the evals package; six the map owns over the scheduling record itself; and the migration-graph walk [event-log-and-persistence.md](event-log-and-persistence.md) declares, which registers against this milestone rather than Milestone 2 because the empty Alembic migration the criteria above already require is a graph, and a walk that only begins once a dozen revisions exist has already missed the branch it exists to prevent. The first two are the only registry entries whose owner is this plan rather than a detailed-design spec, because this is where they are declared. The transaction-hygiene check is a deliverable here whose gate is registered later, which is the same separation of two words ADR-0024 made. No deliverable or acceptance criterion above is changed.

### Milestone 1: In-memory vertical slice

The tool registry, the execution pipeline, and the two types the `Tool` port names are specified in [tool-system.md](tool-system.md) and ADR-0021; its build order places steps 1 through 5 in this milestone.

[builtin-tools.md](builtin-tools.md) and ADR-0026 supply the bodies for the two tool items in the list below. `math.calculate` is a hand-written tokenizer and precedence-climbing parser over `decimal.Decimal` at fifty significant digits - not an allowlist over `ast.parse` - with four bounds enforced by the decimal context rather than by the evaluator, and every failure a single `ToolFailureKind` with one of eight reason codes. `system.current_time` reads the injected `Clock` and nothing else, which is what makes its determinism a property of a port rather than a claim about a tool; that document also fixes `Clock.now()` as returning an aware UTC `datetime`, a clarification of a declaration [runtime-loop.md](runtime-loop.md) leaves open. The required demonstration below is a pure function from `17 * 23` to `391`, asserted on the rendered bytes.

[evaluation-harness.md](evaluation-harness.md) and ADR-0022 place the determinism harness in this milestone - the `Clock` and `IdFactory` ports and their pinned implementations, before anything depends on ambient time - along with the case schema, the loader, and the runner. Ten of the twenty-five Section 20.3 cases are writable here, which is what makes "build evaluations before advanced features" a schedule rather than an aspiration. Cases above this milestone are reported as pending, not failed.

[runtime-loop.md](runtime-loop.md) and ADR-0023 specify the loop this milestone builds: the executor and loop split, `RunOutcome` and its five kinds, `Step`, `FailureReason`, the nine additive `Run` fields, and the `Clock` and `IdFactory` ports the harness above depends on - declared here because the runtime is their heaviest consumer. Five of its fourteen hard gates land in this milestone, including the structural gate that only `runtime/executor.py` may call `RunRepository.transition` or `RunQueue.release`. "State transition logic" in the implement list above is the item that document expands.

[bootstrap-and-composition.md](bootstrap-and-composition.md) and ADR-0024 supply the bodies for three items in the list below. "In-memory repositories" is five adapters in `adapters/persistence/memory.py` - agent, session, run, event, and tool invocation - which are production adapters run against the same contract suites as their PostgreSQL counterparts, not test doubles; there is no in-memory `RunQueue`, because that port's entire content is a locking discipline a simulation cannot tell the truth about. "Inline run dispatcher" is a `RunDispatcher` port with one method whose postcondition both adapters satisfy, called after the creating unit of work commits. "Minimal context builder" is context-engine build-sequence step 1. That document also resolves this milestone's event criterion against Milestone 2's event storage by separating repository from storage - one port, two implementations - and specifies the composition root, the settings object, and the CLI contract the acceptance command below runs through.

[milestone-map.md](milestone-map.md) and ADR-0027 place twenty-eight registry entries in this milestone, more than any other, and resolve two placements the items below depend on. The tool system's build step 3 is separated the same way: the idempotency port, its semantics, and its contract suite are here, and the unique index that makes deduplication correct under concurrency is Milestone 2, so the in-memory adapter declares that gap against the checked-in capability table rather than simulating a race it cannot observe. And `CancellationToken` becomes buildable here as a lazily evaluated deadline plus a `SIGINT` handler, both of which need only `Clock` and the process rather than the queue, the lease, or the supervisor Milestone 2 builds; `CancelReason` splits by dependency, with `DEADLINE` in this milestone, `FENCED` at Milestone 2, and `REQUESTED` arriving twice - by poll at Milestone 2 and by endpoint at Milestone 5. No deliverable or acceptance criterion above is changed.

#### Implement

- Core domain objects
- Port interfaces
- State transition logic
- In-memory repositories
- Fake model provider
- Tool registry
- `math.calculate`
- `system.current_time`
- Minimal context builder

- Normalized reasoning event and usage token classes in the domain (designed in, even if unused until Milestone 3)
- Prompt-stability invariant in the minimal context builder (byte-stable prefix; volatile context in the user turn)

- Inline run dispatcher
- Runtime loop
- CLI `agent run`
- Deterministic tests

#### Required demonstration

```text
agent run "What is 17 multiplied by 23?"
```
Expected flow:

```text
run created
model requests math.calculate
tool executes
tool result returned to model
model produces final response
run completes
```
#### Acceptance criteria

- Direct-answer and calculator scenarios pass.
- Invalid tool arguments never reach the tool implementation.
- Maximum-step enforcement works.
- Every state transition is represented by an event.
- No provider-specific code exists in the runtime.

[model-gateway.md](model-gateway.md) and ADR-0002 supply the domain types this milestone designs in and Milestone 3 consumes: the five conversation items, the content parts, the six streaming event classes, `ModelUsage` with its five token classes, the neutral `StopReason` vocabulary, the three error classes, and `FakeModelScript` for the fake provider above. They also give "no provider-specific code exists in the runtime" a test: an import-graph walk rather than a search for SDK names, since the failure that matters is a transitive import through a shared helper.

### Milestone 2: PostgreSQL persistence and durable worker

#### Implement

- SQLAlchemy database adapter
- Alembic migrations
- PostgreSQL repositories
- Append-only event storage
- Per-session event sequence
- Checkpoint storage
- PostgreSQL-backed run claiming
- Worker leases and heartbeats
- Worker command
- Crash recovery
- Idempotency records

- Event payload schema versioning (payload_schema_version) with an upcasting read path
- Usage token classes and cost-source precedence in the schema (Section 6.5)
- Trajectory-export projection scaffold over the event log (Section 31)

#### Acceptance criteria

- API or CLI process can enqueue a run.
- A separate worker executes it.
- Restarting the worker after a checkpoint resumes the run.
- Two workers do not execute the same claimed run concurrently.
- Duplicate event sequence numbers are impossible.
- Idempotent tool calls are not executed twice after recovery.
- Database transactions are never held across provider or tool I/O.

The persistence design for this milestone - the observation-not-durability contract, watermarked projections and the build sequence that reaches them, upcaster totality, checkpoint dispensability, and exactly-once execution under fencing - is specified in [event-log-and-persistence.md](event-log-and-persistence.md), ADR-0003, and ADR-0004, which add seven hard gates and five tracked metrics to the criteria above.

[runtime-loop.md](runtime-loop.md) and ADR-0023 add the runtime side of the same milestone: the heartbeat supervisor and its lease-interval-over-three cadence, `heartbeat -> bool` with `False` meaning fenced, the `WHERE lease_epoch = :lease_epoch` guard on every non-append write, what a fenced worker does with an in-flight model stream, the six checkpoint triggers with the `full` rule and the two additive `checkpoints` columns it needs, and `seed_checkpoint` as a function with two call sites so that deleting a run's checkpoints and resuming reaches the same terminal state. Four more hard gates land here, of which "the lease is released exactly once, including in the crash and fence cases" is the one the acceptance criterion "two workers do not execute the same claimed run concurrently" turns into a test.

[event-log-and-persistence.md](event-log-and-persistence.md) and ADR-0031 give the first two implement bullets above their bodies, and change no criterion. The ORM surface is settled by elimination rather than by preference: declarative mapping of a domain type puts SQLAlchemy inside `domain` and fails Section 5's first rule on the import walk, imperative mapping avoids the import and fails its seventh silently because the domain object becomes the ORM object, and what survives is one declarative row class per table in `adapters/persistence/sqlalchemy_models.py`, two hand-written translation functions per table in a `mappers.py` beside them, and a repository constructed with a live session that never commits, with the unit of work owned by the caller that opens it. The Alembic conventions are the linear graph, the slugged file name, the structure-and-data split, autogenerate as a draft, and `EXPECTED_REVISION` as a module-level constant compared against `alembic_version` at startup rather than a head computed at runtime from the migrations that shipped in the same image - which is the mechanism ADR-0024 decision 6 required and left open. Five more hard gates land across this milestone and Milestone 0, four of them observing Section 24 criteria that until now nothing evaluated.

### Milestone 3: Model adapters (OpenAI, Anthropic, OpenAI-compatible) and normalized streaming

#### Implement

- OpenAI Responses API adapter
- Provider error translation
- Streaming model events
- Usage capture
- Provider response metadata
- Model registry
- Configuration-backed model policy
- Recorded adapter fixtures

- Anthropic Messages adapter (co-equal with OpenAI Responses; Section 10.4)
- OpenAI-compatible chat_completions adapter - vLLM/Ollama/LM Studio/OpenRouter and self-hosted (Section 10.7)
- Declarative provider plugins/profiles and the model registry (Section 10.5)
- Per-provider reasoning matrix and the streaming think-scrubber (Section 10.6)
- Minimal redacted trajectory export over the event log (Section 31)

- Live test marker

#### Acceptance criteria

- The OpenAI SDK is imported only in the OpenAI adapter.
- The runtime passes all tests against fake and recorded adapters.
- A live calculator scenario works when enabled.
- Tool-call IDs are preserved correctly.
- Provider errors are mapped to internal error types.
- No API keys or raw authorization headers enter logs or events.

- A local OpenAI-compatible endpoint (e.g. Ollama) passes the calculator scenario, giving a no-cost live-test path.
- The normalized protocol passes the same contract suite against OpenAI, Anthropic, and a chat_completions endpoint.

[model-gateway.md](model-gateway.md) and ADR-0002 expand these criteria into ten hard gates and six tracked metrics, and specify the fourteen-step build order for this milestone. Three of the criteria above need definitions this section does not carry: "the runtime passes all tests against fake and recorded adapters" gets `FakeModelScript` and the recorded-fixture format there, "tool-call IDs are preserved correctly" gets the stream invariant that makes preservation testable, and "provider errors are mapped to internal error types" gets the three error classes and the `stream_had_output` split that decides who retries. The fixture list under Implement names OpenAI only; the contract-suite criterion naming three providers is the controlling requirement.

### Milestone 4: Policy, approvals, and complete tool lifecycle

#### Implement

- Tool invocation persistence
- Deterministic policy engine
- Principal scopes
- Approval repository and service
- Approval API and CLI
- Pause and resume
- `workspace.read_text`
- `workspace.write_text`
- `workspace.list_files`
- `demo.external_write`
- Tool timeout and output limits

#### Acceptance criteria

- `demo.external_write` cannot execute without approval.
- Approval creates a durable paused run.
- Approval after worker restart resumes correctly.
- Denial becomes a structured tool result.
- Path traversal is rejected.
- Cross-tenant approval access is rejected.
- Unknown tools and missing scopes are denied before execution.

[policy-and-approvals.md](policy-and-approvals.md) expands these criteria into ten hard gates and six tracked metrics, and specifies the twelve-step build order for this milestone. Two of the criteria above need definitions this section does not carry: "denial becomes a structured tool result" gets its field allowlist there, and "cross-tenant approval access is rejected" gets the `approvals.tenant_id` column and the not-found-rather-than-forbidden response it requires. The optional LLM-assisted approval layer remains sequenced after Milestone 6 per Section 21.1 and is not a dependency of this milestone.

[builtin-tools.md](builtin-tools.md) and ADR-0026 fix the classification of the four tools this milestone implements before their behaviour is designed: the three `workspace.` tools and `demo.external_write` each carry a side-effect class, risk level, idempotency class, trust label, scope, timeout, and output ceiling, which is what lets this milestone's policy work run against real registry rows rather than fixtures. Two constraints on the workspace design are stated there rather than left to be noticed - the reader must lower `output_trust` to `EXTERNAL_UNTRUSTED` for any file whose provenance within the run is not established, and `allow_parallel` is false for every tool that writes. The behaviour of all four remains this milestone's to specify.

[runtime-loop.md](runtime-loop.md) and ADR-0023 specify what "approval creates a durable paused run" means on the runtime side: the suspension outcome, the single `finalize` path that releases the lease before the `run.waiting_for_approval` event is visible to a second worker, the resume ladder that re-enters the tool pipeline at step 6 for each pending call rather than re-proposing it, and the gate that a waiting run holds no lease, no worker slot, and no open transaction and is not reclaimed by the lease sweep. That gate is what makes "approval after worker restart resumes correctly" checkable rather than asserted.

### Milestone 5: HTTP API and SSE

#### Implement

- Session endpoints
- Message submission
- Run retrieval
- Cancellation
- SSE event endpoint
- Event replay with `Last-Event-ID`
- API authentication
- Request IDs
- `Idempotency-Key` handling
- API error envelope
- Health endpoints

#### Acceptance criteria

- A client can create a session and submit a message.
- The response returns immediately with `202 Accepted`.
- Events stream while a worker processes the run.
- Reconnecting after an interruption replays missing persisted events.
- Duplicate message submissions do not create duplicate runs.
- Cancellation reaches the worker.
- Production mode cannot start without configured authentication.

Version 0.1 may be considered minimally usable after this milestone, but sandbox execution and artifact support are still required for the full target.

[http-api-and-streaming.md](http-api-and-streaming.md) and ADR-0028 expand every item in the implement list above and give each of the seven acceptance criteria something to test against: a request shape, a response shape, a status code, a required scope, and an error mapping for each of thirteen routes rather than nine; the code list the error envelope needed; request identifiers with a stated provenance and a stated relationship to the trace identifier; `Idempotency-Key` separated from the Milestone 1 tool-call port it shares a name with; authentication specified at what it produces rather than only at what it refuses; and the cross-process half of cancellation, which is that the endpoint writes `runs.cancel_requested_at` - the worker half was already specified. Ten hard gates land here, every gate that document declares, taking this milestone from one registered invariant to eleven. One route is added: `GET /v1/sessions/{session_id}`, so that a client reconnecting with only a session identifier can find its active run.

"Cancellation reaches the worker" is specified in [runtime-loop.md](runtime-loop.md) and ADR-0023 as one `CancellationToken` per run, observed at six points and shared by the loop, the tool executor, and the sandbox, with the rule that a cancellation observed after a call's `effect_sent_at` watermark is set completes the disposition rather than abandoning a half-sent side effect. That document proposes splitting cancellation across Milestones 1, 4, and 5 rather than introducing it whole here, and records the split as an open question, since collapsing it back into this milestone is cheap and introducing it late is not.

### Milestone 6: Isolated execution and artifacts

#### Implement

- Container-backed execution adapter
- Workspace lifecycle
- Resource limits
- No-network execution
- `sandbox.run_command`
- Filesystem artifact store
- Artifact metadata and content endpoints
- Output truncation and artifactization
- Workspace cleanup
- Sandbox security tests

- Programmatic tool orchestration via an in-sandbox RPC bridge (Section 8.5)
- Tiered credential scrubbing and fail-closed env passthrough for executed code (Section 22)

#### Acceptance criteria

- Model-generated code does not run in the worker process.
- A sandbox cannot access the host filesystem.
- A sandbox cannot access the network.
- A sandbox cannot access application secrets.
- CPU, memory, process, and time limits are enforced.
- A generated file can be exported as an artifact.
- Artifact access is tenant-authorized.
- Artifact checksums are verified.

Completion of this milestone defines **Version 0.1**.

### Milestone 7: Context budgeting and structured working state

#### Implement

- Token estimator interface
- Context budget allocator
- History selection
- Tool-result compaction
- Structured run working state
- Optional internal plan update tool
- Summary provenance
- Long-session evaluations

Suggested working state:

```python
class WorkingState(BaseModel):
    objective: str | None
    constraints: list[str]
    tasks: list[TaskState]
    established_facts: list[Fact]
    open_questions: list[str]
    next_action: str | None
```
#### Acceptance criteria

- Long sessions remain under configured context limits.
- Compaction preserves active goals and constraints.
- Original events remain available for replay.
- Summaries retain source event references.
- The runtime does not persist private reasoning.

The assembly design for this milestone - region membership and the prefix hash gate, absolute class caps with history as the only window-scaling class, the yield order, purity of `build()` with compaction as a checkpoint write, elision rather than paraphrase of untrusted spans, and the working-state carry rules - is specified in [context-engine.md](context-engine.md) and ADR-0020, which adds five hard gates and four tracked metrics to the criteria above.

[runtime-loop.md](runtime-loop.md) and ADR-0023 give compaction the call site it has lacked in every prior version of this plan: `build_with_pressure`, which measures pressure, invokes the compactor if the assembled body will not fit, adopts the checkpoint the compactor returns, and measures again, capped at two compactions per step with `ContextOverflow` permanent on the third. The purity of `build()` above is preserved exactly because the write happens at the call site. Two hard gates land here: every `context.build` span is preceded in its step by a pressure measurement, and two builds of one checkpoint produce the same `prefix_sha256`.

### Milestone 8: Skills and MCP integration

#### Skills

The package format below, its front matter, the validator that decides what is storable, the catalog pinned at session open, `skill.load` and the two context classes it fills, the two tables that hold revisions, and every type this milestone needs are specified in [skills.md](skills.md) and ADR-0030. Ten of that document's sixteen hard gates land here; the six that cover authoring land at Milestone 10.

Use a skill package format:

```text
skills/
`-- repository_analysis/
    |-- SKILL.md
    |-- references/
    |-- templates/
    |-- scripts/
    `-- evals/
```
Example manifest:

```yaml
---
name: repository-analysis
version: 1.0.0
description: Analyze a software repository and produce a structured report.
required_tools:
  - workspace.read_text
  - workspace.list_files
  - sandbox.run_command
---

# Instructions

...
```
Only load skill metadata into ordinary context. Load full instructions and files after skill selection.

Skills are also the substrate for agent self-improvement: the agent can author and refine its own procedural-memory skills under governance. See Section 30 and ADR-0013.

#### MCP

The MCP adapter, the `mcp.{server_id}.{tool}` namespace join, the operator-owned server classification, the two transports and their trust zones, and the resource, prompt, sampling, and roots mappings are specified in [tool-system.md](tool-system.md) and ADR-0021.

Implement MCP as an adapter at the integration boundary. MCP distinguishes tools, resources, and prompts; map these to the internal tool, context-source, and skill abstractions rather than allowing MCP concepts to become the application’s core domain model. See the [Model Context Protocol specification](https://modelcontextprotocol.io/specification/2025-11-25).

Implement:

- MCP client lifecycle
- Tool discovery
- Tool schema mapping
- Resource retrieval
- Prompt retrieval
- Namespaced server IDs
- Authentication configuration
- Timeouts
- Output limits
- Trust labeling
- Mock MCP server tests

#### Acceptance criteria

- Skill metadata can be listed without loading full skill contents.
- A selected skill is version-pinned in the run.
- MCP tools pass through normal validation, policy, approval, and tracing.
- MCP output is marked external and untrusted.
- Disconnecting an MCP server produces a structured tool failure.
- The runtime has no direct dependency on MCP SDK types.

### Milestone 9: Long-term memory and knowledge retrieval

Do not begin this milestone until there are concrete use cases and evaluation cases.

#### Separate stores

Implement separate concepts for:

- Session history
- Working state
- Long-term memory
- Knowledge documents
- Artifacts

Do not treat them all as vector records.

#### Memory record

```python
class MemoryRecord(BaseModel):
    id: UUID
    tenant_id: str
    principal_id: str
    scope: str
    subject: str
    statement: str
    source_event_ids: list[int]
    confidence: float
    sensitivity: str
    valid_from: datetime
    expires_at: datetime | None
    status: str
```
#### Required behavior

- Explicit memory-write policy
- Provenance
- Deduplication
- Conflict handling
- User listing
- User editing
- User deletion
- Expiration
- Sensitivity classification
- Hybrid retrieval
- Memory retrieval traces

Start with PostgreSQL full-text search. Add `pgvector` only when semantic retrieval evaluations show a material benefit.

Memory also gets a human surface and injection hardening (v2.2):

- A human-readable, user-editable surface over the structured store (view, edit, delete), with the database remaining the source of truth and retaining provenance, sensitivity, conflict handling, and tenant scope.
- Injection as a frozen snapshot once per session (the prompt-stability invariant, Section 10.1); mid-session writes persist but do not mutate the cached prefix.
- Prompt-injection scanning of memory at load, replacing poisoned entries with \[BLOCKED\] placeholders.
- External semantic memory as a provider behind the memory port (for example Honcho); the builtin store plus at most one external provider.
- An optional persona/identity surface layered over AgentSpec.instructions. Recorded as ADR-0014.

The write path - how episodes become durable, curated beliefs - is specified in detail in [memory-formation-and-consolidation.md](memory-formation-and-consolidation.md) and ADR-0018. The read path - query formation, hybrid recall, ranking, budgeted injection, and retrieval traces - is specified in [memory-retrieval-and-ranking.md](memory-retrieval-and-ranking.md) and ADR-0019.

Never automatically store:

- Credentials
- Authentication tokens
- Untrusted external instructions
- Raw tool output
- Private reasoning
- Transient task details
- Sensitive data without explicit policy

#### Acceptance criteria

- A user can inspect and delete stored memories.
- Conflicting memories are represented rather than silently overwritten.
- Every memory links to source events.
- External content cannot directly write persistent memory.
- Retrieval respects tenant and scope.
- Memory improves defined evaluation cases without increasing policy failures.

### Milestone 10: Scheduling, model routing, and subagents

These are separate optional extensions.

A fourth extension lands here. [skills.md](skills.md) and ADR-0030 place Section 30's authoring loop at this milestone and specify it - `skill_manage` as a capability tool with four operations, the `skill.write` scope, confinement to trusted turns, an approval carrying a diff, `expected_revision` for the concurrent edit, the background review's four restrictions, and rollback as an `AgentSpec` edit - and register six hard gates against it, the first this plan has at Milestone 10. Section 30.5's evidence gate still decides whether authoring is enabled, and the threshold is the one number that document leaves open.

#### Scheduling

Implement a scheduler only after durable on-demand runs are reliable.

A scheduled run must still have:

- Principal
- Agent version
- Policy profile
- Tool scopes
- Budget
- Deadline
- Audit record

#### Second model provider

Add a second provider adapter and run the same contract suite against it.

Provider routing may then consider:

- Required capabilities
- Latency policy
- Cost policy
- Data residency
- Availability
- Evaluation performance

#### Subagents

Represent subagents as a special tool:

```text
delegate.run
```
A child run must have:

- `parent_run_id`
- Explicit objective
- Restricted context
- Restricted tool set
- Child budget
- Child deadline
- Separate trace
- Concise result returned to the parent
- Artifact references rather than a full transcript

Do not implement handoffs initially. The parent agent should retain responsibility for the user interaction and final response.

#### Gate for multi-agent work

Add subagents only when evaluation evidence shows that a single agent fails because of:

- Independent parallel work
- Context isolation
- Specialized permissions
- Specialized tools
- Independent verification

Do not add role-named agents merely for planning, writing, or criticism without evidence of improvement.

## 22. Security baseline

[development-toolchain.md](development-toolchain.md) and ADR-0025 place the file this section requires: `docs/security.md` exists from Milestone 0, because the definition of done in Section 24 requires security implications to be documented for every milestone and Milestone 0 already ships two security controls. That document also specifies the secret scanner's treatment of `.env.example` and the Milestone 0 egress block; it changes no control listed below.

Create `docs/security.md` documenting these trust boundaries:

```text
Trusted:
- Application code
- Versioned agent configuration
- Versioned skills
- Policy rules

Partially trusted:
- Authenticated user input
- Approved internal tools

Untrusted:
- Websites
- Documents
- Email
- MCP servers
- Tool output
- Generated code
- Uploaded files
```
Required controls:

- Tenant isolation
- Principal authorization
- Least-privilege tool scopes
- Schema validation
- Output validation
- Approval for consequential actions
- Secret redaction
- No raw credential access by the model
- Sandboxed code execution
- Network deny by default
- Tool timeouts
- Tool output limits
- Run budgets
- Cancellation
- Audit events
- Artifact authorization
- Memory write restrictions
- Idempotency protection

- Per-tenant concurrency and rate limits (fair scheduling and cost control across tenants)
- Non-bypassable hardline rules, frozen at load (destructive-command and secret-exfiltration patterns) that no mode or in-process code can disable
- Tiered credential scrubbing for child runs and subprocesses, with env passthrough that fails closed on platform and provider credentials
- Default-deny for untrusted inbound surfaces, with explicit pairing before any run is created (Section 29)

- Prompt-injection tests

The system prompt must not contain secrets.

Tools requiring credentials should obtain them from a credential broker or server-side configuration during execution. The model should receive only a credential reference or capability, never the raw credential.

## 23. Coding standards

The coding agent must follow these rules:

1.  Use complete typing for public interfaces.
2.  Prefer immutable domain values where practical.
3.  Use timezone-aware UTC datetimes internally.
4.  Use UUIDs for externally exposed IDs.
5.  Use enums for statuses and decision types.
6.  Validate external input at the boundary.
7.  Use structured error values.
8.  Do not catch `Exception` without re-raising or converting it explicitly.
9.  Do not log full prompts or secrets.
10. Do not create hidden global state.
11. Do not use unrestricted `eval`, `exec`, or shell strings.
12. Do not hold database transactions across external I/O.
13. Do not couple tests to live model providers.
14. Do not mock internal pure functions unnecessarily.
15. Prefer contract tests for adapter implementations.
16. Add a migration for every schema change.
17. Add an ADR for architectural changes.
18. Keep functions small enough that state transitions are obvious.
19. Ensure cancellation propagates through async operations.
20. Document every public tool’s side effects and idempotency behavior.

## 24. Definition of done for every milestone

The last item below - "`make check` succeeds" - is defined in [development-toolchain.md](development-toolchain.md) and ADR-0025, which fix `make check` as `lint typecheck test-fast` so that it stays runnable on a fresh checkout with no database and no provider credential, and which make it the exact union of the two continuous-integration jobs that need neither. That document adds no item to this list.

Two items below - "Database migrations upgrade from a clean database" and "Database migrations upgrade from the previous revision" - are conditions of every milestone that nothing evaluated. [event-log-and-persistence.md](event-log-and-persistence.md) and ADR-0031 give them the checks that decide them: `gate.event.migration_clean` runs `upgrade head` against an empty database and then requires an autogenerate run against the same metadata to produce an empty diff, and `gate.event.migration_stepwise` walks the revision graph one revision at a time, upgrading and downgrading each, with data revisions exempt from the downgrade half by declaration. Both depend on schema in tests being created by running the migrations rather than by `metadata.create_all`, which is the shortcut that makes these two items unfalsifiable. That document adds no item to this list either.

A milestone is not complete until:

- All acceptance criteria pass.
- Unit tests are added.
- Integration tests are added where relevant.
- New adapters pass contract tests.
- Type checking passes.
- Formatting and linting pass.
- Database migrations upgrade from a clean database.
- Database migrations upgrade from the previous revision.
- Public interfaces are documented.
- New configuration appears in `.env.example`.
- Security implications are documented.
- Relevant ADRs are added or updated.
- No secrets appear in fixtures, logs, or committed files.
- `make check` succeeds.

Do not leave untracked TODO comments. Convert deferred work into documented issues or a roadmap section.

## 25. Initial local-development experience

The detailed design of the commands below - the contents of every Makefile target, why `docker compose up -d postgres` and `alembic upgrade head` remain two steps, the compose service the first of them starts, and the console script that makes `uv run agent api` the invocation rather than a module path - is specified in [development-toolchain.md](development-toolchain.md) and ADR-0025. That document expands this section and Milestone 0; it does not replace the sequence below, and it removes no item from the README list.

The final setup should support:

```bash
git clone <repository>
cd agent-core

cp .env.example .env
uv sync

docker compose up -d postgres
uv run alembic upgrade head

uv run agent api
uv run agent worker
uv run agent chat
```
And:

```bash
make check
```
The README must explain how to:

- Use the fake provider
- Configure OpenAI
- Start PostgreSQL
- Run migrations
- Start the API
- Start a worker
- Use the CLI
- Run deterministic evaluations
- Run optional live tests
- Inspect traces
- Resolve an approval
- Reset the development database

## 26. First assignment for the coding agent

Begin with **Milestone 0 and Milestone 1 only**.

Do not implement PostgreSQL persistence, OpenAI integration, memory, MCP, sandboxing, scheduling, or subagents until the in-memory vertical slice is complete and tested.

The first implementation should produce:

1.  Repository scaffold
2.  `pyproject.toml`
3.  Development commands
4.  Configuration system
5.  Domain models
6.  Port interfaces
7.  State machine
8.  In-memory repositories
9.  Fake model provider
10. Tool registry
11. Calculator tool
12. Current-time tool
13. Minimal context builder
14. Inline runtime loop
15. CLI `agent run`
16. Deterministic tests
17. Initial ADRs
18. README

The first demonstration must execute:

```bash
uv run agent run "What is 17 multiplied by 23?"
```
Expected trace:

```text
run.queued
run.started
model.request.started
model.response.completed
tool.call.proposed
tool.call.authorized
tool.call.started
tool.call.completed
run.checkpointed
model.request.started
model.response.completed
assistant.message.completed
run.completed
```
The coding agent should conclude the first assignment by reporting:

- Files created or changed
- Architecture decisions made
- Commands required to run the project
- Test and type-check results
- The exact demonstration output
- Known limitations
- Any deviation from this brief and the corresponding ADR

The coding agent should not begin Milestone 2 until Milestones 0 and 1 pass all acceptance criteria.

## 27. Run, turn, and session model

This section resolves the domain’s load-bearing ambiguity: what a run is relative to a conversational turn and a session, where a new run’s prior conversation comes from, how the agent can pause for user input mid-run, and whether a session may have concurrent runs. It expands Sections 6.3, 6.4, 12, and 16, and is recorded as ADR-0009.

[runtime-loop.md](runtime-loop.md) and ADR-0023 supply the mechanism this section specifies the behaviour of: suspension as one mechanism with three kinds, so that entering either `WAITING_*` state releases the lease, checkpoints, and emits an event in one place rather than at each of the loop's five exits; a child-run wait reusing `WAITING_FOR_APPROVAL` with a typed suspension kind rather than adding a fourth non-terminal status, which is recorded as an open question; and 27.5's "reject or queue" resolved to reject, with `ConflictError` and HTTP 409, except where the active run is `WAITING_FOR_USER` and the deterministic routing rule sends the text to that run's input endpoint - because ADR-0004's partial unique index makes queueing impossible at the database level as currently specified. The definitions in 27.1 are unchanged, including that a turn has no domain object.

### 27.1 Definitions

- Session: a durable, ordered conversation between one principal and one agent version. It owns the authoritative event log and the per-session sequence, and is long-lived.
- Turn: one user input and the agent’s complete response to it, including any tool use and approvals. It is a unit of conversation.
- Run: the durable execution of exactly one turn. A run is the unit of scheduling, leasing, checkpointing, budgeting, and recovery.

Decision: run == turn. Submitting a user message creates exactly one run; that run ends when the agent has produced its final response for that message, or fails, is cancelled, or times out. A session is a sequence of such runs. Subagents get their own child runs (27.6), not turns.

### 27.2 State machine (expanded)

```text
QUEUED
  -> RUNNING
RUNNING
  -> WAITING_FOR_APPROVAL     (a tool needs human approval)
  -> WAITING_FOR_USER         (the agent asked the user a question)
  -> COMPLETED                (final response produced)
  -> FAILED                   (terminal error / budget / deadline)
  -> CANCELLED
WAITING_FOR_APPROVAL
  -> RUNNING                  (approval resolved -> revalidate policy)
  -> CANCELLED
  -> FAILED                   (approval expired, per policy)
WAITING_FOR_USER
  -> RUNNING                  (user reply delivered to the same run)
  -> CANCELLED
  -> FAILED                   (input deadline passed, per policy)
```
Both WAITING\_\* states release the worker lease, checkpoint the run, and emit an event. Neither holds a database transaction or a worker slot while waiting. Only explicit transition functions may modify run status.

### 27.3 WAITING_FOR_USER and mid-run clarifying questions

WAITING_FOR_USER is what lets an agent ask a clarifying question without ending the turn. It is entered by a dedicated, deterministic control tool rather than by parsing model prose:

```yaml
conversation.ask_user
  input:  { "question": str,
            "expected": "free_text" | "choice",
            "choices": [str] | null,
            "deadline_seconds": int | null }
  effect: checkpoint; run -> WAITING_FOR_USER; release lease;
          emit run.waiting_for_user
```
Delivering the answer resumes the same run rather than starting a new one:

```http
POST /v1/runs/{run_id}/input
{ "content": [ { "type": "text", "text": "Use the EU region." } ] }
```
The service validates that the run is WAITING_FOR_USER, appends a UserMessage to the conversation as the resolution of the outstanding question, re-enqueues the run (QUEUED -\> claimed -\> RUNNING), and the loop continues from the checkpoint. This is distinct from POST /sessions/{id}/messages, which always starts a new turn and run.

Design rule: routing user text to a waiting run versus a new run is a deterministic decision made by the API from the run’s state, never by the model. If a message arrives for a session whose latest run is WAITING_FOR_USER, the API applies one configured policy - treat it as the awaited input, or reject it with guidance - and must not silently do both.

### 27.4 Cross-run conversation continuity

A new run must begin with the session’s accumulated conversation, but checkpoints are per-run. Resolve this explicitly:

- The authoritative history is the session event log. The context builder reads prior conversation from a session-history projection built from events, not from the previous run’s checkpoint.
- At run start, seed the new run’s initial checkpoint conversation from that projection (subject to the context budget and compaction) plus the new user message.
- A run’s checkpoint holds only that run’s evolving working conversation; it is not the system of record for the session.
- Provider-opaque reasoning items never cross a run boundary; only portable items (system, user, assistant text, tool calls and results, and compacted summaries) carry forward.

This keeps the run as the unit of execution and recovery while the session remains the unit of memory, and it makes provider-switching between turns safe by construction.

### 27.5 Concurrency within a session

Decide deliberately whether a session may have more than one in-flight run.

- Default: at most one active (non-terminal) run per session. This makes the per-session event sequence contention-free, keeps ordering intuitive, and matches a single conversational thread.
- Enforce it with a partial unique constraint (one non-terminal run per session_id) or a conditional insert; reject or queue a second submission while one run is active.
- Allocate the per-session sequence inside the same short transaction that appends an event (SELECT ... FOR UPDATE on the session row, or an atomic increment of next_event_sequence), so even permitted concurrency cannot produce duplicate or gapped sequences; UNIQUE(session_id, sequence) is the backstop.
- If a product later needs parallel branches in one session, model them as separate sessions or as child runs (27.6), not as concurrent same-sequence writers.

### 27.6 Subagents and child runs

Subagents (Section 21, Milestone 10) are child runs, not turns. A child run has its own run row with parent_run_id set, its own lease, budget, deadline, checkpoint, and trace, and it belongs to the parent’s session or a dedicated child session per policy. The parent turn does not complete until its child runs reach a terminal state. Child runs never write to the parent’s conversation directly; they return a concise result the parent incorporates.

### 27.7 Idempotency of submission and input

Turn creation is idempotent on the client Idempotency-Key (Section 16): a repeated submit with the same key returns the original run rather than creating a second turn. Input delivery to a WAITING_FOR_USER run should be idempotent on (run_id, outstanding_question_id) so a retried answer does not double-resume the run.

### 27.8 Acceptance criteria

- Submitting a message creates exactly one run; a duplicate Idempotency-Key returns the original.
- A new run in an existing session sees prior conversation reconstructed from the session log, not from a prior checkpoint.
- An agent can enter WAITING_FOR_USER via conversation.ask_user, and POST /runs/{id}/input resumes the same run.
- A message to a session with an active run is handled by a deterministic, configured rule (queued, rejected, or treated as awaited input) - never ambiguously.
- Provider-opaque reasoning items are never present in a run’s seeded conversation from a prior run.
- At most one non-terminal run exists per session under the default policy, enforced at the database.
- Concurrent event appends cannot violate UNIQUE(session_id, sequence).

## 28. Sandbox isolation architecture

The mechanism design for this section - the egress allowlist that 28.5 requires and [tool-system.md](tool-system.md) already depends on by name, given a YAML grammar, an owner, one leftmost wildcard label, mandatory explicit ports, and two independent enforcement points, of which the address denylist runs first and cannot be waived by any allowlist entry; the credentials rule stated as a topology rather than a discipline, so that the execution service holds nothing worth stealing and an escape lands somewhere empty; the fourth `SandboxMechanism` value that makes the development fake a production adapter and refuses it alongside plain Docker; the reaper that makes lease expiry destroy sandboxes rather than leak them; and one contract suite run against four adapters so 28.6's promise is a test rather than an intention - is specified in [sandbox-isolation.md](sandbox-isolation.md) and ADR-0029. That document is subordinate to this one the way [runtime-loop.md](runtime-loop.md) is subordinate to Section 12: where the two overlap, the sentence below is the requirement and the specification's is the mechanism. The threat model, the rejection of the Docker socket, the choice of a kernel-isolating runtime, the execution-service topology, and every restriction in 28.4 are unchanged.

Section 18 lists what the sandbox must forbid; this section decides how the platform actually provides isolated compute and where the trust boundary sits. "Docker-compatible containers" understates a real security decision, because the mechanism the worker uses to create containers is itself the thing an escape will target. Recorded as ADR-0008.

### 28.1 Threat model

Assume model-generated code is hostile. Concretely defend against:

- Container escape to the host kernel (the primary risk with shared-kernel containers).
- Access to the orchestrator’s credentials: database, object storage, provider API keys, cloud IAM.
- Lateral movement to other tenants’ workspaces or runs.
- Network exfiltration of data the code was given.
- Resource exhaustion (CPU, memory, disk, PIDs, inodes) as denial of service.
- Persistence across runs via a dirty or shared workspace.

### 28.2 The mechanism decision

Choose the isolation mechanism explicitly; the options differ by the strength of the boundary and by operational cost.

- Docker socket from the worker (mounting /var/run/docker.sock): rejected. It is effectively root on the host and turns any worker-side flaw into a full compromise. Never mount the Docker socket into a process that handles untrusted code or orchestrates it.
- Shared-kernel containers with hardening (rootless runtime, user namespaces, seccomp, dropped capabilities, read-only root filesystem, no-new-privileges): an acceptable baseline for lower-risk deployments, but the boundary is still the host kernel, so a kernel vulnerability is an escape.
- Kernel-isolating sandboxes - gVisor (runsc) or a microVM (Firecracker or Cloud Hypervisor via Kata): recommended for a multi-tenant production platform. gVisor interposes a user-space kernel; microVMs give each run its own kernel behind a hardware virtualization boundary. Both raise the cost of escape from one kernel bug to one hypervisor or sentry bug.

Decision for a multi-tenant production target: default the container adapter to a microVM or gVisor-backed runtime, and treat plain shared-kernel Docker as a development-only fallback selected by configuration.

### 28.3 Execution-service topology

Keep the mechanism behind the existing ExecutionEnvironment port and run it as a separate, least-privileged component, not inside the worker:

- The worker calls the ExecutionEnvironment port; it never talks to a container runtime directly.
- A dedicated execution service (or node pool) owns sandbox lifecycle. It holds no application secrets, no database credentials, and no provider keys.
- The execution service runs on separate hosts or nodes from the API, worker, and database where the deployment allows, so an escape lands somewhere with nothing worth stealing.
- Sandboxes are created per run (or per tool call) and destroyed after; they are never reused across tenants.

```text
worker --(ExecutionEnvironment port)--> execution service --> { microVM | gVisor }
   |                                         |
 secrets, DB, keys                       no secrets, no DB, no keys
```
### 28.4 Runtime restrictions (concrete)

Realize Section 18.2 as enforced settings, not documentation:

- Network: default deny at the network namespace or firewall, not merely "no DNS". If a tool legitimately needs egress, allow a named destination list through a proxy the sandbox cannot reconfigure.
- Filesystem: a fresh read-write workspace mounted per run; a read-only root image; no host mounts; no Docker socket; tmpfs with a size cap for scratch.
- Identity: a non-root user; no-new-privileges; all capabilities dropped except those required; user namespaces so in-container root maps to an unprivileged host UID.
- Syscalls: a seccomp profile (default deny with an allowlist) even under gVisor or a microVM, for defense in depth.
- Resources: CPU quota, a memory limit with OOM handling, a PID limit, a disk and inode quota, and a hard wall-clock timeout enforced by the execution service (not by the model-provided timeout_seconds alone).
- Cleanup: destroy the sandbox and wipe the workspace after export, and verify exported artifacts by SHA-256 on the way out.

### 28.5 Data flow and egress

Even with the network denied, the model plus its tools form an exfiltration path: untrusted code can write a secret it was (wrongly) given into an artifact the model then reads and emits. Controls:

- Never inject credentials into the sandbox. Tools that need credentials run outside the sandbox via the credential broker (Section 22); the sandbox receives data, never keys.
- Treat sandbox stdout, stderr, and produced files as EXTERNAL_UNTRUSTED (Section 11.2). Large outputs become artifacts, with only a summary and a reference returned to the model.
- Size-cap and scan tool output before it re-enters context; enforce maximum_output_bytes at the execution service.
- If egress is enabled for a specific tool, route it through an allowlisting proxy and log destinations; deny by default.

### 28.6 Local development versus production

Make the strength of the boundary a configured choice with a safe default:

- Development: a fake ExecutionEnvironment (no real code execution) for deterministic tests, and optionally hardened rootless Docker for convenience.
- Production: a kernel-isolating runtime (microVM or gVisor) is required; startup must refuse to run untrusted code under the development fallback when AUTH_MODE is not dev.
- The choice is configuration behind the same port, so the contract tests (Section 20.4) run against both the fake and the real sandbox unchanged.

### 28.7 Acceptance criteria

- Model-generated code runs only inside the isolated runtime, never in the API, worker, or execution-service control plane.
- The sandbox has no network by default, no host mounts, no Docker socket, no application secrets, and no database or cloud credentials.
- In-container root maps to an unprivileged host user; capabilities are dropped; seccomp is enforced.
- CPU, memory, PID, disk and inode, and wall-clock limits are enforced by the execution service and verified by tests.
- A container escape in a test harness cannot reach secrets or another tenant’s workspace (red-team test).
- Production startup refuses the development sandbox fallback.
- Artifacts are content-addressed and verified by checksum on export.

## 29. Multi-device operation and the shared core

The agent must be usable from many devices - phone, laptop, desktop, web - while behaving as one continuous assistant. This is largely a consequence of the existing architecture: because PostgreSQL is the source of truth and devices are clients of the API, the durable state is already shared. This section makes the split explicit, identifies every component that must be cloud-shared (not only memory), introduces the Device concept for capabilities that are inherently local to one machine, and defines the cross-device flows. Recorded as ADR-0011.

Section 29.8 defers this section's own subject, so it is audited rather than expanded. [multi-device-and-surfaces.md](multi-device-and-surfaces.md) and ADR-0034 check the seam instead of building behind it: the eight places the corpus already holds a device-shaped hole and needs no edit at all, the five where it does not - a third registration source at attach, device lifecycle events with no session to be charged to, a fourth suspension kind for a hand-off, no client attributed on a write, and `NotificationService` as a port name with no mechanism behind it - the placement of 29.6's four items under the rule that a port lives in the module named for the capability it abstracts, per-device scopes as an intersection stamped on the run at submission rather than a second evaluation path, and a Surface as this section's `Device` model with an empty capability set and one genuinely new mechanism, the session-key resolver. That document declares no gates and changes no requirement below. 29.1 through 29.3 stand as written, and 29.4's eight bullets, 29.5's four flows, 29.6's model and ports, 29.7's four security notes, and 29.8's scope for 0.1 are carried forward rather than reinterpreted. Two conflicts with later specifications are named there and resolved in the specifications' favour: 29.5's "queue or reject" for a second device on a busy session is reject, which [runtime-loop.md](runtime-loop.md) settled against ADR-0004's partial unique index, and 29.4's presence-based exposure yields to the pinned prefix, which [tool-system.md](tool-system.md) states as advertisement is pinned and availability is resolved at call time. The 0.1 obligation 29.8 states is already discharged: reads and writes are principal-scoped and served from the core, and a second client attaching and replaying is a hard gate at Milestone 5.

### 29.1 Principle: one shared core, many thin clients

There is exactly one authoritative instance of the user’s state, in the cloud. Devices hold no authoritative state; they render, capture input, stream events, and optionally expose device-local capabilities. Any device can attach to any session and, via SSE replay with Last-Event-ID (Section 16), resynchronize to the exact persisted prefix. A write from any device goes to the shared core; there is no device-to-device sync and no offline-authoritative copy to reconcile.

### 29.2 What must be cloud-shared

Memory is the obvious shared component, but it is far from the only one. Everything below is authoritative in the cloud and identical regardless of the device in use:

- Identity and principals - authentication, tenant, roles, and scopes. A device authenticates to the core; it is not itself a trust boundary.
- Sessions, runs, events, and checkpoints - the conversation and its execution history, so a turn started on one device is visible and resumable on another.
- Long-term memory and knowledge - user facts and retrieved documents (Milestone 9).
- Artifacts - files the agent produces, addressed by opaque ID so any device can fetch them (Section 18.4).
- Approvals - the pending-approval queue, so a consequential action can be reviewed and resolved from any authorized device (Section 9).
- Agent configuration - versioned AgentSpec (instructions, enabled tools, policy profile), so behavior is consistent everywhere (Section 6.1).
- Policy rules and the policy engine - the security decision is enforced centrally; a device is never where policy is evaluated (Sections 9, 22).
- Credentials and the secret broker - provider API keys and tool credentials live server-side and are never distributed to devices (Section 22).
- Model gateway - all model calls go through the core, which holds the keys and enforces routing, budgets, and provider pinning (Section 10).
- Tool execution and the sandbox - cloud tools and isolated code execution run server-side; a phone cannot host the sandbox (Sections 8, 18, 28).
- Usage, cost, and budgets - a single ledger, so limits aggregate across devices rather than per-device (Section 6.5).
- Scheduling - scheduled runs fire in the cloud regardless of which devices are online (Milestone 10).
- Observability and audit - one trace and audit stream across all devices (Section 19).

### 29.3 What stays device-local

A small set of things are inherently tied to one machine and must not be centralized:

- The client UI and input - rendering, keyboard, microphone, camera.
- Device-local capability providers - the local filesystem, a browser on that machine, local applications, and locally-running MCP servers. These exist only while that device is connected.
- The live stream connection - transient; replaced by SSE replay on reconnect from any device.
- Optional read-through caches for offline viewing - never authoritative.

### 29.4 Device-scoped capabilities (the hybrid case)

Between cloud and local sits the important hybrid: tools whose execution must happen on a specific device (read a file on this laptop, drive the browser on that desktop). Model these as device-scoped tools, routed to a connected device, without weakening the security model.

- Register each connected device as a Device belonging to the principal, with an ID, a declared capability/tool set, granted scopes, and a presence (connected / last-seen).
- A device-scoped ToolSpec is bound to a device (or a device selector). The tool registry and context builder expose it only when a qualifying device is connected (extend the tool-filtering rule in Section 11.1).
- Execution is routed to the target device over a device channel (the same pattern as an MCP transport), but the call still passes through the full pipeline: schema validation, principal scopes, policy, approval, timeout, output limits, and tracing (Section 8.3). The device is an execution target, not a policy or credential authority.
- Device output is untrusted: label it EXTERNAL_UNTRUSTED (or USER where appropriate) in the trust model (Section 11.2); it can never redefine policy or grant permissions.
- Offline handling: if the target device is not connected, the tool is unavailable and a call yields a structured tool failure (mirror the MCP-disconnect behavior in Milestone 8), never a hang.
- Per-device scopes: a principal may grant a device a subset of scopes (a shared desktop gets fewer than a personal laptop), enforced centrally.
- Surfaces beyond devices: the same registry/presence pattern covers inbound messaging channels (Telegram, Slack, email, and similar) as Surfaces - a Surface is a device-like client with a presence and a capability set, unified under one session-key resolver (DM per user, group per participant, thread shared).
- Untrusted inbound: an unknown sender on a Surface is default-denied and must complete an explicit pairing step (one-time code, expiry, rate-limit, lockout) before any run is created on their behalf; pairing writes them into a per-Surface allowlist (ADR-0017).

### 29.5 Cross-device flows

- Continuity: start a turn on one device, watch it complete on another - both attach to the run’s SSE stream, and replay fills any gap.
- Approvals and notifications: route an approval request to the user’s present devices via a presence / notification service; any authorized device can resolve it, and resolution is idempotent (first resolution wins).
- Hand-off of device-scoped work: if a run needs a capability only Device A has and A is offline, the run enters WAITING_FOR_USER or returns a structured "capability unavailable" result rather than failing silently, so the user can connect A and continue (Section 27.3).
- Concurrency: the single-active-run-per-session rule (Section 27.5) prevents two devices racing the same session; a second device submitting to a busy session follows the configured policy (queue or reject).

### 29.6 New domain and ports

The additions are small and fit the existing model:

```python
class Device(BaseModel):
    id: UUID
    tenant_id: str
    principal_id: str
    name: str
    kind: str                 # "desktop" | "laptop" | "mobile" | "web"
    capabilities: list[str]   # device-scoped tool names it can serve
    granted_scopes: set[str]
    status: str               # "connected" | "disconnected"
    last_seen_at: datetime
```
- DeviceRegistry / DevicePresence port - register devices, track presence, and resolve which device can serve a capability.
- DeviceChannel port - invoke a device-scoped tool on a specific device and stream its result (an adapter over the concrete bridge or websocket).
- NotificationService port - deliver approval and completion notifications to a principal’s present devices.
- Extend the tool registry and context builder to filter and route device-scoped tools by presence.

### 29.7 Security notes

- A device authenticates like any client; compromise of one device must not expose credentials (there are none on the device) or escalate scopes (scopes are central and per-device).
- Device-scoped tool calls are subject to the same policy and approval gates as any tool; "it ran on my laptop" is not an authorization.
- Treat all device-provided content as untrusted input for prompt-injection purposes.
- Revoking a device immediately removes its scopes and presence server-side; no local state needs wiping because none is authoritative.

### 29.8 Scope for version 0.1

The shared core is already multi-device by construction; do not build device-scoped routing until it is needed. For 0.1: ensure every read and write is principal-scoped and served from the core (it is), and confirm a second client can attach to a session and replay. Defer the Device concept, presence, device-scoped tool routing, and notifications to a milestone with concrete use cases, exactly as memory and MCP are deferred.

## 30. Self-improving skills (agent-authored procedural memory)

Skills in the plan begin as static packages (Milestone 8). This section adds the capability that most distinguishes a modern general-purpose agent: the agent authoring and refining its own procedural memory - under governance our architecture is uniquely positioned to provide. Recorded as ADR-0013.

The detailed design - the package layout, its `SKILL.md` front matter, and the ten numbered rules a validator applies before a package is storable; the identity and revision grammar that makes a pin resolvable and an archived revision still readable; seven types the corpus had never named, among them `SkillManifest`, `SkillRevision`, `SkillPin`, and `CatalogEntry`, and the two ports that store them; two tables, one archive, and no expiry; the catalog pinned at session open, so publishing mid-session cannot change what a run can already see; the two new context classes and the prefix ceiling that moves from 13,500 to 15,000 to hold them; `skill.load`, what sticks after a load, and the third load that fails rather than evicting the first; `required_tools` checked at load and recorded as a note rather than a refusal; why a skill that ships a script still ships no tool; MCP prompts as read-only skills; `skill_manage` at `risk: HIGH` and `CONDITIONALLY_IDEMPOTENT`, requiring the `skill.write` scope, denied below `USER` trust, and carrying a diff into approval rather than an argument blob; `expected_revision` and the edit that loses; the background review as a child run with four restrictions; and rollback as an `AgentSpec` edit rather than a delete - is specified in [skills.md](skills.md) and ADR-0030. That document expands this section and Sections 8, 9, 11, 16, 20, 27, and 28 and Milestones 8 and 10; it does not replace the requirements below. 30.1's distinction between procedural memory and a tool, 30.2's two authoring paths, all six of 30.3's governance guarantees, 30.4's metadata-only rule, 30.5's evidence gate, and 30.6's five acceptance criteria are unchanged. One correction: 30.2 calls `skill_manage` a control tool and it is not one, because it writes durable tenant state that outlives the run - the line is [tool-system.md](tool-system.md)'s and the reclassification is made there.

### 30.1 Skills as procedural memory

A skill is procedural memory - instructions and references that shape how a task is done - not a new code tool. It is distinct from declarative memory (user facts, Milestone 9) and from tools (executable capabilities). The agent may create and edit skills; it may not register arbitrary new tools at runtime. Adopt the agentskills.io open format so skills interoperate with the wider ecosystem.

### 30.2 The authoring loop

- Foreground: a skill_manage control tool (create / edit / patch / version / archive) the agent calls after a hard task to capture what it learned.
- Background review: an optional post-run child run that reviews the transcript and proposes skill updates, returning a concise summary to the parent.
- Both paths write versioned skills; nothing is auto-registered as a tool.

### 30.3 Governance (why we can do this safely)

A local-first agent can let the model rewrite its own instructions freely because it already runs code with the user’s privileges. A multi-tenant platform cannot, and does not have to:

- Versioning: every agent-authored skill version is pinned per run, exactly like AgentSpec, so a run is reproducible and a bad skill is one revision to roll back.
- Provenance: each skill version links to the source events that produced it (Section 6.8), so every learned behavior is auditable.
- Policy gating: authoring a skill is a consequential action requiring the right scope and, by policy profile, approval.
- Restricted review: the background-review child run gets a whitelisted tool set ({memory, skills}), reads before writing, and may edit only skills it created.
- Injection resistance: skill authoring is confined to trusted turns; skill content is scanned at load; untrusted tool output cannot drive a skill write.
- Sandboxed scripts: any executable a skill carries runs in the sandbox (Section 28) under normal policy - a skill is never an isolation bypass.

### 30.4 Loading and lifecycle

- Only skill metadata enters ordinary context; full instructions load on selection (Milestone 8).
- Per-agent and per-surface enable/disable; usage tracking; pin and archive.
- Skills are tenant- and principal-scoped like every other user-owned record.

### 30.5 Scope and gate

Gate rollout behind evaluation evidence (Section 20) that self-authored skills improve defined eval cases without increasing policy failures. Ship the static-skill substrate (Milestone 8) first; enable authoring when the evidence supports it.

### 30.6 Acceptance criteria

- The agent can create and revise a skill through skill_manage, and every version is pinned and provenance-linked.
- A background-review run can edit only skills it created, and only with whitelisted tools.
- Skill authoring is denied without the required scope, and untrusted tool output cannot trigger a skill write.
- A bad skill version can be rolled back by pinning an earlier version.
- Enabling self-authored skills improves target eval cases without increasing policy-failure rates.

## 31. Trajectory capture and export

The event log already records every run in full (Section 6.8). This section adds a projection that turns those runs into two assets: evaluation fixtures and model-training data. Recorded as ADR-0016.

### 31.1 What is exported

- A trajectory is the portable conversation plus tool calls, tool results, and the run outcome, in a standard format (for example ShareGPT / messages).
- Both successful and failed trajectories are captured; failures are often the most useful for training.
- Excluded: secrets, raw reasoning (ADR-0006/0007), and policy-restricted PII. Export is tenant-scoped and consent-gated.

### 31.2 Uses

- Eval fixtures: convert real runs into deterministic eval cases (Section 20), so the suite reflects real usage, not only synthetic scenarios.
- Capability regression: feed the capability-evaluation track with real-world distributions.
- Fine-tuning and distillation: training data for self-hosted open models (Section 10.7) - closing the loop from usage to a better in-house model.

### 31.3 Acceptance criteria

- A completed run can be exported as a redacted trajectory with no secrets, raw reasoning, or restricted PII.
- Export honors tenant scope and per-principal consent.
- Exported trajectories can be replayed as deterministic eval cases.
- Failed runs are captured and labeled distinctly from successful ones.
