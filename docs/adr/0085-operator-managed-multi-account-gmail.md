# ADR-0085: Operator-managed multi-account Gmail

- Status: Proposed
- Date: 2026-09-03
- Related: Milestone 18 of the engineering plan; ADR-0021, ADR-0024,
  ADR-0044, ADR-0071
- Detailed design: `docs/plan/email-integration.md`

## Context

ADR-0071 deliberately authorized one Gmail mailbox. The production OAuth
ceremony subsequently proved a second need: the owner has both a personal
Gmail account and a Google Workspace Gmail account, and authorizing the OAuth
client in Workspace does not make the already-installed personal refresh
tokens address the work mailbox. Replacing the installed credential triplet
would switch mailboxes, not make both available.

The owner authorized multi-account support on 2026-09-03. This changes
Milestone 18's explicit single-account boundary, so it is recorded here and in
the engineering plan before implementation. The immediate requirement is
operator-managed accounts for the existing owner principal. A public product
in which arbitrary principals complete OAuth in the Veetbot application would
also require an HTTP authorization callback, per-principal encrypted token
storage, revocation, account lifecycle, and tenant-scoped consent state. Those
surfaces remain undesigned and are not smuggled into this operational change.

## Decisions

1. **Milestone 18 supports one or more operator-managed Gmail accounts for the
   configured principal.** `AGENT_EMAIL_ENABLED` remains the outer default-off
   switch. The existing three credential-file variables remain the complete
   single-account configuration and remain backward-compatible. A new
   `GMAIL_ACCOUNTS_FILE` selects multi-account configuration; using it together
   with any legacy Gmail credential-file variable is a configuration error.
2. **The accounts file is a versioned, non-secret manifest.** Version 1 names a
   `default_account` and one through eight accounts. Each account has a stable
   lowercase `account_id` and three absolute credential-file paths, one for
   each mode. Duplicate ids, an absent default, unknown fields, relative or
   insecure credential paths, unsupported versions, and more than eight
   accounts fail at startup. The manifest contains references, never OAuth
   material.
3. **Every account retains the three-process least-privilege split.** The
   default account uses the existing `gmail_read`, `gmail_write`, and
   `gmail_send` server ids so existing prompts and schedules keep working.
   Each additional account uses `gmail_{account_id}_read`,
   `gmail_{account_id}_write`, and `gmail_{account_id}_send`. Therefore a
   `work` read is `mcp.gmail_work_read.search_threads`, and every process still
   receives exactly one credential through `GMAIL_MCP_CREDENTIAL`.
4. **Multi-account credentials are bound to the operator account id.**
   `python -m gmail_mcp bootstrap --account-id work` writes `account_id` into
   all three credential documents. A server composed from the accounts
   manifest receives the non-secret id in `argv` and refuses a credential
   whose id is absent or different. The id is an operator routing label, not a
   claim that Veetbot independently verified the Google mailbox address; the
   operator must select the same Google account during all three consents.
   Legacy single-account documents without an id remain valid only on the
   legacy configuration path.
5. **Authorization remains server-id scoped.** Each synthesized server grants
   exactly `mcp.{server_id}.use` to the configured principal. Adding `work`
   therefore does not make a principal authorized for an unrelated account,
   and the existing approval, untrusted-output, retry, and uncertainty rules
   apply independently to every account-specific tool.
6. **Account selection is explicit in the advertised tool name.** There is no
   mutable active-account setting and no model-supplied account argument.
   Switching account changes the tool selected, so the proposed action and an
   approval record identify the mailbox routing label before execution.

## Consequences

- The current production single-account environment continues to compose the
  same three server ids and accepts its existing untagged credential files.
- Multi-account deployments create three isolated MCP connections per account,
  up to twenty-four connections at the eight-account bound. This spends more
  processes and prompt space in exchange for keeping credentials and failure
  ladders isolated.
- Renaming a non-default account changes tool names and therefore invalidates
  schedules or prompts that named the old tools. Account ids are durable
  operator configuration, not display labels.
- The bootstrap label detects copied or cross-wired credential files, but it
  cannot prove which Google identity the operator selected. The real-mailbox
  smoke remains the identity check.
- Public self-service OAuth, per-principal accounts, and a second mail provider
  remain future product work.

## Alternatives considered

- **One server triplet with an `account_id` tool argument:** rejected. It would
  place every account's credential in one child process and make a
  model-authored argument choose the credential after approval policy had
  resolved a generic tool.
- **Replacing the legacy tool names with account-qualified names:** rejected.
  It would break existing prompts and schedules for no security benefit.
- **Dynamic environment variables per account:** rejected. A versioned
  manifest is bounded and structurally validated; scanning an open-ended
  environment prefix makes spelling errors and partial triplets harder to
  reject coherently.
- **Public OAuth onboarding in this change:** deferred. It is a user identity
  and secret-lifecycle feature, not an extension of operator composition.
