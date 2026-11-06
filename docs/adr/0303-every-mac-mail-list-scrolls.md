# ADR-0303: Every Mac mail list scrolls

- Status: Accepted (chosen by the repository owner, 2026-11-06)
- Date: 2026-11-06
- Related: ADR-0231, ADR-0234; Milestone 26
- Amends: ADR-0231's five-row Inbox pagination, on macOS only
- Detailed design: `docs/plan/email-experience.md`

## Context

The Mac Important list (pull request 240) loads up to 100 threads, shows
the total, and fetches the next page as the owner scrolls near the end. Email
opens on Inbox, though, which ADR-0231 kept at five rows per page behind a
More mail button, as are Needs you, Handled, Other mail and search results.
Important itself sits behind Find other mail. After pull request 240 shipped,
the owner looked at the list Email opens on, saw five emails, and asked why
the scrolling change was missing. Told that only Important scrolled, the owner
chose "all Mac lists".

## Decision

1. **On macOS, every mail list scrolls the way Important does.** Inbox, Needs
   you, Handled, Important, Other mail and search results each load up to
   100 threads per page and fetch the next page automatically when the owner
   scrolls near the end. No More mail button appears on the Mac.
2. **Refresh keeps the visible thread in every list.** The refresh that keeps
   the loaded rows and retains the visible thread, which pull request 240
   built for Important, applies to every Mac list. The rule that stops chasing
   a thread already confirmed handled, archived or in Spam stays Important's,
   because only Important drops such threads; the other lists may still show
   them.
3. **Touch is unchanged.** iPhone and iPad keep ADR-0231's five-row Inbox and
   the More important and More mail buttons.

## Alternatives considered

- **Inbox only.** It would leave Needs you, Handled and Other mail paging by
  five on a Mac window tall enough for dozens of rows, the same surprise one
  click away.
- **Open Email on Important.** It changes what Email opens on, which ADR-0231
  decided, and still leaves the other Mac lists at five.

## Consequences

- A Mac list's first read asks for up to 100 thread summaries instead of five.
  The thread-list route already serves 100-row pages for Important, and each
  row is a projected summary, not a mail body.
- The header counts are unchanged: Important shows its total, and the
  collections show their counts.
