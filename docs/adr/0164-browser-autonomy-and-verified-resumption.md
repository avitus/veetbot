# ADR-0164 — Browser autonomy and verified resumption

- Status: Accepted under the owner's implementation instruction
- Date: 2026-10-06
- Amends: ADR-0159 completion predicates and ADR-0163 authentication suspension
- Scope: The minimal-user-involvement tranche of ADR-0156

## Decision

Routine recovery, verification and continuation use the existing run and tool
pipeline. They do not create authority or a second effect ledger. User input is
reserved for missing intent, required authentication and uncovered actions.

1. Extend bounded visible postconditions with positive rendered-text, semantic
   region, exact-location and typed collection-row evidence. An optional failure
   predicate reports an observed failure; conflicting success/failure evidence
   stays ambiguous. Legacy control predicates remain valid. All checks share
   the existing deadline and read-count budget. Evidence describes the observed
   window; neither a missing marker nor a schema-valid row proves an action was
   unsent or that a website is truthful. Reconciliation never resends a write.
2. Use trusted, versioned declarative site verification definitions in the
   isolated browser service. Require positive protected-state evidence, retain
   challenge detection, bind account evidence to the sealed profile, and reuse
   valid sessions. Unknown sites retain bounded protected-page differential
   verification; cookie presence is not proof. Definitions contain no scripts,
   credentials or permission exemptions and are maintained by the implementation.
3. Persist a browser-authentication wait with the run's trusted profile binding,
   question and generation. Resolve it only from the isolated service's verified
   ceremony outcome for that principal/profile, recorded durably by the
   application. Resolve and requeue atomically, idempotently, without fabricating
   a user message. A bounded maintenance reconciliation covers missed deliveries
   and worker restarts. Cancellation, deadlines, limits, newer ceremonies and
   profile revocation fence the continuation. Resume with a fresh lease and read;
   old actions and old generation grants are never replayed or transferred.
4. Consolidate repeated notices of one authentication episode. Ordinary recovery
   stays in the passive activity record. Relevant semantic progress, rather than
   revision churn or unrelated page changes, bounds repeated attempts. Preserve
   an explicit partial/uncertain result when recovery cannot make progress.
5. Reviewed workflows select fresh targets from bounded evidence and propose
   one ordinary tool call at a time. Every action retains policy, approval,
   cancellation, budget and effect accounting. Recipes carry expected states,
   not live references or previous approvals. Persist verified step progress;
   ambiguous outcomes return to observation instead of replay. Page-authored
   text cannot install or change a trusted recipe.

Site-specific definitions and recipes ship disabled until reviewed fixtures
justify each entry. The initial catalogs are empty; controlled report/account
fixtures exercise selection, saved-session checks, approval, response loss and
continuation. No automatic learning or promotion is included. Workflow selection
uses exact normalized owner-intent phrases and an already selected origin;
ambiguous matches use the ordinary agent. Each recipe is limited to 16 steps,
64 calls and 15 minutes inside the existing run budget. Completion or uncertainty
forces a tool-free final report.

No provider dependency, model credential access, automatic CAPTCHA/MFA handling,
new browser origin authority or comparative benchmark is introduced. Existing
native sign-in is the user interaction; a second chat acknowledgement is not
required on the verified automatic path. Legacy manual continuation remains
compatible while still requiring current evidence and authority.

Integration with ADRs 0145 and 0151 preserves headed execution, the three-browser
admission limit and all published pinned tool versions. Sign-in and differential
control loads explicitly permit login-page reads independently of launch mode;
agent leases retain interruption checks. Remote verification reserves its two
temporary loads until cleanup finishes. Surface polling does not hold admission
while waiting for verification's operation lock.

## Verification

Begin with failing boundary and real-Chromium regressions for richer evidence,
false completion, authentication event replay, wrong profiles/accounts,
cancellation and stale generations. Assert zero additional prompts for authorized
routine work and exactly one interruption per unresolved sign-in episode, while
requiring eventual completion or an explicit bounded stop. Server-side counters
must show no duplicate effects after response loss or resumption. Run the full
sidecar gate and relevant native and persistence checks on the final inputs.
