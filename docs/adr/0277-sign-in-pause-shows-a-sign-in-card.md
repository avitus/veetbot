# ADR-0277: A sign-in pause shows a sign-in card, not a question

- Status: Accepted by the owner on 2026-10-31
- Date: 2026-10-31
- Amends: ADR-0164
- Related: ADR-0128, ADR-0251
- Scope: The existing browser capability and the Apple client. No milestone or
  gate moves, and no policy profile changes.
- Detailed design: `docs/plan/browser-automation.md` and
  `docs/plan/http-api-and-streaming.md`

## Context

When a website signs the owner out mid-task, the runtime suspends the run with
a server-authored `conversation.ask_user` call (ADR-0164). When the chat has a
selected website, the run continues by itself once the browser service verifies
a new sign-in, so the owner never needs to type an answer. The app could not
tell this pause from a question: it showed the generic "Clarifying question"
card with an answer field and an Answer button. On 2026-10-31 the owner reported
that a patient-portal sign-in request read as a question that needed an answer.

The only way to sign in was Website Access. On the Mac its globe button sat in
the chat's header, which scrolls with the transcript, so in a long chat the
owner had to scroll to the top of the thread to reach it.

## Decision

1. The `run.waiting_for_user` event's `suspension` carries `sign_in` when the
   question is the runtime's sign-in pause. It holds `browser_profile_id` and
   `origin` when the run waits for a verified sign-in to that saved website,
   and is an empty object when there is no such wait (the owner signs in and
   then replies). Model-authored questions never carry it. Neither field is
   page or model content: both come from the trusted wait binding.
2. The app shows a sign-in card in the attention dock in place of the question
   card. With a waiting website it names the website and offers **Sign in to
   <host>**, which opens the device sign-in for that saved login, and **Stop
   task**. It has no answer field: verified sign-in resumes the run. Without a
   waiting website it offers **Open Website Access** and **I've signed in**,
   which answers the question with fixed text, and **Stop task**. The
   transcript's waiting line says "Waiting for you to sign in."
3. Website Access sits with the chat's toolbar actions on every platform, so
   on the Mac it stays in the window toolbar instead of scrolling away.

## Consequences

- The owner signs in from the card at the point of the pause, with no scrolling
  and nothing to type.
- An app that predates this field still shows the question card, whose text
  already says what to do; the server change is additive.
- The question text the model and push notifications see is unchanged.
