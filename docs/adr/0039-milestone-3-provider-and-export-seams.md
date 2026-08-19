# ADR-0039: Milestone 3 provider and trajectory-export seams

- Status: Accepted
- Date: 2026-08-03
- Related: Milestone 3, ADR-0002, ADR-0006, ADR-0007, ADR-0012,
  ADR-0016, ADR-0024, ADR-0029, ADR-0032
- Detailed design: `docs/plan/model-gateway.md` and
  `docs/plan/event-log-and-persistence.md`

## Context

Milestone 3 is the first implementation of real provider adapters, durable
provider selection, exact model accounting, and governed trajectory export.
The detailed designs fix their externally visible contracts but leave several
composition choices to the first implementation. One conflict also remains in
the corpus: the bootstrap design freezes `Settings` at eight fields, while the
trajectory-export design requires operator-controlled tenant enablement and
does not define another tenant-configuration store.

These reversible choices were accepted after repository-owner review. They do
not weaken a security requirement or an acceptance criterion.

## Decisions

1. **Provider profiles are strict, hashed startup configuration.** OpenAI,
   Anthropic, and Ollama ship as separate profile documents. Unknown fields,
   undeclared capabilities, unsafe base URLs, incomplete pricing, alias
   collisions, and unavailable policy targets fail before an adapter is used.
   Duplicate adapter-and-model identities also fail because the normative pin
   has no separate profile field with which to disambiguate them.
   A pin records the provider, resolved model, profile hash, registry version,
   and pricing snapshot so resume never silently follows a changed alias.
2. **Missing remote credentials fail at use, not at process startup.** The
   adapter registry installs a closed unavailable-provider implementation for
   an enabled remote profile with no credential. This keeps fake and local
   workflows usable without remote secrets while ensuring selection of that
   profile fails deterministically. Credentials remain `SecretStr` values and
   never enter a request, event, log, span, or export as metadata.
3. **Provider SDK types stop inside adapter modules.** Each adapter maps its
   wire stream into the normalized `ModelEvent` vocabulary. The runtime,
   domain, persistence mappers, and application services import no provider
   SDK. Recorded streams exercise the same provider contract without network
   access.
4. **Provider pins live on both the run and its checkpoint.** The run is the
   durable source when checkpoints are pruned; the checkpoint keeps the pin
   beside provider-only continuation state. A mismatch fails closed. Anthropic
   signed reasoning blocks and OpenAI response identifiers are checkpoint-only
   continuation data and never enter the event log or trajectory export.
5. **Provider metadata has exactly two named readers.** The protocol transports
   a closed metadata model without runtime branching. A persistence mapper
   flattens its scalar fields, and an observability helper maps the same fields
   to bounded span attributes. No generic JSON metadata column is introduced.
6. **The first artifact adapter is intentionally trajectory-only.** Milestone 3
   needs content-addressed, expiring artifact bytes before Milestone 6 owns the
   general streaming artifact store. A narrow local adapter implements only the
   trajectory port, derives storage keys from platform identifiers, and writes
   outside the source tree. It does not claim the Milestone 6 artifact API,
   sandbox export, upload, object-store streaming, or general authorization
   surface.
7. **Trajectory export remains disabled by default.** As a minimal bridge over
   the missing tenant-configuration mechanism, `Settings` gains
   `trajectory_export_enabled` and `artifact_root`, populated by
   `AGENT_TRAJECTORY_EXPORT_ENABLED` and `AGENT_ARTIFACT_ROOT`. The enablement
   parser accepts only `0` or `1`; no grant can override a disabled tenant. This
   is the one deliberate extension of the eight-field settings design and is
   was accepted as an explicit amendment rather than left implicit.
8. **Consent receives a narrow CLI surface.** `agent session export-consent
   grant|withdraw` manages the per-principal grant required by the export
   design. Grants are prospective, run creation stamps the combined operator
   and principal decision, and withdrawal expires historical exports for the
   ordinary maintenance sweeper.
9. **Recorded fixtures may hold one stream or a sequence of streams.** The
   single-stream shape remains supported, while a `streams` sequence permits a
   complete tool round trip to be replayed through the ordinary runtime without
   creating a separate test-only provider protocol.
10. **The required local live path is a deterministic loopback fixture.** The
    Ollama adapter contract is exercised over loopback with zero-price
    accounting and no credential. Credentialed OpenAI and Anthropic smoke tests
    remain explicit and optional, so the non-live suite is deterministic and
    secret-free.
11. **A single-price profile advertises only the range that price covers.** The
    first pricing model has one immutable rate per token class and no tier
    threshold. The OpenAI profile therefore caps its declared context at the
    272,000-token boundary covered by its configured snapshot instead of
    undercharging longer requests. Adding tiered pricing requires an explicit
    profile-schema extension and cost gate.
12. **Tenant redaction expressions use a conservative linear subset.** Pattern
    names must be non-empty, expressions are capped at 256 characters, and
    quantified groups, wildcard repetition, lookaround, backreferences, and
    conditionals are rejected at construction. This preserves useful literal
    and character-class rules while preventing operator configuration from
    turning untrusted export text into a regex denial of service.
13. **OpenAI wire constraints do not rewrite platform tool identity.** The
    Responses adapter derives deterministic provider-safe aliases for dotted
    tool names and reverses them on streamed calls. It sends function schemas
    in non-strict provider mode because platform schemas intentionally contain
    optional arguments and are validated centrally before execution. It omits
    the neutral `temperature` field because the configured GPT-5.6 Responses
    model rejects that sampling parameter. All three choices remain confined
    to the provider adapter.

## Consequences

- A new provider can be added through a profile and adapter without changing
  the runtime protocol. Multiple profiles may share an adapter only when their
  provider model identifiers are distinct.
- A run can resume after profile aliases change because the resolved identity
  and price snapshot are durable.
- The local trajectory store is a compatibility seam, not the future general
  artifact implementation. Milestone 6 must replace or adapt it behind the
  broader streaming port without changing existing export records.
- The maintenance sweeper currently sees only trajectory artifacts. Its query
  and byte-store dispatch must be generalized before other expiring artifact
  origins are introduced.
- The two added environment settings are documented in `.env.example`, default
  safe, and confined to composition. If a tenant settings repository is chosen
  later, these fields can be removed without changing the export service.
- Long-context and other tier-priced models must either declare the lower
  supported window or wait for a versioned tier-pricing schema; a flat estimate
  is never silently extrapolated.
- Provider wire aliases and schema leniency cannot bypass the registry, policy,
  or centralized argument validator because canonical names and arguments are
  restored before the adapter emits a normalized tool call.

## Review resolutions

The first complete local CodeRabbit review produced 25 findings. Twenty-one
were applied directly or through a stricter fix, including terminal stream
handling, native/XML tool separation, mid-stream transport failures, exact zero
rates, usage consistency, locked consent revalidation, best-effort expiry,
pattern validation, explicit repository failures, hashing typed YAML scalars,
and required pricing values.

Four suggestions were not applied because the accepted design fixes the
opposite behavior:

- `registry_version` retains the registry-wide hash. The model-gateway format
  requires a pin to identify both the chosen profile and the alternatives the
  router was choosing among; unrelated registry changes are intentionally
  visible.
- `ResolvedModel` and `ProviderPin` do not gain a profile field. Their normative
  shapes define `provider` as the adapter key, while the profile name is already
  the prefix of `registry_version`. The merged registry instead rejects a
  duplicate adapter-and-model identity before a pin can become ambiguous.
- A trajectory tool descriptor remains `{name, schema_sha256}`. Export schema
  version 1 explicitly fixes those two fields; adding `version` requires a
  schema revision rather than an undocumented producer-only key.
- OpenAI `previous_response_id` remains absent from requests under `store=False`.
  Stateless and zero-data-retention continuation replays the provider's
  reasoning item and encrypted content verbatim. The actual response id is
  carried beside that opaque item for closed telemetry, then removed from the
  provider input; the reasoning item's own `id` is never mislabeled as a
  response id.

The second full pass produced nine further findings, all applied. Most
materially, artifact expiry is now two-phase: the sweeper lists expired
metadata, deletes bytes, and only then removes the metadata record. A failed
byte deletion therefore remains discoverable and is retried on the next sweep.
The same pass hardened Anthropic SDK-error normalization, malformed integer
fields, provisional usage carry-forward, status-body retry classification and
gapless failure sequences, and removed stale-session reads from guarded
repository fallbacks.

## Alternatives considered

- **Fail startup when any enabled remote profile lacks a key:** rejected because
  it makes deterministic, local, and documentation workflows depend on remote
  credentials they never select.
- **Resolve model aliases again on resume:** rejected because it can move a run
  to a different provider, model revision, or price after a crash.
- **Persist arbitrary provider metadata JSON:** rejected because it creates an
  unbounded secret and cardinality surface and contradicts the closed-key
  design.
- **Wait for the Milestone 6 artifact store:** rejected because Milestone 3's
  normative export acceptance criteria require materialized, expiring bytes
  now.
- **Enable export whenever a principal grants consent:** rejected because it
  collapses operator governance and individual consent into one decision.
- **Exercise a developer's Ollama daemon in the blocking suite:** rejected
  because model availability and response text would make a hard gate depend on
  workstation state.
