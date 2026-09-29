# Activate and use the Telegram surface

This runbook connects a Telegram bot to Veetbot so the owner can chat, answer
questions, and approve actions from a phone. The bot uses long polling from the
surface role on the production host; it needs no inbound port, webhook, or
Nginx change. The governing design is [Inbound Surfaces](plan/inbound-surfaces.md);
the [WhatsApp integration runbook](whatsapp-integration-runbook.md) adds the
optional second channel on the same role.

## Finish line

Activation is complete only when:

- the bot exists and joins no groups;
- its token is one owner-only file on the host and in Doppler, nowhere else;
- the application and surface environments agree on the surface flags, and
  `veetbot-surface.service` is active;
- one owner pairing completes through `/pair <code>`; and
- the live smoke below passes: an unpaired refusal, pairing, a run, a question,
  an approval with `/approve`, and revocation before the next message.

## Safety rules

- Never paste the bot token into chat, Git, tickets, command arguments, shell
  history, logs, or milestone evidence. It moves from BotFather to Doppler, and
  from Doppler to the host over a pipe.
- The token file is a regular, non-symlink file owned by `veetbot` with mode
  `0600`. Only the surface role reads it; every other role refuses to start
  with a surface secret-file variable in its environment.
- The surface environment holds no API bearer and no model, web, browser, or
  sandbox credential; the surface role refuses to start with a provider key.
- A pairing code is shown once, expires in ten minutes, and is single use.
  Five wrong codes lock that sender for an hour.
- Every production command below runs as `root` or `veetbot` by SSH key. None
  asks for a password.

## Phase 1 — Create the bot

In Telegram:

1. Open `@BotFather` (the verified account) and send `/newbot`. Give it a
   display name such as `Veetbot` and a username ending in `bot`.
2. BotFather replies with the token. Copy it only into Doppler: project
   `veetbot`, config `prd`, secret `TELEGRAM_BOT_TOKEN`, either in the Doppler
   dashboard or with
   `doppler secrets set TELEGRAM_BOT_TOKEN --project veetbot --config prd`,
   pasting at the prompt.
3. Send `/setjoingroups`, choose the bot, and select **Disable**. Veetbot
   answers direct messages only; this stops anyone adding the bot to a group.
4. Optionally send `/setcommands` with:

   ```text
   new - start a new conversation
   stop - cancel the running request
   status - check the connection
   help - list the commands
   ```

## Phase 2 — Install the token file

From the owner's Mac, pipe the token from Doppler to the host. It never
appears in a command line or in either shell's history:

```bash
doppler secrets get TELEGRAM_BOT_TOKEN --project veetbot --config prd --plain | ssh root@api.veetbot.com 'set -e; d=/etc/veetbot/secrets; test -d "$d" || install -d -o veetbot -g veetbot -m 0700 "$d"; umask 077; t=$(mktemp "$d/.telegram.XXXXXX"); cat > "$t"; chown veetbot:veetbot "$t"; chmod 0600 "$t"; mv -f "$t" "$d/telegram-bot-token"; stat -c "%a %U:%G %s bytes %n" "$d/telegram-bot-token"; runuser -u veetbot -- test -r "$d/telegram-bot-token" && echo "readable by veetbot"'
```

It must print `600 veetbot:veetbot`, a size of about 46 bytes, and
`readable by veetbot`.

## Phase 3 — Configure both environments

The surface role reads its own environment, `/etc/veetbot/veetbot-surface.env`.
This command creates it from the application environment's database and
identity lines, which it never prints, and refuses to overwrite an existing
file. It connects with the application's database login, as the notify role
does:

```bash
ssh root@api.veetbot.com 'set -e; src=/etc/veetbot/veetbot.env; dst=/etc/veetbot/veetbot-surface.env; test ! -e "$dst" || { echo "$dst exists; left unchanged"; exit 1; }; umask 027; grep -E "^(DATABASE_URL|PGSSLMODE|AUTH_TENANT_ID|AUTH_PRINCIPAL_ID|AGENT_CONFIG_DIR)=" "$src" > "$dst"; grep -q "^PGSSLMODE=" "$dst" || echo PGSSLMODE=disable >> "$dst"; grep -q "^AGENT_CONFIG_DIR=" "$dst" || echo AGENT_CONFIG_DIR= >> "$dst"; printf "%s\n" DEPLOYMENT_MODE=production AUTH_MODE=token AUTH_ROLES=surface AUTH_SCOPES=run.read,run.write,run.cancel,surface.read,surface.write,approval.read,approval.resolve,schedule.read,schedule.write AGENT_SURFACE_API_ENABLED=1 AGENT_SURFACE_WORKER_ENABLED=1 AGENT_SURFACE_WHATSAPP_ENABLED=0 AGENT_SURFACE_TELEGRAM_TOKEN_FILE=/etc/veetbot/secrets/telegram-bot-token >> "$dst"; chown root:veetbot "$dst"; chmod 0640 "$dst"; cut -d= -f1 "$dst" | tr "\n" " "; echo'
```

`AUTH_SCOPES` here is the paired principal's scope ceiling on this channel: a
pairing's grant is intersected with it on every message. The schedule scopes
let the smoke create a reminder, which needs approval.

Then turn on the surface flags and add the two surface scopes in the
application environment. The command is idempotent, keeps a backup, and prints
the scope and flag lines so you can confirm notifications are on:

```bash
ssh root@api.veetbot.com 'set -e; f=/etc/veetbot/veetbot.env; cp -p "$f" "$f.bak-telegram"; sed -i -E "/^AUTH_SCOPES=/{/surface\.read/!s/$/,surface.read/;/surface\.write/!s/$/,surface.write/}" "$f"; for v in AGENT_SURFACE_API_ENABLED AGENT_SURFACE_WORKER_ENABLED; do if grep -q "^$v=" "$f"; then sed -i -E "s/^$v=.*/$v=1/" "$f"; else echo "$v=1" >> "$f"; fi; done; grep -q "^AGENT_SURFACE_WHATSAPP_ENABLED=" "$f" || echo AGENT_SURFACE_WHATSAPP_ENABLED=0 >> "$f"; grep -E "^(AUTH_SCOPES|AGENT_SURFACE_[A-Z_]+_ENABLED|AGENT_NOTIFICATION_(API|DISPATCH)_ENABLED)=" "$f"'
```

Both `AGENT_NOTIFICATION_API_ENABLED` and `AGENT_NOTIFICATION_DISPATCH_ENABLED`
must print `1`: approval prompts and questions reach the chat through the
notification outbox. The printed `AUTH_SCOPES` must contain every scope a
pairing will grant, because a code can grant only scopes its minter holds.

## Phase 4 — Deploy

Complete Phases 2 and 3 before the deployment that should activate the
channel: the release preflight refuses a missing surface environment, a missing
token file, or flags that disagree, and it then changes nothing. The release
installs, enables, and restarts `veetbot-surface.service` with the other units.
Deploy through the reviewed `main` pipeline, or rerun the latest `main`
deployment in CircleCI if the code is already live. Then check the role:

```bash
ssh root@api.veetbot.com 'systemctl is-active veetbot-surface; journalctl -u veetbot-surface -n 20 -o cat --no-pager'
```

It must print `active`, and the journal must show no `ConfigurationError`.

## Phase 5 — Pair the owner

The surface role registers the bot as a surface when it first starts. List
surfaces from the current release, as `veetbot`, and note the `id` of the row
whose `platform` is `telegram`:

```bash
ssh veetbot@api.veetbot.com 'cd /opt/veetbot/current && set -a && . /etc/veetbot/veetbot.env && . ./.release.env && set +a && .venv/bin/agent surface list'
```

Mint a one-time code, replacing `SURFACE_ID`. The command prints the code once:

```bash
ssh veetbot@api.veetbot.com 'cd /opt/veetbot/current && set -a && . /etc/veetbot/veetbot.env && . ./.release.env && set +a && .venv/bin/agent surface pair SURFACE_ID --scope run.read --scope run.write --scope run.cancel --scope approval.read --scope approval.resolve --scope schedule.read --scope schedule.write --label "Owner phone"'
```

Within ten minutes, send `/pair CODE` to the bot as a direct message. It
answers "Pairing complete. You can now message Veetbot." List the pairing
without exposing the code:

```bash
ssh veetbot@api.veetbot.com 'cd /opt/veetbot/current && set -a && . /etc/veetbot/veetbot.env && . ./.release.env && set +a && .venv/bin/agent surface pairings SURFACE_ID'
```

## Everyday use

Send an ordinary direct message to start or continue a conversation; it opens
as an ordinary chat that the Apple client shows too. The commands are:

```text
/new                       start a new conversation
/stop                      cancel the running request
/status                    check the connection
/help                      list the commands
/approve ID                approve a pending action once
/deny ID                   deny a pending action
```

While Veetbot is working, another message is refused with "Still working;
/stop to cancel." When Veetbot asks a question, your next message answers it.
Groups and media are refused. Each run a message starts may spend up to USD 10
and is told to answer once USD 2 remains; the surface channel as a whole may
spend USD 25 a day and USD 250 a month.

## Live smoke and evidence

Use harmless text and record only timestamps, results, and identifiers:

1. [ ] **Unpaired refusal.** Before pairing, send `hello`. The bot answers
   "This sender is not paired. Send /pair followed by a code." and nothing
   appears in the Apple client.
2. [ ] **Pairing.** Pair as in Phase 5. Sending the same `/pair CODE` again
   answers "That pairing code is invalid or expired."
3. [ ] **A run.** Send `What is 17 times 23?`. One reply arrives, and the
   conversation appears in the Apple client.
4. [ ] **A question.** Send `Before you answer, ask me one clarifying question
   using the ask-user tool: suggest a name for my new houseplant.` The
   question arrives in the chat; answer it with a plain message, and the
   final reply follows.
5. [ ] **An approval.** Send `Remind me in 10 minutes to drink water.` The bot
   sends "Approval needed", the summary, an `ID:` line, and
   `/approve ID or /deny ID`. Send exactly `/approve ID`. It answers
   "Approval resolved.", and the reply confirms the reminder. Sending the same
   `/approve ID` again changes nothing.
6. [ ] **Commands.** `/status` and `/help` answer; `/new` answers "A new chat
   session will start with your next message."; `/stop` during a long answer
   answers "The active run was stopped."
7. [ ] **Revocation.** List pairings, then revoke the owner pairing, replacing
   `PAIRING_ID`:

   ```bash
   ssh veetbot@api.veetbot.com 'cd /opt/veetbot/current && set -a && . /etc/veetbot/veetbot.env && . ./.release.env && set +a && .venv/bin/agent surface revoke PAIRING_ID'
   ```

   The very next message is answered "This sender is not paired." Pair again
   with a new code to keep using the bot.
8. [ ] **No leaks.** The surface journal and the evidence contain no token,
   code, or message text:

   ```bash
   ssh root@api.veetbot.com 'journalctl -u veetbot-surface --since "1 hour ago" -o cat --no-pager | tail -n 50'
   ```

## Troubleshooting

- **"The daily surface budget is exhausted."** Surface runs today have spent,
  or are reserving, the USD 25 daily ceiling; each run in flight reserves
  USD 10, so a third concurrent run is refused this way. It resets at 00:00 UTC.
- **"Too many requests are running"** Four surface runs are in flight. Wait
  for one to finish.
- **"The agent has a question" or "Approval needed" without detail.** The
  pairing lacks `run.read` or `approval.read`; pair again with them.
- **No approval prompt at all.** Check both notification flags in Phase 3.
- **The unit is not active.** Read
  `ssh root@api.veetbot.com 'journalctl -u veetbot-surface -n 50 -o cat --no-pager'`.
  A `ConfigurationError` names the variable or file to fix.
- **A reply never arrives.** Replies retry for about four hours and then stop;
  the answer is always in the Apple client.

## Disable or rotate

To disable the channel, set `AGENT_SURFACE_API_ENABLED=0` and
`AGENT_SURFACE_WORKER_ENABLED=0` in both environment files and deploy; the
release disables `veetbot-surface.service`. Pairings and conversations remain
as records.

To rotate the token, send `/revoke` to BotFather for the bot, store the new
token in Doppler, rerun the Phase 2 command, and restart the role:

```bash
ssh root@api.veetbot.com 'systemctl restart veetbot-surface && systemctl is-active veetbot-surface'
```
