# ADR-0300: Read card activity without per-click approval and capture an approved CSV export

- Status: Proposed; implementation requested by the owner on 2026-11-05. Acceptance awaits a successful review on Chase after delivery.
- Amends: ADR-0209, ADR-0263 and ADR-0294. No milestone, gate, compiled policy or tool schema changes.
- Detailed design: `docs/plan/browser-automation.md`

A card review in a signed-in chat asked for approval to open transaction activity,
select a date range, fill its dates and search. The first group of refusals cited a
non-browser tool: the agent had checked the clock. The next group cited a sensitive
path: Chase serves its activity and other views under the same authenticated
dashboard path. The individually approved final Download click completed without
a saved file or refusal. The available event does not establish what Chase sent.

1. The platform clock adds no outside information to a browser turn. Run
   bookkeeping still counts as a non-browser tool in general. An interactive
   principal's selected Chase profile may authorize a closed list of activity
   controls even after run bookkeeping, but never after a mail, memory, web or
   other outside read in the turn. This exception requires the exact HTTPS
   origin, dashboard path and recognized card summary or transactions fragment.
   The worker and isolated browser check the control again. Only activity
   navigation, a search, date-range choices and valid calendar dates in From
   and To textboxes qualify. Consequence, target, origin, field and provenance
   checks remain. No document navigation or ping is permitted during the act.
   Account changes, payments, exporting and unrecognized controls still require
   their ordinary authorization. This is not permission to browse any /auth path.
2. A final export control uses the existing individual approval and file window.
   Besides a native download, the browser can receive a CSV as a fetch or XHR
   response without a native download event. Only requests begun by the main
   frame during that window, on the approved origin, qualify. The observer
   never sends or repeats an HTTP request. It admits one successful response
   bearing a CSV media type or CSV attachment disposition; redirected requests,
   frames and other origins do not qualify. Chromium's response buffer and the
   decoded byte count are both bounded before reading the body. The same 2 MiB,
   text validation, session-scoped capture, untrusted label and reader apply.
   Native downloads keep their existing rules.
3. A final export waits within the act's existing deadline. If no file arrives,
   the result explicitly reports a timeout and tells the agent to inspect the
   page for validation, export limits or a ready-file link. When possible,
   continue with smaller ranges or the visible activity pages and report gaps.
   A submitted export is not blindly repeated. This cannot guarantee a site
   produces a file or authorize bypassing a popup, foreign host or sign-in.

Verification uses synthetic card views and real isolated Chromium, including
compressed CSV responses and oversize data. A live acceptance run must produce
a readable capture or accurately report incomplete coverage; selecting the login
and individually approving the actual file transfer remain owner actions.
