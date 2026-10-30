# ADR-0270: One approval for a widget installation

- Status: Accepted for implementation on 2026-10-30 by the owner's explicit
  instruction: "I want a single, summarized approval at the end."
- Amends: ADR-0240 and ADR-0268 for the combined widget installation workflow.
- Scope: The owner-requested repair to widget creation. No milestone moves.

## Context

A real widget-authoring session asked the owner to approve reads, repeated
trials, publication, access and scheduling separately. The owner approved more
than ten times before receiving a usable widget. This is not the intended MVP.

## Decision

Preparation and installation are separate. In interactive owner chat,
`cockpit.prepare` checks a complete widget package and, when needed, its Python
collector, private access and interval schedule without publishing or granting
anything. The successful tool invocation is the immutable private proposal.
The agent can read existing jobs and run offline trials or trials under an
existing owned grant without another approval. Trial feeds remain previews;
new hosts, credentials and model budgets cannot be exercised before approval.
These narrow preparation permissions apply even after reading untrusted data;
they never override a policy denial or apply to unattended runs.

`cockpit.install` references that exact successful preparation. Its one approval
card summarizes the widget, public code publication, private inputs, hosts and
credential references, feeds, per-run limits and refresh interval. Source files
are bound through the immutable preparation, not replaced by model-written
summary text. Approval installs the prepared widget and optional collector,
creates the exact private grant and the job schedule. Scheduled collection
starts at the next interval boundary; there is no hidden initial model run.
Widget-only installation is also supported. Private layout/settings tools
remain available without additional publication approval.

The combined tool calls the existing publication and scheduling services. It
retains privacy checks, commit-before-activate, optimistic revisions, owner and
session boundaries, sandbox limits and operator restrictions. Partial
publication is reported honestly. Repeating the same approved proposal resumes
its idempotent component operations without another approval; changing the
proposal requires preparing and approving a new one. A denial still blocks it.
This is one concrete installation authorization, not a general task-wide grant.
Existing individual tools retain their mutation rules for other workflows.

The owner explicitly authorized this replacement of per-step approvals. No
blanket suppression of the trust overlay or general approval framework is
introduced. External service installation remains an operator prerequisite.

## Verification

Required evidence: preparation causes no publication, grant or schedule;
reads and bounded trials do not prompt; installation prompts exactly once;
all summarized fields are bound; cross-owner/session proposals and altered or
unapproved installs fail; repeated installation creates no duplicate components;
invalid packages return repair feedback before approval; explicit denials hold.
Production verification follows separately authorized delivery.

Implementation evidence is in `tests/unit/test_cockpit_installation.py` and
`tests/unit/test_job_authorization.py`. The first regression run reproduced the
read/trial approval loop and missing combined tools (five expected failures).
A malformed-feed regression separately reproduced missing repair feedback.
The composed tests cover private preparation after job reads and trials, one
approval, idempotent continuation, denied/foreign/changed proposals, code-only
schedule limits and validation diagnostics. Focused job/workflow tests pass,
as do the contract partition and PostgreSQL approval, publication, job-store
and process-event checks. Reading lane A; no milestone gate is advanced.
Production verification remains pending delivery.
