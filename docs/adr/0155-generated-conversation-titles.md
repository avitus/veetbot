# ADR-0155: A model titles chat conversations and retitles them when the subject moves

- Status: Accepted (authorized by the repository owner, 2026-10-07; amends
  the first-writer-wins title rule of `docs/plan/http-api-and-streaming.md`)
- Date: 2026-10-07
- Related: Section 16 of the engineering plan; ADR-0050, ADR-0102, ADR-0110
- Detailed design: `docs/plan/conversation-titles.md`

## Context

A chat conversation's title is the first 64 characters of its first message,
written once and never replaced (ADR-0050 made it server-owned; the HTTP
specification made it first-writer-wins). Opening questions rarely name their
subject, and a conversation that moves on keeps its first words, so the
sidebar is a poor index. The owner asked for titles that are set after the
first reply and refreshed as the conversation moves on, refreshed only when
the subject clearly changes, and written from the owner's own messages only.

## Decisions

1. **A model writes the title, on the server.** The generated title replaces
   the first-message title in the existing `sessions.title` field, so every
   client shows it through the unchanged `SessionView`. No route, scope, wire
   field or environment flag is added.
2. **Only first-message titles are regenerated.** A new internal
   `title_source` (`first_message`, `generated`, `fixed`) records where a
   title came from. Titles set at creation by other features (email subjects,
   schedule names, delegated objectives, fixed background names) are `fixed`
   and never touched. A migration backfills the field from the metadata keys
   those features write.
3. **A completed reply leaves a durable, coalescing request.** The worker's
   post-run step sets `sessions.title_requested_at` for an eligible top-level
   session. The maintenance role answers requests on its existing loop. No
   model call runs on the run's worker, so a title never delays the next run.
4. **The model reads only the owner's words.** The first and latest few
   owner-authored messages (text and attachment names, capped), after the
   folder grouper's secret and injection scans. Assistant, tool and other
   actors' text, and session metadata, are never sent.
5. **Titles stay stable.** After the first titling, the model keeps the
   current title unless the latest messages have clearly moved to another
   subject.
6. **Every failure is inert.** A model error, budget breach or rejected
   answer leaves the title unchanged and clears the request; the next reply
   asks again. Writes are compare-and-set and never touch `updated_at`.
7. **The cost is bounded and visible.** One structured call per request under
   fixed budgets (USD 0.05 and 20 seconds per call) on the `balanced` policy,
   recorded on a content-free `session.title.checked` process event and
   charged to no run.

## Consequences

- Sidebar titles name the subject and follow it when it changes, typically
  5–15 seconds after a reply. Their sidebar order is unchanged.
- Each completed chat reply costs one small model call (about a tenth of a
  cent, estimate).
- Folder grouping reads better titles; open proposals are unaffected because
  they key on member ids.
- A conversation's text reaches the title model, the same provider that
  already answers it; the content-free audit and the inert failure path keep
  titles out of logs.
- Hand renaming remains unbuilt; if it is added, it must pin the title by
  setting a source the pass never regenerates.

## Alternatives considered

- **Call the model in the run's worker right after the reply.** Slightly
  faster, but production runs one job per lane, so the next queued message
  would wait on the title, and a worker crash would lose the request.
- **Piggyback on memory formation.** Formation waits for the conversation to
  go idle, so titles would lag by minutes.
- **Retitle after every reply, or at fixed reply counts.** The first makes
  titles churn and harder to find; the second lags behind a topic change.
- **Send the assistant's replies too.** More specific titles, but replies
  quote websites and email, so untrusted text could steer the title.
