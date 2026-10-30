# ADR-0272: Jobs and semantic recall coexist

- Status: Accepted for implementation on 2026-10-30 by the owner's request to
  keep jobs running, repair coexistence and restore semantic recall.
- Amends: ADR-0268's recall-release restriction. ADR-0269's sparse startup
  fallback and all exact semantic admission requirements remain in force.
- Scope: Existing jobs and M33; no milestone or quality gate moves.

## Decision

Jobs do not need to be disabled merely because owner-interactive semantic
recall is admitted. Admission must bind the actual jobs-enabled configuration,
including the registry and effective operator host restrictions. Scheduled jobs
retain their pinned code and access and execute outside the agent loop; they
receive no implicit semantic retrieval or model call. An explicit job model
operation remains within its approved budget. Invalid semantic admission falls
back to sparse recall without disabling jobs or their schedules.

This replaces the blanket jobs-off condition only after the following repairs
are delivered and the exact final configuration is assessed. This document is
not itself admission, owner review, or production verification.

### Public source

The owner explicitly directed that there be no human pushes to `cockpit/jobs`.
An active repository ruleset restricts creation and updates of that exact
branch to the existing publishing App, with no human or repository-role bypass.
Separate no-force-push and no-deletion rules retain their protections. Human
edits use the same private authoring/screening path as agent edits. Import
screening remains defense in depth; it cannot undo disclosure in a public push.
Administrators can change repository rules, so rule readback is part of the
operational assessment rather than a claim that administrators lack authority.

Publication screens proposed private inputs before the first grant exists,
in addition to known owner/People names, current settings and stored grant
inputs. Widget installation passes its approved inputs through that same
boundary. The outbox screens again against current private context immediately
before a new public write. A refusal rejects the queued revision permanently;
maintenance cannot publish it later merely because the private context changed.
A corrected package needs a new revision. Archiving screens only its new commit
message, so a newly private value in old source cannot prevent its removal. Existing screening remains bounded
literal/credential-pattern screening, not a proof that arbitrary personal prose
or arbitrary transformations can be recognized.

### Execution authority

A new external grant is bound to its immutable revision and approval using
`uuid5(approval_id, "job-revision:" + str(revision_id))`. The UUID is a binding
identifier, not a secret or a substitute for authorization. Creation still
requires the owned approved action's exact normalized arguments. No stored
record field or database migration is added, preserving older readers.

Standing authorization reuses network, model or feed access only for that exact
revision. Reusing an old unbound grant or choosing another revision requires
fresh execution approval and creates a new grant; prior grants are retained.
A live trial receives standing authority only when its source, manifest and
private inputs match the bound revision/grant. Publication's default allow
cannot approve live trial execution. Pure local computation retains its existing
rules. Already approved schedules retain their exact code/grant pair; this
change does not silently alter their pins or stop them.
New schedules and changed code/grant pairs require revision-bound external
access at the schedule service boundary. Edits retaining an already approved
pair preserve legacy authority. Re-approval cards resolve stored grants and show
redacted inputs, hosts and credential bindings, feeds, model budget and limits;
an unresolved grant is explicitly unknown, not an empty access proposal.

### Credential responses

The HTTP broker continues to remove literal credential echoes from JSON keys
and values. It refuses responses containing standard or URL-safe base64,
case-insensitive hexadecimal, or fully percent-encoded credentials before returning JSON to the
job. Refusal carries a fixed reason without response or credential text.
These are explicitly supported encodings, not protection against arbitrary
adversarial transformations. Credential hosts remain trusted principals;
operator host restrictions, public HTTPS, DNS checks, no redirects, response
bounds and the sandbox broker boundary remain unchanged.

## Verification and release

Focused regressions reproduce encoded echoes, cross-revision standing authority,
live-trial publication approval, inherited feed authority, missing proposed-input
screening and delayed publication screening. Composition tests cover jobs with
admitted indexing and with semantic startup fallback; these use scripted
providers and do not establish production recall quality. Existing schedule
and explicit-model usage tests remain required.
Review regressions also reproduce an unbound grant accepted during schedule
conversion and missing stored access on re-approval cards. Coverage preserves
existing schedule pairs while rejecting new legacy or cross-revision pins,
and verifies owned, missing and foreign grant presentations without secret text.

Before restoration: finish review and final-head hosted CI, verify delivery,
refresh exact source/configuration/schema/assets/rollback evidence with jobs on,
read back branch restrictions, then install a distinct current admission under
the already authorized restart procedure. Preserve the original trial start,
absolute end, previous expiry and all withdrawal evidence. Scheduled/delegated
semantic recall and unattended reconsolidation remain disabled.
