# Activate and use the WhatsApp integration

This runbook activates Veetbot's dedicated business number through the official
Meta WhatsApp Business Cloud API. It does not connect to or read a personal
WhatsApp account. The governing designs are [Inbound Surfaces](plan/inbound-surfaces.md)
and [WhatsApp Surface](plan/whatsapp-surface.md).

## Finish line

Activation is complete only when:

- the Meta app, WhatsApp Business Account, dedicated number, and approved
  content-free utility template exist;
- the webhook callback is `https://api.veetbot.com/webhooks/whatsapp` and is
  subscribed to messages;
- the access token, app secret, and independently generated verify token are
  installed as three owner-only regular files on the production host;
- the API and surface-role flags agree, the surface listener binds only
  `127.0.0.1:8002`, and Nginx is the sole public ingress;
- one owner pairing completes through `/pair <code>`; and
- the live smoke proves inbound text, a reply, an approval or question
  round-trip, the twenty-four-hour template boundary, and immediate revocation.

Meta changes its dashboard labels periodically. Reconfirm the ceremony against
the current [Cloud API documentation](https://developers.facebook.com/docs/whatsapp/cloud-api/get-started),
[Webhooks documentation](https://developers.facebook.com/docs/graph-api/webhooks/getting-started),
and [template documentation](https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-message-templates)
before production activation.

## Safety rules

- Never paste any of the three credential values into chat, Git, tickets,
  command arguments, shell history, logs, or milestone evidence.
- Store each value in a separate regular, non-symlink file owned by the
  `veetbot` service identity with mode `0600`. The normal application
  environment contains only flags; only the surface role receives the files.
- Use the dedicated business number. Do not use an unofficial linked-device
  bridge for a personal account.
- Keep the utility template content-free. It may say only that a Veetbot update
  is available; it must not contain a prompt, answer, approval summary, or
  private identifier.
- Treat a delivery with an unknown outcome as unreconciled. Inspect Meta's
  delivery state before attempting another send.

## Phase 1 — Prepare Meta resources

In the owner-controlled Meta business portfolio:

1. Create or select the Veetbot Meta app and add the WhatsApp product.
2. Create or select the WhatsApp Business Account and attach the dedicated
   Veetbot number. Record the phone-number ID, not the displayed phone number.
3. Provision a production access token with only the permissions required to
   send through that WhatsApp Business Account. Do not install a temporary
   dashboard token on the host.
4. Read the app secret from the app settings. Generate a separate high-entropy
   webhook verify token outside the repository.
5. Submit a utility template named `veetbot_update_available` in `en_US`. It
   must have no variables and no private content; its purpose is only to ask
   the owner to reopen the chat when the freeform window is closed.

Keep the three secret values and the phone-number ID in an owner-controlled
credential manager. Record only resource identifiers and review status in
operational evidence.

## Phase 2 — Install the private files

Transfer the three secrets to temporary owner-only files out of band, then
install them without printing their contents:

```bash
sudo install -d -o veetbot -g veetbot -m 0700 /etc/veetbot/secrets
sudo install -o veetbot -g veetbot -m 0600 \
  /secure/input/whatsapp-access-token \
  /etc/veetbot/secrets/whatsapp-access-token
sudo install -o veetbot -g veetbot -m 0600 \
  /secure/input/whatsapp-app-secret \
  /etc/veetbot/secrets/whatsapp-app-secret
sudo install -o veetbot -g veetbot -m 0600 \
  /secure/input/whatsapp-verify-token \
  /etc/veetbot/secrets/whatsapp-verify-token
sudo stat -c '%a %U:%G %n' /etc/veetbot/secrets/whatsapp-*
```

The final command must report `600 veetbot:veetbot` for three regular files.
Delete the temporary transfer copies after verifying the installed files and
the credential manager backup.

## Phase 3 — Configure and deploy the surface role

Set all three flags to `1` in `/etc/veetbot/veetbot.env`:

```text
AGENT_SURFACE_API_ENABLED=1
AGENT_SURFACE_WORKER_ENABLED=1
AGENT_SURFACE_WHATSAPP_ENABLED=1
```

Create `/etc/veetbot/veetbot-surface.env` from
`deploy/veetbot-surface.env.example`. Set the same flags, the production
database identity, tenant and principal identity, phone-number ID, current
reviewed Graph API version, and these paths:

```text
AGENT_SURFACE_TELEGRAM_TOKEN_FILE=/etc/veetbot/secrets/telegram-bot-token
AGENT_SURFACE_WHATSAPP_TOKEN_FILE=/etc/veetbot/secrets/whatsapp-access-token
AGENT_SURFACE_WHATSAPP_APP_SECRET_FILE=/etc/veetbot/secrets/whatsapp-app-secret
AGENT_SURFACE_WHATSAPP_VERIFY_TOKEN_FILE=/etc/veetbot/secrets/whatsapp-verify-token
AGENT_SURFACE_WHATSAPP_TEMPLATE_NAME=veetbot_update_available
AGENT_SURFACE_WHATSAPP_TEMPLATE_LANGUAGE=en_US
```

The Telegram token remains required because WhatsApp is an additive adapter on
the Milestone 14 surface role. Keep `AUTH_TOKEN`, model/provider keys, browser
credentials, sandbox access, and the general credential map out of this file.
The role scopes should include `run.read`, `run.write`, `run.cancel`,
`surface.read`, `surface.write`, `approval.read`, and `approval.resolve`.

Deploy through the reviewed `main` release pipeline. The release preflight
refuses mismatched flags, absent or unsafe credential files, and missing
WhatsApp identifiers before it changes the active release. After deployment,
verify without printing environment contents:

```bash
sudo systemctl is-active veetbot-surface.service
sudo systemctl show veetbot-surface.service -p MainPID -p FragmentPath
sudo ss -ltnp | grep '127.0.0.1:8002'
curl --fail --show-error --output /dev/null https://api.veetbot.com/health/ready
```

There must be no public bind on port `8002`.

## Phase 4 — Register the webhook

In the Meta app's WhatsApp webhook configuration:

1. Set the callback URL to
   `https://api.veetbot.com/webhooks/whatsapp`.
2. Supply the same verify-token value held in the private verify-token file.
3. Complete verification and subscribe the WhatsApp Business Account to the
   `messages` field.

The GET challenge succeeds only for the exact token. Signed POST delivery is
accepted only when `X-Hub-Signature-256` matches the raw body; failed
verification is content-free and creates no message, session, or run.

## Phase 5 — Pair the owner

From an authenticated Veetbot CLI environment, list the registered surfaces:

```bash
agent surface list
```

Select the row whose platform is `whatsapp`, then mint a short-lived one-time
code. Grant only the chat actions you intend to use:

```bash
agent surface pair WHATSAPP_SURFACE_ID \
  --scope run.read \
  --scope run.write \
  --scope run.cancel \
  --scope approval.read \
  --scope approval.resolve \
  --label Owner
```

Send the printed code to the dedicated business number as:

```text
/pair ONE_TIME_CODE
```

The code is shown once, expires, and cannot be reused. Confirm the pairing
without exposing the code:

```bash
agent surface pairings WHATSAPP_SURFACE_ID
```

## Everyday use

Send an ordinary direct text to start or continue a Veetbot session. The
supported commands are:

```text
/new                       rotate to a new session
/stop                      cancel the current run
/status                    request channel status
/help                      show the command reminder
/approve APPROVAL_ID       approve a pending action once
/deny APPROVAL_ID          deny a pending action
```

Group chats, threads, media, and files are refused. While a run is active, a
second ordinary message does not start another run. When Veetbot is waiting for
input, the next ordinary message answers that run. Pairing scopes are
intersected with the principal's current scopes on every message, so removing a
principal scope takes effect immediately.

Within twenty-four hours of the latest inbound message, Veetbot may send the
redacted reply or authorized notification as freeform text. Outside that
window it sends exactly one content-free approved template; reply to the chat
to reopen the freeform window.

## Live smoke and evidence

Use non-sensitive test text and record identifiers, timestamps, dispositions,
and service health only:

- [ ] unsigned and incorrectly signed webhook probes are rejected empty;
- [ ] one direct `/pair` succeeds and replay of the code fails;
- [ ] one plain message creates one ordinary run and one reply;
- [ ] replaying the Meta message ID creates no second run;
- [ ] `/new`, `/stop`, and a waiting-question reply behave as documented;
- [ ] one approval is resolved once and the opposite decision cannot win later;
- [ ] a send outside twenty-four hours uses only the approved template;
- [ ] `agent surface revoke PAIRING_ID` blocks the very next message; and
- [ ] logs and evidence contain no secret, message body, or private answer.

## Disable or rotate

To disable WhatsApp while retaining Telegram, set
`AGENT_SURFACE_WHATSAPP_ENABLED=0` in both application and surface environment
files and deploy the reviewed configuration. Confirm the surface unit remains
active for Telegram and port `8002` is no longer listening. Then remove the
WhatsApp webhook subscription and retire the token in Meta.

To remove one sender without disabling the adapter:

```bash
agent surface revoke PAIRING_ID
```

Revocation clears its delivery route and rotates the chat's session mapping
before the next message. After a credential rotation, replace the corresponding
private file atomically and restart `veetbot-surface.service`; never print or
copy the value through the command line.
