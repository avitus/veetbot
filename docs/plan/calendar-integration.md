---
title: Google Calendar Integration
status: design
canonical: true
---

# Read-only Google Calendar integration

[ADR-0305](../adr/0305-read-only-google-calendar.md) authorizes the read-only
portion of engineering-plan roadmap B11. One operator-managed Google account
exposes one `calendar_read` stdio MCP server, classified `NETWORK_READ`, `LOW`,
`READ_ONLY`, with scope `mcp.calendar_read.use`. Google code is confined to
`src/calendar_mcp/`; existing MCP validation, policy, trust and tracing apply.

## Credentials and selection

`AGENT_CALENDAR_ENABLED` defaults off. When enabled in credential-bearing roles,
`CALENDAR_READ_CREDENTIAL_FILE` names a private 0600 JSON document containing
`client_id`, `client_secret`, `refresh_token`, `scope` and `calendar_ids`.
The scope set is exactly `calendar.calendarlist.readonly` and
`calendar.events.readonly` under `https://www.googleapis.com/auth/`.
One through sixteen distinct calendar IDs are operator-selected at bootstrap;
`primary` is permitted. Tool arguments cannot widen this selection. The server
checks the selection before any event request, including point reads. Calendar
listing reads only selected CalendarList entries; it never returns other calendars.

The broker supplies the document through `CALENDAR_MCP_CREDENTIAL` in a constructed
child environment. Tokens never enter arguments, results, logs or events. Google
endpoints are fixed, redirects forbidden, TLS verified, bodies and time bounded.
Refresh occurs at use; refusal requires repeating consent. Setup uses an installed
application OAuth client, PKCE, one-time state validation and a five-minute loopback
callback. Consent discloses data use and retention before opening the browser.

The scheduler mirrors only the flag, never the credential file. A schedule must
name `calendar_read` to hold its scope; a saved job derives that scope from its
approved grant. Disabled composition adds no server or scope. Deployment validates
that application and scheduler flags agree.

## Tools and temporal semantics

- `list_calendars()` returns selected calendar IDs, names, time zones and access
  roles. Failure of any selected calendar fails the call; absence is not an empty agenda.
- `list_events(calendar_id, time_min, time_max, query?, page_token?, max_results=50)`
  requires offset-bearing RFC3339 instants, an increasing window no longer than
  366 days, and one through 100 results. Google expands recurring instances with
  `singleEvents=true`, orders by start time, and omits cancelled occurrences.
  Results retain IDs, links, original time fields, recurrence identity, status,
  transparency and attendee responses. `next_page_token` and `complete` explicitly
  distinguish a full window from one page. Queries and tokens are bounded.
- `get_event(calendar_id, event_id)` returns one selected calendar's event, including
  cancelled status when Google supplies it. An unknown or forbidden event fails.

All-day values stay civil dates with exclusive end dates. Timed values retain
Google's offsets and IANA time zones; no conversion through the server's local
zone occurs. Event text is external and untrusted, and links are never fetched.
No new background synchronization, memory formation, schema or Calendar UI is added.

## Failures

The MCP runtime recognizes only the first-party Calendar server's closed error
codes. Revoked credentials become `tool.server_unauthorized` with a reconnect
instruction; an unselected calendar or invalid arguments become
`tool.arguments_invalid`; denied or missing resources and invalid provider output
remain distinct failures. Only rate limits and temporary provider outages are
retryable through the existing bounded read retry policy. No raw provider error
body is returned to the model or journal.

## Acceptance and verification

Begin with failing adapter contract and configuration tests. Offline tests cover
three-tool discovery, exact scopes, calendar selection on both list and point reads,
recurrence request semantics, all-day and DST preservation, pagination, malformed
arguments and provider output, revoked tokens, 429/5xx failures, response bounds,
redaction, private credential files, disabled composition, policy classification,
credential-free scheduler authority and explicit schedule selection. Run focused
checks and relevant partitions inside the local Linux sandbox, plus lint, typing,
architecture and documentation checks. Production acceptance needs owner consent,
a real agenda comparison and an explicitly selected scheduled briefing.

## References

- [Google scopes](https://developers.google.com/workspace/calendar/api/auth).
- [Event listing](https://developers.google.com/workspace/calendar/api/v3/reference/events/list).
- [Desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app).
