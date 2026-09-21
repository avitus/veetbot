# ADR-0108: The owner's introduction, honest answers, and approved outbound voicemail

- Status: Proposed; the owner chose decisions 1, 3 and 4 on 2026-09-19
- Date: 2026-09-19
- Related: ADR-0097 (amended by this decision), ADR-0017
- Detailed design: `docs/plan/bland-calling.md`

## Context

ADR-0097 and `bland-calling.md` required every call to open with AI identity
and transcription disclosure. The reviewed receptionist greeting was "Hi, I'm
Willow, Andy's assistant. I'm an AI assistant, and this call is transcribed and
shared with Andy. How can I help?" Outbound instructions required the same two
disclosures.

On 2026-09-17 and 2026-09-18 the owner edited the live inbound configuration in
Bland's dashboard. On 2026-09-19 a readback against the reviewed payload found
three differences:

- The greeting became "Hi, this is Willow, Andy's assistant."
- The prompt lost "You answer public calls as an AI assistant." and gained a
  blank line before the profile.
- A malformed fallback number, "+1", appeared.

Everything else matched: the capability-clearing fields, recording off, the
duration limit, the webhook and the voice. The owner confirmed the edits and
asked outbound calls to match.

The same day, the owner asked for a voicemail when an outbound call goes
unanswered. The 2026-09-18 no-answer test shows the current behavior:

- iPhone call screening answered with "If you record your name and reason for
  calling, I'll see if this person is available."
- Willow, already speaking, was cut off. She said "Goodbye" and ended the call
  after seven seconds.
- Bland reported `answered_by: "unknown"` and `call_ended_by: "ASSISTANT"`.

Bland's default voicemail action is to hang up. The dispatch allowlist in
`src/bland_mcp/client.py` rejected every voicemail field.

## Decisions

1. **The owner's introduction replaces proactive disclosure.** The inbound
   greeting is "Hi, this is {assistant}, {owner}'s assistant." Outbound
   instructions use the same introduction. Neither direction announces AI
   identity or transcription unprompted. The profile generator reproduces the
   owner's live prompt exactly, so the configuration readback can pass again.
2. **Asked directly, the assistant answers truthfully.** Both the inbound prompt
   and the outbound instructions tell the assistant to confirm, if asked, that it
   is an AI assistant and that the call is transcribed for the owner. Removing
   the announcement does not license a false answer. This safeguard was added
   during implementation, not chosen by the owner, so it is proposed here for the
   owner's confirmation.
3. **An approved outbound call may leave an approved voicemail.** `start_call`
   accepts an optional `voicemail_message`, bounded to 1,000 characters. The
   approval's argument fingerprint covers it, and the approval card shows it
   beside the brief, so the owner approves the exact words. Changing it
   afterwards voids the approval, as a changed recipient or brief does.
   - With a message, the provider receives `voicemail: {action: leave_message}`.
   - Without one, the provider receives `{action: hangup}`, so the choice is
     explicit rather than a provider default.
   - The SMS variants stay refused, because SMS is deferred scope for
     Milestone 27.
4. **Outbound calls let the recipient speak first.** Every outbound dispatch
   sets `wait_for_greeting`, which is the provider's fixed platform behavior and
   is not model-controlled. The recipient answers the phone, so the recipient
   speaks first, and the assistant's first words are then neither clipped at
   connection nor spoken over a person, voicemail greeting or screening prompt.
   The first live test showed exactly that failure. Inbound calls are unchanged,
   because there the assistant is the one answering.

## Consequences

- **Consent and disclosure risk.** Callers are no longer told unprompted that
  an AI is answering, or that the call is transcribed. Several jurisdictions,
  including all-party-consent states such as California, regulate recording or
  transcribing calls without notice. The owner accepted that trade-off; this
  record does not assess it legally.
- **Answers on request.** Decision 2 keeps a truthful answer available to any
  caller who asks.
- **No silence timeout.** Bland documents no timeout for `wait_for_greeting`. A
  person who answers and waits silently hears nothing until they speak, and the
  five-minute call limit still bounds the call. A screening prompt or voicemail
  greeting now reaches the provider before the assistant speaks.
- **No reliable voicemail label.** The provider reported a screened call as
  `answered_by: "unknown"`, so Veetbot cannot reliably mark an outbound call as
  voicemail. The transcript and summary still show what happened.
- **Live configuration must be re-applied.** It must match the new reviewed
  payload, which clears the fallback number and adds decision 2's instruction,
  before Milestone 27's configuration readback can pass.

## Alternatives considered

- **Keep the reviewed disclosures.** Rejected by the owner.
- **Leave a voicemail from a generated message.** Rejected: the owner would not
  see the words before they were spoken to a stranger's voicemail.
- **Speak first with a shorter greeting.** Rejected: it still talks over call
  screening and voicemail greetings, and a clipped opening still wastes the
  first words.
