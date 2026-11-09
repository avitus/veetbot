---
title: Google Calendar Setup
---

# Connect Google Calendar

This integration reads one Google account's selected calendars. It cannot create,
edit or delete events or invite attendees. It is disabled until configured.

1. Enable the Google Calendar API in your Google Cloud project. Configure an
   installed desktop OAuth client and download its configuration to an absolute,
   private file (`chmod 600`). Use production publishing status for durable access;
   external testing grants may expire after seven days. Complete any Google consent
   or Workspace administrator requirements that apply to your account.
2. Run the bootstrap on your own computer, where a browser can complete its local
   callback. Select `primary`, or copy exact calendar IDs from Google Calendar's
   calendar settings. Repeat `--calendar-id` for each selected calendar (up to 16).

   ```sh
   uv run python -m calendar_mcp bootstrap \
     --client-file /private/path/google-client.json \
     --output-directory /private/path/calendar \
     --calendar-id primary
   ```

   Read the disclosure, type `CONTINUE`, choose your Google account and consent.
   The command writes `calendar-read.json` with mode 0600 and never prints tokens.
   It refuses to overwrite an existing file. A new consent should use a fresh
   directory; install the replacement only after checking the selected account.
3. Securely install that file on the application host and configure the API and
   run workers:

   ```text
   AGENT_CALENDAR_ENABLED=1
   CALENDAR_READ_CREDENTIAL_FILE=/var/lib/veetbot/calendar/calendar-read.json
   ```

   Set `AGENT_CALENDAR_ENABLED=1` in the isolated schedule environment as well,
   without the credential-file setting. Restart the affected roles. Open a fresh
   chat so its pinned tool catalog includes the Calendar server.
4. Ask for the selected calendars and tomorrow's agenda. Compare the answer with
   Google Calendar, including a recurring event, an all-day event and an event
   across a daylight-saving transition. Check event links and complete pagination.
5. For a daily briefing, explicitly select `calendar_read` through the schedule's
   `use_mcp_servers` field and approve the schedule. Existing schedules do not gain
   Calendar access automatically. Saved jobs require their own approved MCP grant.

If authorization expires or is revoked, repeat consent. A denied calendar or a
provider outage is an error, never an empty agenda. Changing selected calendars
requires replacing the credential document and restarting the credential-bearing
roles. The selection applies to Chat and every schedule or job granted this server,
so replacing it changes the calendars available to those existing grants; it is
not an instant in-chat revocation control. To disconnect, disable the flag in application and schedule environments, restart the roles, remove the local
credential file and revoke the grant in your Google Account. Session history is
removed through ordinary session deletion; disconnecting does not erase it.

For rollback to a release before ADR-0305, first cancel or replace Calendar-backed
schedules and job grants, remove the Calendar settings from all roles, and verify
no active work relies on the removed scope. Do not enable production before owner
consent and the live smoke above.
