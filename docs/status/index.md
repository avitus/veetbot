---
title: Project State
---

# Project state

The authoritative, machine-readable project state lives in
[`project-state.yaml`](project-state.yaml). Update that file (not this page) when
project status changes; **status changes require evidence**.

That record is the sole authoritative, machine-readable status surface for the
current milestone, authorization, completion state, and remaining work. This
page does not duplicate those mutable values. Evidence for **completed**
milestones lives in
[`verification-history.yaml`](verification-history.yaml); the in-progress
milestone's evidence stays in `project-state.yaml` until it completes.

The milestone titles in `project-state.yaml` mirror the canonical
[engineering plan](../plan/engineering-plan.md); keep the two synchronized.

The human-readable summary — every milestone grouped by state, with the open
work itemized for the in-progress ones — is the [milestones page](milestones.md).
It is a projection of `project-state.yaml`, and `make docs-check` fails when
the two disagree, so it cannot silently go stale.

The `readiness` block in that file is derived from the
[readiness review](../plan/readiness.md), which traces every milestone's work
items to the document that designs them. It records what the documentation
covers; it does not authorize work. The historical corpus audit passes are
preserved verbatim in the [corpus audit log](corpus-audit-log.md).

Autonomous decisions taken while the plan was being written, and the questions
they raise, are recorded in [questions for review](questions-for-review.md).
