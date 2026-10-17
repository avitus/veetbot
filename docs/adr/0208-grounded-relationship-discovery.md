# ADR-0208: Discover people through governed relationship evidence

- Status: Accepted implementation mechanism under the authorized M33 recovery; verification pending
- Date: 2026-10-15
- Related: ADR-0100, ADR-0178, ADR-0197

## Context

Name discovery can retrieve a person's linked facts, but a task referring to a
relationship can miss those facts even when the relationship and its person link
are stored. The partial development evaluation exposed this separation between
relationship evidence and linked interests. Adding a vocabulary of special car,
family or profession questions would not repair that missing connection.

## Decision

When relevance-based task recall has no explicit person selection or name match,
use the existing governed lexical candidate query to discover relationship
beliefs relevant to the task. This bounded identity-discovery query keeps the
owner, project visibility, sensitivity, temporal and lifecycle restrictions.
It does not change the final requested belief types or rank unrelated types as
relationships. Discard candidates with no whole-term overlap with the task.

Resolve those eligible belief IDs only through current visible, resolved,
direct owner-to-person relationship assertions. Apply relationship evidence,
endpoint and source restrictions in the repository before its result limit.
No arbitrary graph traversal, person creation, role inference or identity merge
is permitted. An empty candidate set returns ordinary recall; more than one
possible person abstains from person context rather than blending identities.
An existing ambiguous name never falls back to this discovery path.

One unambiguous person enters the existing People context service. Its linked
facts of every permitted type are candidates under the original query and its
hard filters. The same prepared encoder input, search allowance, twenty-item
limit, two-thousand-token ceiling and uncertainty rendering remain in force.
Identity discovery itself is local and adds no model request. Explicit name and
session selection retain precedence. Automatic and planned deliberate recall
must share this mechanism.

Resolve the runtime-bound semantic lookup before opening the People read
transaction, then pass that local handle into the fenced retrieval. Identity
narrowing reuses the prepared vector under its existing subset checks. It cannot
silently discard semantic retrieval merely because People context shares a
transaction, and it cannot perform another encoder call. Missing or revoked
runtime authority still yields ordinary sparse recall.

Retained deliberate results record the People and memory revisions captured
under the same owner fence, plus the relationship belief IDs that selected an
identity. The answer builder admits a saved result only while those revisions
still match and its bounded dependencies remain visible and valid. It checks
time-sensitive backing beliefs and relationship intervals, including discovery
edges omitted by rendering budgets. This adds no search, reranking or provider
call. Unknown legacy People receipts remain withheld. A mutation elsewhere in
the owner's directory conservatively invalidates the receipt.

## Verification

Red-first checks must cover several relationship roles with linked facts that
do not repeat the role word. Shared in-memory/PostgreSQL contracts must reject
unrelated, indirect, hidden, expired, unresolved or erased relationship evidence
before limiting candidates, and preserve dated reads. Ambiguous roles must not
combine profiles. Runtime checks must exercise the actual context/tool path and
shared budget. These mechanism checks do not replace complete confirmatory
answer-quality evidence or the original release gates.

The runtime evaluator must substitute semantic binding together with measured
recall and retained-result validation on the shared retriever. Leaving binding
on the original composition makes People reads lose the measured semantic
handle before their fenced transaction. Regression coverage must exercise both
automatic and reader-planned search through the actual reader and PostgreSQL
observation compiler, including dense and hybrid channels. Scripted answers in
these checks establish wiring only; they supply no release-quality evidence.
