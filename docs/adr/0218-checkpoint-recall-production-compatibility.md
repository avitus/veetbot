# ADR-0218: Release-specific compatibility for checkpointed recall

- Status: Accepted scope under the owner-approved bounded rollout; verification pending
- Date: 2026-10-18
- Related: ADR-0178, ADR-0199, ADR-0212, ADR-0214, ADR-0215
- Scope: The completed checkpoint comparison and one explicitly verified release

## Problem

The owner authorized completing the recall release, opening its pull request when
ready, and following it through production deployment. The separately evaluated
runtime remains pinned to `16153a5f`, with the approved formation repair recorded
under its own identity. Production has meanwhile advanced to `9456e67c` without
waiting for this isolated evaluation, as the owner directed.

The complete ADR-0199 input census finds 109 changed included files between those
commits, including 29 application source files. These are real differences, not
just a merge commit label. In particular, retained-recall validation now checks
semantic authority only for semantic receipts and withholds unavailable source
recovery. The run loop reserves time for synthesis and rechecks resumed tool
admission. Browser and calling composition also changed. The proposed release
adds the separately verified checkpoint collector and formation deadline repair.

ADR-0199 currently requires new matching measurements for any changed evaluated
input. The checkpoint input-binding implementation correctly refuses this target;
its publication also explicitly grants no activation authority. Passing the
current comparison cannot establish complete byte equality with production.

## Bounded compatibility exception

The owner authorized the compatibility route on 2026-10-19, alongside the
release-specific acceptance revision in ADR-0220. Admit the completed 1,600-row comparison through
a separately versioned compatibility receipt for one final release tree. Preserve
the existing exact-input route and its refusal tests unchanged. This exception
must never masquerade as an ADR-0199 equality receipt.

The new route requires all of the following before activation:

1. Retain and mechanically reconstruct every original observation, bank, event,
   segment and charge. Preserve the original identities and separately named
   formation repair. Complete all 400 scenarios and both paths without favorable
   resampling, unknown-work retries or invented timing or usage.
2. Apply the explicitly revised bounded-rollout criteria in ADR-0220 while
   retaining every original numerical result and failed gate. Obtain a
   genuine independent human review of difficult answers, interruptions, repairs
   and uncertain costs, including explicit residual-risk findings. No human
   findings or successful compatibility assessment are implied by scope approval.
3. Inventory every included file in the measured, collector and final release
   trees. Enumerate every changed file and its exact hashes, behavior disposition
   and verification evidence. No caller-supplied wildcard, ignored source subtree,
   aggregate pass flag or inherited branch name establishes compatibility.
4. Verify the production-shaped effective configuration, model and embedding
   identities, schema, formation requests, context limits and recall budgets.
   Keep the actual runtime changes visible. Specifically exercise retained recall,
   semantic-authority failures, source-recovery failures, synthesis time reserves,
   resumed approval handling, and unchanged successful reader/tool requests.
   Any material model-visible or policy difference not established compatible by
   these checks requires matching additional measurements before admission.
5. Bind the accepted compatibility assessment and owner decision to the exact
   final reviewed source, publication bytes and actual deployment tree. Recheck
   source and configuration at deployment, and revoke admission on mutation.
   A later release cannot inherit this exception merely because it descends from
   the admitted commit. Complete CodeRabbit, final-head CI and ordinary production
   delivery verification for this release.

This is an authorized change to the evidence-transfer rule, not an implementation or
a claim that compatibility is already established. The existing owner instruction
to ship does not silently turn changed inputs into identical inputs. The `dev`
push hold and unrelated production work remain unaffected.

## Alternative

Keep ADR-0199 unchanged and measure a complete new comparison against the final
integrated runtime, retaining the current completed evidence separately. This
avoids the compatibility exception but requires another full collection and human
review. It cannot be represented as merely continuing the current measurement.
