# Finish the Gmail integration

This runbook completes the owner-operated Gmail integration after the Milestone
18 code has reached production. It covers the Google Auth Platform setup, the
three-grant bootstrap ceremony, secure production installation, activation,
the required real-mailbox smoke, rollback, and milestone-close evidence.

The governing design remains [Email Integration](plan/email-integration.md)
and [ADR-0071](adr/0071-milestone-18-email-integration.md). This runbook does
not widen that contract: one Gmail account, no attachment download, no
permanent deletion, and explicit approval for every write and send.

## Finish line

Milestone 18 is operationally complete only when all of these are true:

- the Google project is **External** and **In production**;
- one **Desktop app** OAuth client requests exactly the three designed scopes;
- bootstrap has produced separate read, write, and send credential documents;
- the three owner-only documents are installed on the production host and
  `AGENT_EMAIL_ENABLED=1` composes the Gmail tools;
- a fresh interactive session proves read, approved draft/write, and exactly
  one approved send against the owner's real mailbox;
- a scoped daily schedule reads mail, parks its write for approval, sends a
  content-free approval notification to the phone, and completes after
  approval; and
- the smoke evidence is recorded without credentials or private mail content.

## Verified starting point

This was checked on 2026-09-03 and must be rechecked before activation:

- The Gmail implementation reached `main` in [GitHub pull request 73](https://github.com/avitus/veetbot/pull/73)
  ([Glen review](https://app.tryglen.com/avitus/veetbot/pull/73)). Its final
  head `0adf2d07524f6b4489e8c0cdd70abe56a3d7a1e1` passed CodeRabbit,
  GitGuardian, and the CircleCI static, contract, integration, sandbox, and
  Apple jobs; all nine review threads are resolved. The merge commit is
  `c46595f4741c482eafaeb2fbc5974818c76cdcf2`.
- Production release `20260903-043405-b6f003f` contains that merge. The API,
  documentation, and public website report the same release identity.
- `https://www.veetbot.com/`, `https://www.veetbot.com/privacy`, and
  `https://www.veetbot.com/tos` return HTTP 200.
- The schedule API, schedule worker, notification API, notification dispatcher,
  and APNs provider are enabled. All application and notification units are
  active.
- Gmail remains disabled and `/etc/veetbot/gmail` does not exist.
- On the current Droplet, the active release owner and application service user
  are both `veetbot`. Recheck this identity assumption in phase 3.

These commands refresh the public part of that checkpoint:

```bash
curl --fail --show-error --dump-header - --output /dev/null \
  https://api.veetbot.com/health/ready
curl --fail --show-error https://docs.veetbot.com/release.txt
curl --fail --show-error https://www.veetbot.com/release.txt
curl --fail --show-error --output /dev/null https://www.veetbot.com/
curl --fail --show-error --output /dev/null https://www.veetbot.com/privacy
curl --fail --show-error --output /dev/null https://www.veetbot.com/tos
```

## Safety rules

- Never paste an OAuth client JSON, refresh token, access token, API bearer
  token, or raw credential document into chat, a ticket, Git, shell history,
  logs, or milestone evidence.
- Keep the downloaded client JSON and all generated credential documents in
  owner-only directories. Every JSON file must be a regular, non-symlink file
  with mode `0600`.
- Do not add `https://mail.google.com/`, identity scopes, Calendar scopes, or
  any scope not listed below.
- Use a fresh Veetbot session after activation. Existing sessions retain their
  pinned agent/tool configuration.
- Never repeat a Gmail write or send after `gmail.outcome_unknown` or another
  uncertain post-dispatch result. Reconcile the draft, labels, trash, or
  recipient mailbox first.

## Phase 1 — Finish Google Auth Platform setup

Use the Google Cloud project owned by the intended personal Google account.
Google's current console groups the settings under **Google Auth Platform**.
The official references are [Create access credentials](https://developers.google.com/workspace/guides/create-credentials),
[Manage App Audience](https://support.google.com/cloud/answer/15549945),
[OAuth scope catalog](https://developers.google.com/identity/protocols/oauth2/scopes),
and [App Homepage requirements](https://support.google.com/cloud/answer/13807376).

1. Select the Veetbot project and enable **Gmail API** under **APIs & Services >
   Library**.
2. Open **Google Auth Platform > Branding** and use:

   | Field | Value |
   | --- | --- |
   | App name | `Veetbot` |
   | User support email | the owner's monitored email address |
   | Application home page | `https://www.veetbot.com/` |
   | Privacy policy | `https://www.veetbot.com/privacy` |
   | Terms of service | `https://www.veetbot.com/tos` |
   | Authorized domain, if requested | `veetbot.com` |
   | Developer contact | the owner's monitored email address |

   Include `https://`; do not enter a bare `www.veetbot.com` value. The
   homepage must remain public, must not redirect to another domain, and must
   retain its visible privacy-policy link.
3. Under **Audience**, choose **External**. While the app is in Testing, add
   the intended Gmail account as a test user.
4. Under **Data Access**, add exactly these scopes:

   ```text
   https://www.googleapis.com/auth/gmail.readonly
   https://www.googleapis.com/auth/gmail.modify
   https://www.googleapis.com/auth/gmail.send
   ```

5. Under **Clients**, select **Create client**, choose **Desktop app**, name it
   `Veetbot owner bootstrap`, create it, and download the JSON. Do not create a
   Web application client and do not configure a redirect URI manually; the
   bootstrap command uses installed-app loopback callbacks on
   `127.0.0.1:8765`, `:8766`, and `:8767`.
6. Under **Audience**, select **Publish app** so the publishing status is **In
   production** before generating the three refresh tokens. Google documents
   that Testing authorizations expire after seven days; the repository
   contract deliberately accepts the possible one-time unverified-app warning
   for this owner-only client rather than operating on that seven-day clock.
7. If Google offers a verification workflow, keep the configured scopes and
   URLs exact. Do not widen scopes to get past a console prompt. An owner-only,
   unverified production client may show Google's warning and remains subject
   to Google's user cap. If Google blocks authorization instead of offering the
   warning, stop and record only the console status/error text; complete
   Google's verification workflow before continuing.

Checkpoint:

- [ ] Gmail API enabled
- [ ] Branding URLs saved with `https://`
- [ ] Audience is External
- [ ] Publishing status is In production
- [ ] Exactly three Gmail scopes configured
- [ ] Desktop app client JSON downloaded privately

## Phase 2 — Run the three-grant bootstrap ceremony locally

Run this on the owner's Mac from a current repository checkout. The browser
must run on the same machine because Google returns to loopback ports. The
command refuses to overwrite existing output files.

```bash
git switch main
git pull --ff-only
uv sync --all-groups

export VEETBOT_GMAIL_CLIENT="/absolute/path/to/client-secret.json"
export VEETBOT_GMAIL_OUTPUT="$HOME/.config/veetbot/gmail-bootstrap-$(date +%Y%m%d-%H%M%S)"

chmod 0600 "$VEETBOT_GMAIL_CLIENT"
install -d -m 0700 "$VEETBOT_GMAIL_OUTPUT"
lsof -nP -iTCP:8765 -iTCP:8766 -iTCP:8767 -sTCP:LISTEN

uv run python -m gmail_mcp bootstrap \
  --client-file "$VEETBOT_GMAIL_CLIENT" \
  --output-directory "$VEETBOT_GMAIL_OUTPUT"
```

`lsof` should print nothing. During bootstrap, complete three separate Google
consents while signed in to the intended Gmail account:

1. read — `gmail.readonly`;
2. write — `gmail.modify`; and
3. send — `gmail.send`.

The command may print only each output path and granted scope. It must create:

```text
gmail-read.json
gmail-write.json
gmail-send.json
```

Validate names, modes, keys, and scopes without printing credential values:

```bash
stat -f '%Lp %N' "$VEETBOT_GMAIL_OUTPUT"/*.json
uv run python - "$VEETBOT_GMAIL_OUTPUT" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
expected = {
    "gmail-read.json": "https://www.googleapis.com/auth/gmail.readonly",
    "gmail-write.json": "https://www.googleapis.com/auth/gmail.modify",
    "gmail-send.json": "https://www.googleapis.com/auth/gmail.send",
}
for name, scope in expected.items():
    data = json.loads((root / name).read_text())
    assert set(data) == {"client_id", "client_secret", "refresh_token", "scope"}
    assert data["scope"] == scope
    assert all(data[key] for key in ("client_id", "client_secret", "refresh_token"))
    print(f"OK {name}: {scope}")
PY
```

Checkpoint:

- [ ] Three consents completed under the intended account
- [ ] Three files exist and report mode `600`
- [ ] Validation prints three `OK` lines and no credential values

### Bootstrap named accounts

The command above remains the backward-compatible single-account ceremony.
For concurrent accounts, create a separate empty output directory for each
stable routing id and add `--account-id`. The id is lowercase configuration,
not necessarily the mailbox address:

```bash
export VEETBOT_GMAIL_ACCOUNT_ID="personal"
export VEETBOT_GMAIL_OUTPUT="$HOME/.config/veetbot/gmail-$VEETBOT_GMAIL_ACCOUNT_ID-$(date +%Y%m%d-%H%M%S)"
install -d -m 0700 "$VEETBOT_GMAIL_OUTPUT"
uv run python -m gmail_mcp bootstrap \
  --client-file "$VEETBOT_GMAIL_CLIENT" \
  --output-directory "$VEETBOT_GMAIL_OUTPUT" \
  --account-id "$VEETBOT_GMAIL_ACCOUNT_ID"
```

Repeat with `VEETBOT_GMAIL_ACCOUNT_ID=work`, selecting the matching Google
account for all three browser consents. Tagged credential documents are not
accepted under a different id, but the label cannot independently prove which
Google identity was selected; the work-only draft smoke below is the identity
check. Do not edit an existing credential JSON to add the label.

## Phase 3 — Install and activate production credentials

The current Droplet uses `veetbot` for both release ownership and application
services. Verify rather than assuming that remains true:

```bash
ssh root@api.veetbot.com '
  set -eu
  printf "release owner: "
  stat -c %U /opt/veetbot/shared/deploy.lock
  printf "api user: "
  systemctl show --property User --value veetbot-api
  printf "worker user: "
  systemctl show --property User --value veetbot-worker
'
```

All three values must be `veetbot`. If the release owner and service user
differ, stop: an owner-only `0600` source cannot be read by both identities.
Add a reviewed systemd `LoadCredential` handoff and a deploy-readable source
before activation; do not loosen the files to `0640` or `0644`.

Create a root-only upload directory, transfer the three files, and install
them for the service identity:

```bash
ssh root@api.veetbot.com \
  'install -d -o root -g root -m 0700 /root/veetbot-gmail-upload'

scp "$VEETBOT_GMAIL_OUTPUT"/gmail-{read,write,send}.json \
  root@api.veetbot.com:/root/veetbot-gmail-upload/

ssh root@api.veetbot.com '
  set -eu
  install -d -o veetbot -g veetbot -m 0700 /etc/veetbot/gmail
  for mode in read write send; do
    install -o veetbot -g veetbot -m 0600 \
      "/root/veetbot-gmail-upload/gmail-$mode.json" \
      "/etc/veetbot/gmail/gmail-$mode.json"
  done
  stat -c "%U:%G %a %n" /etc/veetbot/gmail /etc/veetbot/gmail/*.json
  rm -f /root/veetbot-gmail-upload/gmail-read.json \
    /root/veetbot-gmail-upload/gmail-write.json \
    /root/veetbot-gmail-upload/gmail-send.json
  rmdir /root/veetbot-gmail-upload
'
```

The directory must report `veetbot:veetbot 700`; each file must report
`veetbot:veetbot 600`.

Back up `/etc/veetbot/veetbot.env`, then edit it in one save. Store paths only,
never JSON or token material:

```text
AGENT_EMAIL_ENABLED=1
GMAIL_READ_CREDENTIAL_FILE=/etc/veetbot/gmail/gmail-read.json
GMAIL_WRITE_CREDENTIAL_FILE=/etc/veetbot/gmail/gmail-write.json
GMAIL_SEND_CREDENTIAL_FILE=/etc/veetbot/gmail/gmail-send.json
```

The flag and all three paths are atomic configuration: the application rejects
partial enablement, and it also rejects configured Gmail paths while the flag
is off.

For concurrent named accounts, install each tagged triplet in its own
owner-only directory and use a non-secret manifest instead of the three legacy
variables:

```json
{
  "version": 1,
  "default_account": "personal",
  "accounts": [
    {
      "account_id": "personal",
      "read_credential_file": "/etc/veetbot/gmail/personal/gmail-read.json",
      "write_credential_file": "/etc/veetbot/gmail/personal/gmail-write.json",
      "send_credential_file": "/etc/veetbot/gmail/personal/gmail-send.json"
    },
    {
      "account_id": "work",
      "read_credential_file": "/etc/veetbot/gmail/work/gmail-read.json",
      "write_credential_file": "/etc/veetbot/gmail/work/gmail-write.json",
      "send_credential_file": "/etc/veetbot/gmail/work/gmail-send.json"
    }
  ]
}
```

Install the manifest as `/etc/veetbot/gmail/accounts.json`, owned by `veetbot`
and not group- or world-writable. Then configure paths only:

```text
AGENT_EMAIL_ENABLED=1
GMAIL_ACCOUNTS_FILE=/etc/veetbot/gmail/accounts.json
GMAIL_READ_CREDENTIAL_FILE=
GMAIL_WRITE_CREDENTIAL_FILE=
GMAIL_SEND_CREDENTIAL_FILE=
```

The default account retains `mcp.gmail_read.*`, `mcp.gmail_write.*`, and
`mcp.gmail_send.*`. The `work` account is advertised as
`mcp.gmail_work_read.*`, `mcp.gmail_work_write.*`, and
`mcp.gmail_work_send.*`. The two configuration forms are mutually exclusive.

Run the production preflight as the same identity that owns the credentials,
then restart the four units that compose application tools:

```bash
ssh root@api.veetbot.com '
  set -eu
  sudo -u veetbot bash --noprofile --norc -c '\''
    set -a
    . /etc/veetbot/veetbot.env
    . /opt/veetbot/current/.release.env
    set +a
    cd /opt/veetbot/current
    .venv/bin/python scripts/check_production_deployment.py
  '\''
  systemctl restart \
    veetbot-maintenance veetbot-worker veetbot-async-worker veetbot-api
  systemctl is-active \
    veetbot-maintenance veetbot-worker veetbot-async-worker veetbot-api \
    veetbot-schedule veetbot-notify
'
curl --fail --show-error --dump-header - --output /dev/null \
  https://api.veetbot.com/health/ready
```

Do not continue unless preflight reports `OK`, every listed unit reports
`active`, and readiness returns the expected release header.

## Phase 4 — Real-mailbox interactive smoke

Send a harmless message from a second mailbox to the connected Gmail account
with a unique subject such as `VEETBOT-GMAIL-SMOKE-20260903-A`. Do not use real
customer mail or confidential content. Open a **new** Veetbot conversation
after the service restart.

Run these checks in order:

For each non-default named account, first create an unsent draft manually in
that mailbox with a unique subject and ask the matching read tool to search
`in:drafts subject:"<unique subject>"` while reporting only the match count.
For `work`, require `mcp.gmail_work_read.search_threads`. A match through the
default `mcp.gmail_read.*` tools does not prove the work credential is active.

1. **Read without approval.** Ask: “Use the Gmail tools to list my labels and
   find the message with subject `VEETBOT-GMAIL-SMOKE-20260903-A`.” Confirm the
   correct message is found and no approval is requested.
2. **Write with approval.** Ask: “Create a draft reply to that smoke-test
   message with body `Veetbot Gmail draft smoke passed.` Do not send it.”
   Confirm the run parks, the phone receives an approval notification that
   contains no recipient, subject, or body, approve once, and verify the draft
   exists in Gmail but was not sent.
3. **Send with approval.** Ask: “Send exactly one plain-text email to the test
   recipient with subject `VEETBOT-GMAIL-SEND-20260903-A` and body `Veetbot
   Gmail send smoke passed.`” Inspect the complete proposal, approve once on
   the phone, and verify exactly one message arrives.

If the write or send returns an uncertain outcome, do not repeat it. Inspect
Gmail drafts, thread labels/trash, Sent, and the recipient mailbox, then record
the reconciled outcome.

Checkpoint:

- [ ] Read completed without approval
- [ ] Draft/write parked and completed only after phone approval
- [ ] Phone notification contained no mail content
- [ ] Exactly one approved message was received

## Phase 5 — Scheduled triage smoke

Do not create this schedule through the conversational `schedule.create` tool.
That tool intentionally fixes `requested_scopes` to an empty set, so its runs
cannot use Gmail. Create the smoke schedule through `POST /v1/schedules` with
the exact read and write scopes.

First create a fresh session through the API and copy its returned `agent_id`
and `agent_version`. Protect the bearer token from process arguments by passing
the authorization header through a private file or standard input, as the
release script does. Then submit this definition with a unique
`Idempotency-Key`; choose a `local_time` three to five minutes in the future:

```json
{
  "title": "Gmail triage smoke",
  "instruction": "Search Gmail for the message whose subject is VEETBOT-GMAIL-SMOKE-20260903-A. Summarize it, then create one draft reply with body 'Scheduled Gmail triage smoke passed.' Never send the draft.",
  "agent_id": "<agent_id from the fresh session>",
  "agent_version": "<agent_version from the fresh session>",
  "policy_profile": "default",
  "requested_scopes": [
    "mcp.gmail_read.use",
    "mcp.gmail_write.use"
  ],
  "limits": {
    "max_steps": 8,
    "max_model_calls": 8,
    "max_tool_calls": 8,
    "max_cost": "1"
  },
  "run_timeout_seconds": 300,
  "cadence": {
    "kind": "DAILY",
    "local_time": "<HH:MM:SS>",
    "timezone": "America/Los_Angeles"
  },
  "misfire_grace_seconds": 60,
  "max_consecutive_failures": 3
}
```

The following production-host sequence keeps the bearer token out of command
arguments. Set the smoke subject and triage time before running it:

```bash
ssh root@api.veetbot.com

set -eu
umask 077
set -a
. /etc/veetbot/veetbot.env
set +a

export VEETBOT_SMOKE_SUBJECT="VEETBOT-GMAIL-SMOKE-20260903-A"
export VEETBOT_TRIAGE_TIME="<HH:MM:SS three to five minutes ahead>"
export VEETBOT_API_BASE="http://127.0.0.1:8000"
export VEETBOT_PYTHON="/opt/veetbot/current/.venv/bin/python"

VEETBOT_AUTH_HEADER="$(mktemp)"
VEETBOT_SESSION_RESPONSE="$(mktemp)"
VEETBOT_SCHEDULE_REQUEST="$(mktemp)"
VEETBOT_SCHEDULE_RESPONSE="$(mktemp)"
trap 'rm -f -- "$VEETBOT_AUTH_HEADER" "$VEETBOT_SESSION_RESPONSE" \
  "$VEETBOT_SCHEDULE_REQUEST" "$VEETBOT_SCHEDULE_RESPONSE"' EXIT

printf 'Authorization: Bearer %s\n' "$AUTH_TOKEN" >"$VEETBOT_AUTH_HEADER"

curl --fail-with-body --silent --show-error \
  --header @"$VEETBOT_AUTH_HEADER" \
  --header 'Content-Type: application/json' \
  --data '{"agent_id":"general","metadata":{"purpose":"gmail-triage-smoke"}}' \
  --output "$VEETBOT_SESSION_RESPONSE" \
  "$VEETBOT_API_BASE/v1/sessions"

"$VEETBOT_PYTHON" - \
  "$VEETBOT_SESSION_RESPONSE" \
  "$VEETBOT_SCHEDULE_REQUEST" \
  "$VEETBOT_TRIAGE_TIME" \
  "$VEETBOT_SMOKE_SUBJECT" <<'PY'
import json
import pathlib
import sys

session_path, request_path, local_time, subject = sys.argv[1:]
session = json.loads(pathlib.Path(session_path).read_text())
request = {
    "title": "Gmail triage smoke",
    "instruction": (
        f"Search Gmail for the message whose subject is {subject}. Summarize it, "
        "then create one draft reply with body 'Scheduled Gmail triage smoke "
        "passed.' Never send the draft."
    ),
    "agent_id": session["agent_id"],
    "agent_version": session["agent_version"],
    "policy_profile": "default",
    "requested_scopes": ["mcp.gmail_read.use", "mcp.gmail_write.use"],
    "limits": {
        "max_steps": 8,
        "max_model_calls": 8,
        "max_tool_calls": 8,
        "max_cost": "1",
    },
    "run_timeout_seconds": 300,
    "cadence": {
        "kind": "DAILY",
        "local_time": local_time,
        "timezone": "America/Los_Angeles",
    },
    "misfire_grace_seconds": 60,
    "max_consecutive_failures": 3,
}
pathlib.Path(request_path).write_text(json.dumps(request))
PY

curl --fail-with-body --silent --show-error \
  --header @"$VEETBOT_AUTH_HEADER" \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: gmail-triage-smoke-20260903-a' \
  --data-binary @"$VEETBOT_SCHEDULE_REQUEST" \
  --output "$VEETBOT_SCHEDULE_RESPONSE" \
  "$VEETBOT_API_BASE/v1/schedules"

VEETBOT_SCHEDULE_ID="$(
  "$VEETBOT_PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["schedule"]["id"])' \
    "$VEETBOT_SCHEDULE_RESPONSE"
)"
printf 'schedule_id=%s\n' "$VEETBOT_SCHEDULE_ID"
```

Observe the occurrence through
`GET /v1/schedules/{schedule_id}/occurrences`. It must materialize a run, read
the message without approval, park before `create_draft`, send a content-free
approval notification to the phone, and complete after one approval. Verify
the draft in Gmail and the schedule-outcome notification. Then cancel the
recurring smoke schedule with
`DELETE /v1/schedules/{schedule_id}?expected_revision=1` so it does not run the
next day.

From the same protected shell, inspect and cancel with:

```bash
curl --fail-with-body --silent --show-error \
  --header @"$VEETBOT_AUTH_HEADER" \
  "$VEETBOT_API_BASE/v1/schedules/$VEETBOT_SCHEDULE_ID/occurrences" \
  | "$VEETBOT_PYTHON" -m json.tool

curl --fail-with-body --silent --show-error --request DELETE \
  --header @"$VEETBOT_AUTH_HEADER" \
  "$VEETBOT_API_BASE/v1/schedules/$VEETBOT_SCHEDULE_ID?expected_revision=1" \
  | "$VEETBOT_PYTHON" -m json.tool

exit
```

Checkpoint:

- [ ] Daily schedule created with only read and write Gmail scopes
- [ ] Occurrence materialized and linked to a run
- [ ] Read ran without approval
- [ ] Draft waited for approval and notification was content-free
- [ ] Run completed after approval and outcome notification arrived
- [ ] Smoke schedule cancelled after the successful occurrence

## Phase 6 — Record evidence and close Milestone 18

Record only non-secret evidence:

- date and operator;
- Google project ID, External audience, and In production status;
- OAuth client type and name, never its ID secret or downloaded JSON;
- the three granted scope names;
- production credential paths, owner, and modes, never hashes or contents;
- production release identity and active unit names;
- Veetbot session, run, approval, schedule, and occurrence IDs;
- the harmless smoke subject tokens and pass/fail outcomes; and
- confirmation that the approval notification exposed no mail content and the
  recipient received exactly one message.

Update `docs/status/project-state.yaml` and `docs/status/milestones.md` only
after every owner-smoke checkpoint passes. Mark Milestone 18 complete, set its
completion date, and move the completed verification record to
`docs/status/verification-history.yaml` as required by repository governance.
Run:

```bash
make citations-fix
make docs-check
make check
uv run python -m scripts.check_reading_lane --base origin/dev
```

The closing commit is a full-contract change and must carry a
`Reading-Lane: A` trailer. Deliver it through the normal dev-to-main review
path; do not treat credential installation or a local smoke alone as milestone
completion.

## Rollback and recovery

If activation fails before any successful write or send:

1. Set `AGENT_EMAIL_ENABLED=0`, blank all three
   `GMAIL_*_CREDENTIAL_FILE` values, and blank `GMAIL_ACCOUNTS_FILE` in the
   same edit.
2. Restart `veetbot-maintenance`, `veetbot-worker`, `veetbot-async-worker`, and
   `veetbot-api`.
3. Confirm readiness and a fresh non-email conversation.
4. Keep the `0600` credential documents in place while diagnosing; removing a
   file before disabling the feature makes configuration fail closed.

If a refresh token is revoked or Google rejects it, disable the integration,
revoke Veetbot under the Google Account's third-party connections, run the
bootstrap ceremony into a new empty directory, replace all three production
files together, rerun preflight, and reactivate. If the OAuth client itself is
compromised, delete or rotate that client in Google Cloud before bootstrapping
new grants.

Common failures:

| Failure | Action |
| --- | --- |
| `OAuth client file must be owner-only` | Use an absolute, non-symlink regular file and `chmod 0600` it. |
| `credential output already exists` | Use a new empty owner-only output directory; do not overwrite a partial grant set. |
| `authorization did not complete` | Free loopback ports 8765–8767, use a browser on the same Mac, and finish within five minutes. |
| `token exchange omitted the exact grant` | Recheck the three exact scopes, revoke the partial Veetbot grant, and rerun all three consents into a new directory. |
| `gmail.credential_rejected` | Check publishing status and revocation, then disable and re-bootstrap; do not edit a credential JSON. |
| configured account id rejected | Confirm the bootstrap `--account-id` matches the manifest `account_id`; re-bootstrap rather than editing the credential document. |
| `gmail.outcome_unknown` | Never retry the write/send. Reconcile directly in Gmail and the recipient mailbox. |
| schedule authorization failure | Confirm the schedule was created over HTTP with the exact `mcp.gmail_read.use` and `mcp.gmail_write.use` scopes. |
