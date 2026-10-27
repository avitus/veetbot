# ADR-0260: Plain approval cards and an honest capture marker

- Status: Proposed. The owner authorized implementation on 2026-10-27;
  acceptance follows the production check in Validation. Two further changes
  were considered and declined (see Alternatives).
- Date: 2026-10-27
- Scope: Approval presentation, the approval response's `policy_reason`, and
  the model-facing marker on an artifactized tool result; no gate, milestone,
  policy profile or compiled `policy_version` changes
- Detailed design: `docs/plan/policy-and-approvals.md`,
  `docs/plan/http-api-and-streaming.md`, `docs/plan/tool-system.md`,
  `docs/plan/builtin-tools.md`

## Context

On 2026-10-27 the owner asked Chat to find Anthropic's S-1 and analyze its
growth and financials. The run raised three approvals, and the owner approved
each without being able to tell what was being asked. All three were
`sandbox.run_command` calls that only listed directories: `ls -la /mnt/data`,
then `ls /` with a `find` for `*web-fetch*`, then a `find` under
`/workspace/.agent` and `/opt/agent`.

Two separate defects produced that experience.

1. **The card said nothing.** Every tool without its own presenter gets the
   headline "Run <tool> with validated arguments." Only browser, schedule,
   records, unsubscribe, Cockpit, skill and device tools have presenters, so
   sandbox commands, Gmail and other MCP writes, media generation and workspace
   writes all show it. Beneath it came the registry name, "High risk", an
   expiry and raw JSON. `http-api-and-streaming.md` already defines
   `policy_reason` as a human-readable statement of why approval was required,
   but the implementation returned the reason code
   (`policy.matrix.code_execution`), and the Apple card did not show it. The
   code was misleading as well: the matrix allows isolated code execution, and
   these commands asked only because the turn had read web pages, which the
   trust overlay escalates. Nothing recorded that the overlay was the cause.
2. **The marker sent the model looking for a file it could not open.** The
   524 KB filing was admitted as a 3 KB head-and-tail excerpt whose marker read
   `full output: artifact:<id>`, followed by a reference to
   `web-fetch-output.json`. `tool-system.md` states that no model-callable tool
   reads a capture back, but the marker did not say so. The model searched the
   sandbox for the file three times. Each search needed an approval and none
   could have succeeded.

## Decision

1. **A headline for every tool.** A tool without a presenter gets a headline
   from a closed table keyed on its side-effect class: "Send a message with
   <tool>", "Make a change outside Veetbot with <tool>", "Delete something
   outside Veetbot with <tool>", "Spend money with <tool>", "Publish something
   with <tool>", "Run code in Veetbot's sandbox with <tool>", "Write a file in
   this chat's workspace with <tool>", and "Use <tool>" otherwise. A presenter
   that fails falls back to the same headline.
2. **`sandbox.run_command` names its command.** Its presenter's headline is
   "Run a command in Veetbot's sandbox: <command>". A `sh -c` (or `bash`,
   `zsh`, `dash`; `-c`, `-lc`, `-cl`) invocation shows its script. Each
   argument passes through the approval argument view's redaction first, so the
   headline never shows what the arguments view masks. Whitespace collapses to
   one line, and the headline is cut to 160 characters. The arguments the card
   carries are unchanged.
3. **The decision records an overlay escalation.** `PolicyDecision` gains
   `untrusted_escalation`, true exactly when the trust overlay raised an allow
   to approval. Stored decisions without it read as false. Reason codes,
   decisions and the policy version are unchanged.
4. **`policy_reason` is a sentence.** When it creates an approval, the
   pipeline stores `policy_reason_text`, taken from a closed table and never
   from model text:
   - An advisory escalation: "A second safety check flagged this request, so
     Veetbot asks first."
   - An overlay escalation: "This chat has read web pages or other outside
     content, so Veetbot asks before running code." (`writing files` for
     workspace writes, `taking this action` otherwise.)
   - A class whose own row asks: "Veetbot asks before sending a message for
     you.", "… changing anything outside Veetbot.", "… deleting anything
     outside Veetbot.", "… spending money." or "… publishing anything."
   - Anything else: "Veetbot's safety rules ask for your approval on this
     action."

   The approval response returns this sentence as `policy_reason`. An approval
   stored before this change returns the class sentence its stored
   `policy.matrix.<class>` code maps to, or the fallback. The stored code stays
   on the record for diagnosis, and responses still never carry the rule that
   fired.
5. **The Apple card leads with what and why.** The generic card shows the
   headline, then the reason sentence, then the denial reason and the
   decision buttons. A Details control below them reveals the registry name,
   risk, expiry and arguments; it starts collapsed while the approval is
   pending. A settled card shows its details directly. A `policy_reason` with
   no spaces (an older server's code) shows no reason line. The `browser.act`
   card (ADR-0129) is unchanged.
6. **The capture marker says it cannot be followed.** An artifactized tool
   result's marker now reads `[... N bytes elided; full output: artifact:<id>,
   kept for the owner; no tool, sandbox or workspace can open it ...]`. The
   excerpt still fits the same inline budget. The history excerpt that context
   assembly makes for sessions older than admission-time excerpts keeps its
   existing `event:<sequence>` marker, so those sessions' assembled history
   does not change.

## Consequences

- The owner can read an approval at a glance, and push and lock-screen
  payloads still carry only a templated title and the registry tool name.
  Telegram's "Approval needed" text and the Cockpit approval feed show the
  clearer headline automatically.
- The reason sentence describes the decision, not the model's intent. A
  model-written statement of purpose would be more specific, but text an
  untrusted page could influence would then sit beside the Approve button.
- Approval records gain one optional field and decisions one boolean. No
  migration is needed, because both live in JSON documents and default when
  absent.
- The marker change costs about 70 bytes of excerpt within the 4,096-byte
  inline budget.

## Alternatives

- **Let isolated sandbox commands run without asking after web reads.** The
  owner declined on 2026-10-27. It would loosen the trust overlay and change
  the compiled policy version, which memory-formation evidence is bound to.
- **A model-written purpose line on the card.** The owner declined on
  2026-10-27, for the injection reason above.
- **Let the model read a long capture in sections.** Not part of this change.
  It would let a large filing be read rather than excerpted, but it is a new
  tool capability that needs its own design.

## Validation

Red tests precede each change: the overlay flag for an isolated command after
untrusted content and for a class that asks anyway, older stored decisions
reading as not escalated, every reason sentence and headline, the sandbox
headline for a plain command, a shell script, a credential-shaped argument and
a long command, the API returning the sentence for new and older approvals,
the capture marker, the fallback headline in the Milestone 4 presenter-failure
gate, and the Apple `ownerReason` rule. `make check` on the sidecar, the Swift
package suite and the macOS approval UI cases gate delivery.

Production acceptance: after delivery, a Chat that reads a web page and then
runs a sandbox command shows the command in the headline and the
outside-content sentence beneath it. A Chat whose fetch is excerpted does not
search the sandbox for the capture.
