# ADR-0232: Bounded owner-memory formation and recall repairs

- Status: Implementation decision under the owner's incident-repair request
- Date: 2026-10-21
- Related: ADR-0018, ADR-0019, ADR-0100, ADR-0178, ADR-0206, ADR-0223

## Problem

Synthetic reproductions confirm that a required model-chosen write scope can
misfile owner facts; sentence openers become query entities; long procedures
consume the snapshot; generic child mentions supersede named relationships;
and bare capitalized possessives form unsupported resource claims. A successful
bounded search does not currently identify its coverage in its rendered bytes.
These are mechanism findings, not measurements of production answer quality.

## Decision

New sessions advertise `memory.remember@2.1.0` with People enabled and `1.1.0`
otherwise. Scope is optional and resolved through the authenticated session,
as in ADR-0206. Explicit scopes retain their meaning. The three existing tool
versions keep their schemas and behavior. There is no automatic historical
rescoping: a project name alone cannot prove an old assignment was invented.

The existing personal-query anchors accept an explicit family follow-up and
retain both fact and attribute candidates for a residence question. Bounded
query-opener filtering removes false entities without constructing an owner
identity from display-name similarity. This adds no query-less overview arm.

Relevance-mode session snapshots count direct, durable, owner-authored facts
and relationships whose statements identify the user among their durable
items. They pack durable items first, admit only whole statements no larger
than 256 tokens or one third of the current budget. The existing durable
reservation still releases a slot when no durable candidate remains. Other retrieval moments keep their packing.
The trace records `+owner-core@1`; explicit legacy recent-mode queries preserve
their previous core selection. Existing surface, lifecycle, portability,
persona exclusion and erasure predicates remain mandatory.

Relevance-mode rendered memory identifies a successful, bounded read. Its
header counts against the existing token allowance, including empty results;
if even the empty header cannot fit, it is omitted. A successful empty result
does not establish that a fact is false, forgotten, or unavailable. This
metadata cannot guarantee that a model describes a result honestly.

Possession restatements use a deliberately narrow assertion key: the complete
object and all numbers remain, `has` and `owns` agree, and `new` immediately
before a number is nonessential. A bounded governed candidate query may find
the same assertion under a different subject **within the same session**.
It does not use evaluation-oriented subject similarity as an identity rule,
merge name-only people, or broaden matching across sessions. A general claim
that the owner has a child coexists with a named-child claim; it neither
supersedes that child nor corroborates the child's identity.

The deterministic possessive fallback requires a model number or concrete device
noun; ordinary affirmative ownership clauses remain available without that limit.
The People provider normalizer classifies direct owner possessions as attributes
through a current subclass outside the frozen distillation module; procedural
resources and the frozen formation@9 normalizer retain their classification.
The deterministic M16 control explicitly retains its old
possessive extraction so the checked-in baseline remains historical; current
compositions use the stricter fallback. No historical beliefs are automatically
retired or rewritten.

## Deliberately separate decisions

Deleting a replacement does not establish that its predecessor became true.
Automatic predecessor restoration could revive an owner correction, retraction,
or erased influence. Existing inactive predecessors remain inactive. Recovery
requires reviewing surviving evidence and obtaining a new explicit owner
assertion through governed formation, rather than changing status in SQL.

General cross-session near-duplicate reinforcement needs session-qualified
support for every contributing event and deletion behavior for that support.
The legacy single source-session field and session-local sequence union are
insufficient authority for a broader similarity merge. Yearless model names,
ambiguous people and materially different assertions remain distinct.

The separately retained unpublished People metadata and directed-relationship
repairs are dependencies to assess independently, not commits imported by this
repair. Global ranking changes, stemming, owner-overview intent and broad semantic
equivalence remain outside these mechanisms. No migration, production data write,
trial configuration change, timer change or benchmark run is part of this decision.

## Verification and release

Synthetic cases exercise overview context plus family, vehicle and residence
questions, write-scope defaults and pinned compatibility, formation and deletion
protections against both persistence adapters. Record the red and green outcomes
with the submission. Existing frozen measurements remain unchanged; their
signatures cannot be reused as evidence for changed runtime inputs. These tests
close only the reproduced mechanism defects, not M33 quality or release gates.

Production use still requires an authorized release and fresh compatibility and
trial admission assessment by the owner of that rollout. The active trial and its
fixed clock remain under their existing private review workflow.
