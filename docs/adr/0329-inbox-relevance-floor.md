# ADR-0329: Inbox omits mail ranked below a relevance floor

- Status: Accepted — owner requested on 2026-11-16
- Date: 2026-11-16
- Amends: ADR-0231
- Related: ADR-0234, ADR-0326
- Detailed design: `docs/plan/email-experience.md`

## Context

ADR-0231 made the Inbox collection every retained, authorized cached Inbox
thread, including low-ranked mail. The owner reviewed the 100 most recent
production Inbox threads on 2026-11-16: 70 ranked below 0.1, almost all junk
first-pass results, suspected spam, bulk promotion and event invitations, and
none of them was mail the owner needed to see. The owner asked to filter that
noise out of Inbox.

## Decision

The Inbox collection and its count include a thread only when its effective
priority, after owner feedback and attention expiry, is at least 0.1. Marking a
thread Important still sets it to 1.0 and returns it to Inbox; marking it less
important sets 0.0 and removes it. Needs you, Handled, Important, Other mail,
suspected spam and search are unchanged. Needs you deliberately keeps
low-ranked reply requests. The draft-preparation and Important thresholds are
unchanged.

A thread not yet assessed, or after a junk first pass, has priority 0 and stays
out of Inbox until a full assessment ranks it at least 0.1. Other mail and search
still reach it.

## Verification

`tests/gates/test_email_collections.py` lists a thread ranked 0.1 and omits one
ranked 0.09 from Inbox and its count, while Needs you still lists both.
