# ADR-0319: Outbound calls run a pinned provider agent version, up to fifteen minutes

- Status: Proposed under the owner's 2026-11-13 instruction
- Date: 2026-11-13
- Amends: ADR-0097 (Bland calling and public receptionist), ADR-0108 (owner introduction)
- Detailed design: `docs/plan/bland-calling.md`

## Context

The owner rebuilt Willow as a Bland V2 agent: a versioned graph that takes an
inbound message or, given a trusted call mode and objective, makes an outbound
inquiry. Veetbot still dispatched every outbound call with its own prompt and
voice, so the agent's outbound lane could never run, and capped every call at
five minutes.

On 2026-11-13 the owner directed four changes: a fifteen-minute call length,
post-call delivery to Veetbot for the agent's calls, the plain introduction
"Hi, this is Willow, Andy's assistant." on outbound calls with no unprompted
AI announcement, and the agent's outbound mode enabled.

## Decision

The reviewed call configuration gains an optional pair, `agent_id` and
`agent_version`. Both are present or both absent. `agent_version` is a minted
semantic version; `production`, `staging`, `dev`, `latest` and branch
selectors are refused, so a later publish at the provider cannot change what
an approved call says. Both values are part of the configuration revision
that binds outbound approval. A file without the pair keeps its earlier
revision and the earlier prompt-based dispatch, which is the rollback.

With a pin, dispatch sends the recipient, caller ID, the agent pin, the
duration, recording off, the callback URL, the correlation metadata, the
explicit voicemail action and `request_data` holding exactly `call_mode`
(`outbound`), `call_objective` (the approved brief) and `call_context` (the
approved disclosed facts). It sends no prompt, voice, tools, transfers or
greeting-order field: the pinned version owns them. The client refuses any
other field before the network.

The call ceiling rises from five to fifteen minutes for both dispatch forms.
The configured `max_duration_minutes` still defaults to five and still bounds
each call; raising it is a reviewed configuration change with a new revision.

The agent has no post-call setting of its own. Result delivery stays where it
was: the number's callback for inbound calls and the per-call `webhook` for
outbound calls. No new ingress path, role or signature rule is added.

The pinned version is operator-reviewed before its number is written to the
file. The review confirms the owner's introduction, a truthful answer when
asked about AI identity or transcription, recipient-speaks-first, recording
off, no tools, no transfers, no memory and no knowledge sources.

The review also confirms that the version references no provider environment
variable (`{{env.KEY}}`). The provider resolves those from its development
environment at call time for a version-numbered call, outside the version and
therefore outside the approved revision. A version that references one must
not be pinned. Veetbot holds no provider administration read, so it cannot
check this at dispatch; the rule is part of the operator review. The only
variables a pinned version may read are the three `request_data` keys Veetbot
sends with each call.

## Consequences

- The boundary that the call agent receives no tools moves from fields Veetbot
  sends on every call to a reviewed, immutable provider version named by the
  approved revision. Veetbot cannot verify the version's contents at dispatch;
  the operator review is the control, as it already is for the inbound number.
- A call may now last and bill up to fifteen minutes. One concurrent outbound
  call remains the limit.
- Publishing a new agent version changes nothing until the operator pins it,
  which invalidates pending approvals.
- Attaching the number to the agent replaces the prompt-based inbound profile.
  The setup guide's readback then compares the number's callback, recording
  and transfer fields and the pinned version, not the generated prompt.
- Live voice behavior is unverified until one inbound and one owner-authorized
  outbound call are made after activation.
