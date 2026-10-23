# ADR-0238: Owner relationship overviews and complete memory delivery

- Status: Accepted by the owner on 2026-10-22
- Related: ADR-0100, ADR-0137, ADR-0195, ADR-0232

## Problem

An owner overview omitted existing direct family relationships. The People graph
held their directed endpoints and supporting beliefs, but keyword discovery did
not select them. A successful deliberate search could also exceed the generic
inline byte allowance: excerpting changed its bytes, so recall validation correctly
withheld the whole result. The production snapshot planner used the persisted
query's legacy default instead of the configured selection mode. Helper-level
snapshot checks did not observe that production entrypoint.

## Decision

Explicit owner and family overviews discover direct family and partner relationships
through the existing scoped People repository. They do not infer identity from a
name, traverse indirect edges, promote provisional beliefs, or alter formation.
Pages contain at most three focal people. The existing memory predicates govern
all supporting beliefs, including portability, sensitivity, lifecycle, exclusions,
source erasure and historical visibility. Each focal person contributes at most
100 relationship candidates; rendering remains subordinate to the shared item,
token and byte limits. Pages disclose bounded coverage and a
continuation when more people remain. Other relationship groups are deliberately
available through the same bounded read.

New sessions advertise `memory.search@2.2.0`, adding optional `relationship_group`
and `people_after` arguments. Older versions remain registered with their schemas.
Automatic overview recall uses the first family page; deliberate lookup can request
another group or page. Each page shares the existing twenty-item, two-thousand-token
allowance, requires `people.read`, and retains current People dependency validation.
A page is not a claim to enumerate all known facts about its people or the owner.
Prepared semantic input may be reused only with unchanged encoder text and no
widening of hard query restrictions; relationship pagination does not authorize
another provider call or activate a retrieval channel.

Deliberate memory rendering packs complete evidence within the configured serialized
inline byte limit as well as its token/item limits, before issuing a trace and
receipt. The generic tool pipeline and digest validation stay intact. Truncated
coverage remains explicit; a result cannot use the receipt for different bytes.

The production planner propagates the configured snapshot selection mode. Existing
frozen prefixes remain stable; new sessions and ordinary epoch rotations use the
configured mode. Frozen evaluation controls retain their explicit legacy mode.

## Verification and release

Regressions use composed session submission, real tool admission and final model
request assembly, with both persistence adapters. They cover multiple relatives,
directional endpoints, candidate floods, byte limits, continuation, changed
evidence, erasure and visibility. The PostgreSQL cases dispatch through the
composed durable worker; the reader boundary is the final model request. Synthetic facts replace private incident data.

No production memory rewrite, semantic-channel activation, trial renewal or milestone
gate change is included. Changed runtime inputs need fresh release compatibility
assessment; earlier comparison measurements are not recertified by these repairs.
