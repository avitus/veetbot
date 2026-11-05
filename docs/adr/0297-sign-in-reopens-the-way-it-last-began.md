# ADR-0297: A saved login signs in again the way it last began

- Status: Accepted by the owner on 2026-11-04
- Date: 2026-11-04
- Amends: ADR-0128 (the device window opens first), ADR-0280 (the sign-in card),
  ADR-0288 (the remote browser only from inside that window)
- Related: ADR-0251, ADR-0287, ADR-0294
- Scope: one nullable column on browser profiles and its public field, the Apple
  client's choice of sign-in, the signed-out control's wait, and one runtime guard.
  No policy, scope, gate or milestone change.
- Detailed design: `docs/plan/browser-automation.md`

## Context

On 2026-11-04 the owner tried the Chase download of ADR-0294 in a new chat. Three
faults stopped it before the download could run.

1. **The wrong sign-in.** Chase had signed the saved login out, so Chat asked the
   owner to sign in. The card opens the device window, which Chase redirects to an
   unsupported-browser page (ADR-0287), although that login was saved through the
   remote browser. Nothing recorded how a login was signed in, and the remote browser
   was reachable only from inside the window.
2. **The crash.** The owner deleted that login and added it again. Deleting leaves
   each chat's binding in place, and the browser workflow step read the bound login
   without handling its absence, so every later message in the chat failed in half a
   second with `NotFoundError`.
3. **The sign-in that did not save.** The new login's checks ended
   `session_unconfirmed` twice: the copy with the session passed, and the copy without
   it showed no sign-in field on the same path. The check looks once after the page's
   requests go quiet. Chase draws its form in a frame, and on 2026-11-02 and 2026-11-03
   the same check passed, so a form drawn after that look is the suspected cause. The
   service recorded only the code, so this is inferred, not observed.

## Decision

1. **A profile remembers how its latest sign-in began.** `begin_authentication` writes
   `last_sign_in_mode`, in the unit of work that records the ceremony, so a failed begin
   records nothing. It is the latest begun rather than the latest to succeed, so a
   choice the owner just made holds while that sign-in is still failing. It moves neither
   the generation grants pin nor the update time. It is secret-free and public on the
   profile view, so every device of the owner agrees.
2. **The app reopens that way.** Sign in again, the chat's sign-in card and the
   website offer open the remote browser for a profile whose mode is `remote`, and the
   device window otherwise, as before. From a chat, Website Access opens with the launch
   link (ADR-0288 decision 4).
3. **The device window stays within reach.** A profile last signed in through the
   remote browser shows **Use this device** on its row, because a site can refuse a
   datacenter browser while accepting the owner's device. Beginning a sign-in there records
   `device`, and the app notes it at once, so the choice holds if that sign-in fails.
4. **The signed-out control may wait for a sign-in form.** Only the load without a
   session, only while it still reads as signed in, and only until five seconds before
   the check's thirty-second limit, or until it leaves the confirmed path. A hash change
   keeps the path. The same page still ends `session_unconfirmed` if no form appears.
   The wait alone could make two identical signed-out pages look different, so a pair
   passes only if the signed-in page has been watched for at least three seconds longer
   than the control's page was, each measured from its own load, because the two
   browsers can load at different speeds. A pair that would pass keeps looking at the
   signed-in copy until then, and a form or a move away there ends `session_signed_out`.
   A new document on the signed-in copy restarts its age, so time spent on the page before
   never counts. The control's age keeps counting across documents, so a move on its side
   cannot shorten what the signed-in copy must pass.
   That look must end with a reading of the page that worked: one that fails ends the check
   as `tool.browser.provider_unavailable`, which saves nothing, unless the page has left the
   confirmed path, which is already the move away.
   If the check's limit, less two seconds to read the session, leaves too little time, the
   sign-in ends `session_unconfirmed` and nothing is saved. All of this shares one limit,
   which starts with the check's first step. A configured site definition reads the page's
   own content and keeps its earlier check, with neither the wait nor the second look: a
   form found by waiting would excuse a control showing the same account markers.
5. **A remote rejection logs what each load saw.** Whether each load showed a challenge
   and stayed on the path, in yes or no, never an address. The device path keeps its
   rule that a verdict logs nothing (ADR-0128, amendment of 2026-10-06).
6. **A chat bound to a deleted login has no website.** The workflow step treats it as
   unbound, as the other readers of that binding do. A workflow already in progress still
   ends with its existing conflict. A follow request in such a chat is admitted without a
   follow, and the chat is not rebound to another login.

## Validation

Tests cover the deleted binding through a full run, the recorded mode through the
public API and the shared repository contract, the card, the row and the offer in the
Apple client, and the wait in real Chromium on a page that moves its hash and draws a
framed password field three seconds after going quiet. Contract tests also verify that a
late form or move on the signed-in copy is rejected for device sign-ins, and that a late
form is rejected for remote sign-ins. A real-Chromium device-handoff test covers the
late-form case. Chase has not been exercised.

The owner's production check is the one ADR-0294 names: in a new chat, ask for
recurring charges, sign in when asked, approve the Download clicks.

## Limits

- A login that existed before this has no mode, so its next sign-in opens the device
  window once. Choosing the remote browser there records it.
- If Chase's signed-out page shows no form at all within the wait, the save still fails.
  The new log line will then say so, and the next step is a site definition, not a
  longer wait.
- A check that ends without a sign-in form now takes about twenty-five seconds rather
  than failing as soon as the page goes quiet, and a sign-in that passes takes about
  three seconds longer.
- A page so slow that the signed-in copy cannot be watched for long enough is refused,
  even if the session was good. The owner can check again.
- A signed-in page that asks for a sign-in more than three seconds later than the
  signed-out page did can still pass. Before this work, a form the signed-in load
  finished too early to see could pass as well.
- Chase may sign a saved login out within hours, which this does not change.
- Deleting a login still leaves the chat's binding in place, so the chat shows no website
  until one is connected.
