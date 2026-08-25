# MAKIMA — Telegram Group Watcher

MAKIMA watches the Telegram groups you are already a member of and sends you a
private alert the moment something you care about is said.

It signs in as **your personal account** (via Telethon) to read group messages,
and uses a **separate bot account** purely to deliver alerts and accept your
commands. The bot never joins a group and never reads one.

```
Telegram Groups
      |
Telethon User Client        <- your personal account, read-only monitoring
      |
Message Watcher
      |
Mention / Reply / Keyword Detection
      |
Optional AI Classification Layer      (off by default)
      |
Alert Formatter
      |
Telethon Bot Client         <- delivers alerts, accepts /commands
      |
Private MAKIMA Alerts
```

---

## Table of contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Telegram API setup](#telegram-api-setup)
- [Bot setup with BotFather](#bot-setup-with-botfather)
- [Environment variables](#environment-variables)
- [Local installation](#local-installation)
- [First authentication](#first-authentication)
- [Bot commands](#bot-commands)
- [Configuration files](#configuration-files)
- [Alert template](#alert-template)
- [Hostinger VPS deployment](#hostinger-vps-deployment)
- [Updating](#updating)
- [Migrating from the old watcher](#migrating-from-the-old-watcher)
- [Session file safety](#session-file-safety)
- [Future AI classification](#future-ai-classification)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## Features

- **Keyword alerts** — case-insensitive, boundary-aware matching, so `claim`
  does not fire on `disclaimer`. Multi-word phrases such as `fuel card` work.
- **Mention alerts** — Telegram's own mention flag, a literal `@yourname` in the
  text, and hidden text-mentions of your account.
- **Reply alerts** — when somebody replies to a message you sent.
- **Private bot control panel** — add and remove keywords, flip modes, change
  the alert template, all from a Telegram chat. No SSH, no file editing.
- **Live config updates** — every change is written to disk immediately and
  applied without restarting the process.
- **Docker deployment** — one command to build, one to run.
- **Persistent Telethon sessions** — you log in once; rebuilds never ask again.
- **Graceful shutdown** — SIGTERM and SIGINT close both clients cleanly, so no
  locked SQLite session files.
- **Future AI classification** — a clean, disabled-by-default hook for category,
  severity and "action required" analysis.

---

## How it works

Every incoming group or channel message is checked against three rules. Private
chats and your own outgoing messages are skipped before anything else happens.

| Rule | Triggers when |
| --- | --- |
| **Mention** | Telegram marks the message as mentioning you, the text contains `@yourusername`, or the message text-mentions your user ID |
| **Reply** | Somebody replies to a message your account sent |
| **Keyword** | The text contains one of the keywords in `data/keywords.txt` |

A single message can trigger for several reasons at once, and the alert says so:

```
Reason: 🟥 Mention | 🔍 Keyword: fuel card
```

Messages that trigger nothing cost one regex pass and are dropped.

---

## Requirements

- A Telegram account that is already a member of the groups you want watched
- Telegram `api_id` and `api_hash` (free, from my.telegram.org)
- A Telegram bot token (free, from @BotFather)
- Either Docker (recommended) or Python 3.11+

---

## Telegram API setup

These identify *your application*, not your account, and are needed for
Telethon to connect at all.

1. Go to <https://my.telegram.org> and log in with your phone number.
2. Open **API development tools**.
3. Create an application. Any title and short name will do (e.g. `makima`).
4. Copy the two values it shows you:
   - **App api_id** — a number, e.g. `35221038`
   - **App api_hash** — 32 hex characters

Put them in `.env` as `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`. Never commit
them and never paste them into a chat, an issue or a screenshot.

---

## Bot setup with BotFather

1. Open Telegram and start a chat with [@BotFather](https://t.me/BotFather).
2. Send `/newbot`.
3. Choose a display name (e.g. `MAKIMA Alerts`) and a username ending in `bot`
   (e.g. `makima_alerts_bot`).
4. BotFather replies with a token that looks like
   `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`. That token **is** the bot —
   anyone holding it controls it. Put it in `.env` as `TELEGRAM_BOT_TOKEN`.
5. **Open a private chat with your new bot and press Start.** Telegram forbids
   bots from messaging a user who has never started them, so alerts silently
   fail until you do this.

Optional polish, still in BotFather: `/setdescription`, `/setuserpic`, and
`/setcommands` with:

```
start - Wake the bot and confirm alerts can reach you
help - Show all commands
status - Current modes and counters
keywords - List active keywords
addkeyword - Add a keyword or phrase
removekeyword - Remove a keyword
setmentions - Alert on mentions on|off
setreplies - Alert on replies on|off
setkeywords - Alert on keywords on|off
setmaxchars - Message preview length
template - Show the alert template
settemplate - Replace the alert template
reload - Re-read keywords and settings
```

To revoke a leaked token: BotFather → `/mybots` → your bot → **API Token** →
**Revoke current token**. The old token stops working instantly.

---

## Environment variables

Copy `.env.example` to `.env` and fill it in. `.env` is git-ignored.

| Variable | Required | Meaning |
| --- | --- | --- |
| `TELEGRAM_API_ID` | yes | Numeric app ID from my.telegram.org |
| `TELEGRAM_API_HASH` | yes | 32-character hex hash from my.telegram.org |
| `TELEGRAM_PHONE` | for login | Your number in international format, `+15551234567`. Only used by `app.auth_user` to request the login code |
| `TELEGRAM_BOT_TOKEN` | yes | Bot token from @BotFather |
| `ADMIN_USER_IDS` | no | Comma-separated numeric user IDs allowed to control the bot and receive alerts. Empty = just the account the watcher signs in as |
| `LOG_LEVEL` | no | `DEBUG`, `INFO` (default), `WARNING`, `ERROR` |
| `TELETHON_LOG_LEVEL` | no | Telethon's own logger, default `WARNING` |
| `MAKIMA_DATA_DIR` | no | Override `./data` |
| `MAKIMA_SESSIONS_DIR` | no | Override `./sessions` |
| `MAKIMA_LOGS_DIR` | no | Override `./logs` |
| `AI_PROVIDER` / `AI_API_KEY` / `AI_MODEL` | no | Reserved for a future AI backend. Not needed to run |

**Finding your numeric user ID:** send any command to the bot before setting
`ADMIN_USER_IDS`. If you are not yet authorised, the bot replies with your ID.
It is also printed in the logs on every unauthorised attempt.

---

## Local installation

### With Docker (recommended)

```bash
git clone <YOUR_REPO_URL> telegram-watcher
cd telegram-watcher
cp .env.example .env
nano .env

docker compose run --rm makima python -m app.auth_user
docker compose up -d
docker compose logs -f
```

### Without Docker

```bash
git clone <YOUR_REPO_URL> telegram-watcher
cd telegram-watcher

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
nano .env

python -m app.auth_user
python -m app.main
```

---

## First authentication

Telethon needs to log into your personal account **once**. This is interactive,
so it cannot happen inside a background container.

```bash
docker compose run --rm makima python -m app.auth_user
```

or, without Docker:

```bash
python -m app.auth_user
```

You will be asked for:

```
Phone:                     (skipped if TELEGRAM_PHONE is set in .env)
Telegram login code:       sent to your Telegram app, not usually by SMS
2FA password:              only if your account has two-step verification
```

On success it writes `sessions/user_session.session`. Every later start reuses
that file and never prompts again — which is exactly why the container can
restart unattended.

The bot session (`sessions/bot_session.session`) needs no interaction; it is
created automatically from the bot token on first run.

There is also a convenience wrapper:

```bash
./scripts/auth.sh
```

---

## Bot commands

All commands work only in a **private chat** with your bot, and only for the
user IDs in `ADMIN_USER_IDS` (or, if that is empty, the account the watcher
signed in as).

| Command | What it does |
| --- | --- |
| `/start` | Confirms the bot is alive and alerts can reach you |
| `/help` | Lists every command |
| `/status` | Modes, keyword count, uptime and counters |
| `/keywords` | Lists active keywords (first 120, then a count of the rest) |
| `/addkeyword <word>` | Adds a keyword or phrase, saved to disk immediately |
| `/removekeyword <word>` | Removes a keyword, saved immediately |
| `/setmentions on\|off` | Mention alerts on or off |
| `/setreplies on\|off` | Reply alerts on or off |
| `/setkeywords on\|off` | Keyword alerts on or off |
| `/setmaxchars <20-4000>` | How much message text an alert includes |
| `/template` | Shows the current alert template |
| `/settemplate <text>` | Replaces the template. Multi-line is fine; a literal `\n` also becomes a line break. `/settemplate default` restores the shipped one |
| `/reload` | Re-reads `keywords.txt` and `watcher_settings.json` from disk without restarting |

Example `/status` output:

```
⚙️ MAKIMA STATUS

Mentions: ON
Replies: ON
Keywords: ON
Keywords loaded: 26
Max preview chars: 500
AI classification: OFF

Uptime: 3h 12m 40s
Messages inspected: 1841
Alerts raised: 12
Alerts delivered: 12 (failed 0, queued 0)
Account: Your Name (@yourname)
Bot: @makima_alerts_bot
```

Adding a keyword:

```
/addkeyword broker
/removekeyword broker
```

---

## Configuration files

Two files hold the live configuration, both inside `data/`, both persisted
through a Docker volume:

| File | Purpose |
| --- | --- |
| `data/keywords.txt` | One keyword or phrase per line. Blank lines and `#` comments ignored |
| `data/watcher_settings.json` | Modes, alert formatting, AI toggle |

**These two files are git-ignored on purpose.** The versions tracked in git live
in `data/defaults/`, and the live files are created from them on first run. That
separation is what lets `git pull --ff-only` succeed on every deploy — if the
live files were tracked, every keyword you added through the bot would collide
with the next update.

Default settings:

```json
{
  "watching": {
    "mentions": true,
    "replies": true,
    "keywords": true
  },
  "alerts": {
    "include_message_text": true,
    "max_message_chars": 500,
    "max_keyword_preview": 8,
    "template": "..."
  },
  "formatting": {
    "timestamp_format": "%Y-%m-%d %H:%M:%S UTC"
  },
  "ai": {
    "enabled": false
  }
}
```

Your file is deep-merged onto these defaults at load time, so a future release
can add settings without breaking your existing config, and a partial file never
crashes startup. A file that is not valid JSON is copied aside as
`watcher_settings.json.corrupt` and the defaults are used, with a loud log line.

---

## Alert template

The default alert looks like this:

```
🟥 𝐌𝐀𝐊𝐈𝐌𝐀 𝐀𝐋𝐄𝐑𝐓
━━━━━━━━━━━━━━━━━━
🕒 2026-08-24 14:03:11 UTC
🧠 Reason: 🟥 Mention | 🔍 Keyword: fuel card
👥 Group: Dispatch Team
🔗 Group Link: https://t.me/c/1234567890
👤 From: Alex Driver (@alexdriver)
🧷 Keyword hits: fuel card
📝 Message:
My fuel card is declining and I only have 8% left.
──────────────────
👉 Message: https://t.me/c/1234567890/48213
```

Available placeholders:

| Placeholder | Value |
| --- | --- |
| `{{timestamp}}` | Message time, formatted per `formatting.timestamp_format` |
| `{{reasons}}` | `🟥 Mention`, `🧷 Reply`, `🔍 Keyword: <word>`, joined by ` \| ` |
| `{{group}}` | Group title |
| `{{group_link}}` | `https://t.me/<username>` or `https://t.me/c/<id>` |
| `{{sender}}` | Display name plus `(@username)` when there is one |
| `{{sender_name}}` | Display name only |
| `{{sender_username}}` | `@username` or `-` |
| `{{sender_id}}` | Numeric sender ID |
| `{{chat_id}}` | Numeric chat ID |
| `{{message_id}}` | Numeric message ID |
| `{{keyword_hits}}` | Matched keywords, capped by `max_keyword_preview` |
| `{{message_text}}` | Message text, truncated to `max_message_chars` |
| `{{message_link}}` | Direct link to the message |
| `{{category}}` `{{severity}}` `{{summary}}` `{{requires_action}}` `{{unit}}` | Filled once AI classification is enabled; `-` otherwise |

Message links are built correctly for both kinds of group: public groups get
`https://t.me/<username>/<message_id>`, private supergroups get
`https://t.me/c/<internal_id>/<message_id>` with the `-100` prefix stripped.
Legacy basic groups have no link form in Telegram, so those fields show `-`.

Alerts are sent as plain text with no parse mode, so message content containing
`*`, `_` or `[` can never break formatting or inject markup.

---

## Hostinger VPS deployment

Tested against a plain Ubuntu 22.04 / 24.04 VPS. Run as root, or prefix with
`sudo`.

> A step-by-step Hostinger runbook, including SSH, snapshots, private-repo
> access and day-2 operations, is in [docs/HOSTINGER.md](docs/HOSTINGER.md).

### 1. Install Docker and git

Ubuntu's own repositories do **not** carry `docker-compose-plugin` on 22.04, so
use Docker's official install script — it works on 22.04 and 24.04 alike and
installs the `docker compose` plugin:

```bash
apt update
apt install -y git curl
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
```

Verify both are present before continuing:

```bash
docker --version
docker compose version
```

### 2. Clone the repository

```bash
cd /opt
git clone <YOUR_REPO_URL> telegram-watcher
cd telegram-watcher
chmod +x scripts/*.sh
```

For a private repo, either use a deploy key or clone over HTTPS with a personal
access token.

### 3. Create the environment file

`.env` exists only on the server. It is never committed and never baked into
the image.

```bash
cp .env.example .env
nano .env
```

Fill in:

```
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_PHONE=
TELEGRAM_BOT_TOKEN=
ADMIN_USER_IDS=
```

Lock it down:

```bash
chmod 600 .env
```

### 4. Authenticate the Telegram user session, once

```bash
docker compose run --rm makima python -m app.auth_user
```

Enter the login code Telegram sends to your app, and your 2FA password if you
have one. This writes `sessions/user_session.session`.

### 5. Start the bot chat

Open Telegram, find your bot, press **Start**. Without this, Telegram refuses to
let the bot message you.

### 6. Launch

```bash
docker compose up -d
docker compose ps
docker compose logs -f --tail=200
```

You should see:

```
MAKIMA TELEGRAM WATCHER ONLINE
User: Your Name (@yourname)
Bot: @makima_alerts_bot
Keywords loaded: 26
Modes: mentions=on, replies=on, keywords=on, ai=off
```

and in Telegram:

```
🟥 MAKIMA watcher is running.

Use /help in private chat.
```

`restart: unless-stopped` means the container comes back automatically after a
crash or a VPS reboot.

---

## Updating

Push your changes to GitHub, then on the VPS:

```bash
cd /opt/telegram-watcher
./scripts/deploy.sh
```

That pulls, rebuilds and restarts. It never touches `.env`, `sessions/`,
`data/` or `logs/` — those are git-ignored on disk and bind-mounted into the
container.

Other helpers:

| Script | Does |
| --- | --- |
| `./scripts/deploy.sh` | `git pull` → rebuild → restart → status |
| `./scripts/start.sh` | Start in the background |
| `./scripts/stop.sh` | Stop (volumes untouched) |
| `./scripts/restart.sh` | Restart without rebuilding |
| `./scripts/logs.sh` | Follow the last 200 log lines |
| `./scripts/auth.sh` | Re-run the interactive Telegram login |

If `git pull --ff-only` fails, you have local edits on the server. Inspect with
`git status`, then either `git stash` them or commit them properly. The script
deliberately refuses to guess.

Later, this can become a GitHub Actions job — push to `main` → SSH to the VPS →
`./scripts/deploy.sh`. Nothing in the project depends on that; manual deploy
works fine and is the recommended starting point.

---

## Migrating from the old watcher

The old layout kept everything in one directory:

```
.env
bot_session.session
telegram_bot.session
telegram_user.session
user_session.session
watcher.py
watcher_settings.json
keywords.txt
```

The new project uses only two session files:

```
sessions/user_session.session
sessions/bot_session.session
```

**Nothing is deleted automatically.** Leave the old directory in place until the
new one has been running happily for a few days.

### Option A — reuse the old sessions (fastest)

Only works if the old sessions were created with the *same* `api_id` /
`api_hash`. Stop the old watcher first, or you will get a locked database.

```bash
# stop the old process first
cd /opt/telegram-watcher
mkdir -p sessions

cp /opt/old-watcher/user_session.session sessions/user_session.session
cp /opt/old-watcher/bot_session.session  sessions/bot_session.session

chmod 600 sessions/*.session
docker compose up -d
docker compose logs -f
```

If the log shows `The user session is not authorised` or an auth-key error, the
sessions are not compatible — delete them and use Option B.

### Option B — authenticate cleanly (recommended)

```bash
cd /opt/telegram-watcher
rm -f sessions/user_session.session sessions/bot_session.session
docker compose run --rm makima python -m app.auth_user
docker compose up -d
```

### Bringing over your keywords

```bash
cp /opt/old-watcher/keywords.txt /opt/telegram-watcher/data/keywords.txt
```

`watcher_settings.json` can be copied too — anything missing is filled in from
the defaults by the deep merge. Restart, or just send `/reload`.

### Retiring the old directory

Once you are confident:

```bash
# revoke the old sessions from Telegram first:
# Telegram -> Settings -> Devices -> terminate the old session
rm -rf /opt/old-watcher
```

---

## Session file safety

`*.session` files are **live authentication material**. Anyone who copies one
can read and send messages as your account, without your password and without
triggering a login code.

**Do not:**

```bash
cat *.session              # do not print them
git add *.session          # do not commit them
```

Do not upload them, paste them into a chat, attach them to an issue, or include
them in a screenshot or a support ticket. `.gitignore` and `.dockerignore` both
exclude them, and `.gitattributes` marks them binary, but none of that helps
against a manual `git add -f`.

Keep them tight:

```bash
chmod 700 sessions
chmod 600 sessions/*.session
```

**If a session file is ever exposed:**

1. Telegram → **Settings → Devices** → terminate the session (look for a device
   named `MAKIMA watcher`).
2. Delete the file: `rm sessions/user_session.session`
3. Re-authenticate: `docker compose run --rm makima python -m app.auth_user`

**If your bot token is exposed:** BotFather → `/mybots` → your bot → **API
Token** → **Revoke current token**, then update `.env` and redeploy.

**If your `api_hash` is exposed:** my.telegram.org lets you delete the
application and create a new one. Re-authentication is required afterwards.

MAKIMA also runs a redaction filter over its own logging, so a token or hash
cannot reach `logs/makima.log` even by accident.

---

## Future AI classification

`app/ai_classifier.py` is the hook for turning this:

> "My fuel card is declining and I only have 8% left."

into this:

```json
{
  "important": true,
  "category": "FUEL",
  "severity": "CRITICAL",
  "summary": "Fuel card declined and driver cannot continue.",
  "requires_action": true,
  "unit": "155"
}
```

instead of just noticing the word `fuel`.

The intended flow:

```
Incoming Telegram Message
        |
Rules / Keywords          <- cheap, always runs
        |
AI Analysis               <- optional
        |
Category -> Severity -> Action Required?
        |
Send Alert
```

Categories: `CLAIMS`, `SAFETY`, `FUEL`, `INSURANCE`, `MAINTENANCE`, `DRIVER`,
`LEGAL`, `PERMITS`, `GENERAL`.
Severities: `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`.

**Current state.** With `ai.enabled` set to `false` (the default), no analysis
runs and no network call is made:

```python
{"enabled": False, "important": True, ...}
```

Setting `ai.enabled` to `true` without registering a backend uses a free,
rule-based categoriser, so `{{category}}` and `{{severity}}` become meaningful
in your template right away.

**Plugging in a real model later:**

```python
from app.ai_classifier import register_backend

async def my_backend(text: str, context: dict) -> dict:
    ...  # call your provider
    return {
        "important": True,
        "category": "FUEL",
        "severity": "CRITICAL",
        "summary": "Fuel card declined.",
        "requires_action": True,
        "unit": "155",
    }

register_backend(my_backend)
```

Backends are wrapped in a 20-second timeout, and any failure degrades to the
rule-based result — a broken or slow model can never stop an alert being
delivered. The core watcher has no AI dependency of any kind.

---

## Project layout

```
telegram-watcher/
├── app/
│   ├── __init__.py
│   ├── __main__.py           # python -m app
│   ├── main.py               # startup, shutdown, signal handling
│   ├── config.py             # .env loading, paths, validation
│   ├── clients.py            # the two Telethon clients + reconnect monitor
│   ├── watcher.py            # mention / reply / keyword detection
│   ├── bot_commands.py       # the private control panel
│   ├── alerts.py             # alert formatting + delivery queue
│   ├── keywords.py           # keyword storage and matching
│   ├── settings.py           # settings with deep-merge and atomic writes
│   ├── utils.py              # links, templates, atomic file writes
│   ├── logging_config.py     # rotating log + secret redaction
│   ├── ai_classifier.py      # optional classification layer
│   ├── auth_user.py          # one-time interactive login
│   └── health.py             # python -m app.health
│
├── data/
│   ├── defaults/             # tracked seeds
│   │   ├── keywords.txt
│   │   └── watcher_settings.json
│   ├── keywords.txt          # live, git-ignored, bot-editable
│   └── watcher_settings.json # live, git-ignored, bot-editable
│
├── docs/
│   └── HOSTINGER.md          # step-by-step VPS runbook
│
├── sessions/                 # Telethon sessions (git-ignored)
├── logs/                     # makima.log (git-ignored)
├── scripts/                  # deploy, start, stop, restart, logs, auth
│
├── .env.example
├── .gitignore
├── .gitattributes
├── .dockerignore
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── README.md
└── LICENSE
```

### Health check

```bash
docker compose exec makima python -m app.health
```

Verifies that the settings and keyword files exist and parse, the required
environment variables are set, the sessions and logs directories are writable,
and the user session is present. Exits `0` when healthy. It never contacts
Telegram, so it is fast and cannot trip a rate limit. Docker runs it every five
minutes as the container's `HEALTHCHECK`.

### Logging

Logs go to `logs/makima.log` (rotating, 2 MB × 5) and to stdout for
`docker compose logs`. Levels used: `INFO` for lifecycle and alerts, `WARNING`
for recoverable problems (flood waits, unresolvable replies, disconnects),
`ERROR` for things that need you. Exceptions are logged with a traceback —
nothing is silently swallowed.

---

## Troubleshooting

### `database is locked`

Two processes are using the same session file. Almost always an old copy of the
watcher still running.

```bash
docker compose down
pkill -f "app.main" || true
docker compose up -d
```

Never run two containers, or a container and a bare `python -m app.main`,
against the same `sessions/` directory.

### Session file permission problems

```bash
ls -la sessions/
chown -R "$(id -u):$(id -g)" sessions data logs
chmod 700 sessions && chmod 600 sessions/*.session
```

Symptoms are `unable to open database file` or `attempt to write a readonly
database` at startup.

### `FloodWaitError`

Telegram is rate-limiting you. MAKIMA handles this itself: short waits are slept
through, alerts are retried, and anything over 15 minutes is logged and dropped
rather than blocking the queue. If it happens constantly, you are probably
restarting the container in a loop — check `docker compose ps` and the logs.

### Invalid API ID/hash

```
Startup failed: Telegram rejected TELEGRAM_API_ID / TELEGRAM_API_HASH
```

Re-copy both from my.telegram.org. Watch for stray quotes, trailing spaces, or a
truncated hash — it must be exactly 32 hex characters. MAKIMA validates the
shape at startup and tells you before Telegram does.

### Wrong bot token

```
Startup failed: TELEGRAM_BOT_TOKEN was rejected by Telegram
```

Get a fresh one from BotFather (`/mybots` → your bot → API Token). If you
revoked the token, the old one is dead permanently.

### User not authorized

```
The user session is not authorised. Run this once, interactively: ...
```

The session file is missing or was invalidated (you terminated it from
Telegram's Devices screen, or it was created with different API credentials).

```bash
docker compose run --rm makima python -m app.auth_user
```

### Docker container keeps restarting

```bash
docker compose logs --tail=100
```

The last error before each restart is the real cause. The usual suspects are a
missing user session, a bad token, or an unwritable `sessions/` volume. Fix it,
then `docker compose up -d`.

### Telegram code not arriving

The login code goes to your **Telegram app** first, not by SMS — check other
devices where you are already logged in, including Saved Messages. If you are
signed in nowhere, Telegram may take a minute before falling back to SMS.
Requesting codes repeatedly triggers a flood wait, so wait it out rather than
retrying.

### Telegram 2FA

If your account has two-step verification, `app.auth_user` asks for the password
after the code. Input is hidden. If you have forgotten it, reset it from a
device where you are already logged in: Settings → Privacy and Security →
Two-Step Verification.

### Duplicate sessions

Running the same session from two IPs at once makes Telegram invalidate the auth
key:

```
AuthKeyDuplicatedError
```

Stop every copy, delete `sessions/user_session.session`, and re-authenticate.
Run MAKIMA in exactly one place.

### Network disconnection

Telethon reconnects on its own, indefinitely. MAKIMA's connection monitor logs
the state changes so an outage is visible instead of looking like silent
failure:

```
Telegram disconnected (user client)
Reconnect attempted (user client)
Telegram reconnected (user client)
```

No action needed unless it repeats for a long time — then check the VPS network
and `docker compose logs`.

### Alerts are not arriving

Work down this list:

1. Send `/status` to the bot. No reply → the bot client is not running, or you
   are not authorised (the reply tells you your user ID).
2. Have you pressed **Start** in the bot's private chat? Bots cannot message
   first.
3. Check `ADMIN_USER_IDS` — a typo there sends alerts to the wrong ID, and the
   logs will show `Recipient ... is unreachable`.
4. Check the modes: `/status` shows whether mentions, replies and keywords are
   each `ON`.
5. Remember that your **own** messages never trigger alerts.
6. `docker compose logs --tail=100` — every delivery failure is logged with a
   reason.

### A keyword is not matching

Matching is on word boundaries, so `claim` matches `claim` and `claim.` but not
`disclaimer` or `claims` (`claims` is a separate keyword, and it ships by
default). Add the variants you need with `/addkeyword`. Keywords are lowercased
automatically; matching is case-insensitive.

---

## License

MIT — see [LICENSE](LICENSE).
