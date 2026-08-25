# Deploying MAKIMA on a Hostinger VPS — full walkthrough

This is a complete, do-this-in-order guide. It assumes you have never deployed a
Docker application to a Linux server before, and explains what each command does
rather than just listing it.

**How to use this page.** Every step has three parts:

1. **Run** — the command to paste.
2. **Expect** — what a successful result looks like.
3. **If it goes wrong** — what to do when it doesn't.

If a step's output doesn't match "Expect", stop and resolve it before moving on.
Continuing past a broken step is what turns a 20-minute deployment into a
two-hour one.

**Two values you must substitute throughout:**

| Placeholder | Replace with |
| --- | --- |
| `YOUR_VPS_IP` | Your server's IP address, from hPanel |
| `YOUR_GITHUB_USERNAME` | Your GitHub account name |

**Time:** about 25 minutes, most of it waiting on downloads.

---

## Contents

**Setup**
- [Step 0 — Prerequisites checklist](#step-0--prerequisites-checklist)
- [Step 1 — Get your VPS details from hPanel](#step-1--get-your-vps-details-from-hpanel)
- [Step 2 — Take a snapshot](#step-2--take-a-snapshot)
- [Step 3 — Connect over SSH](#step-3--connect-over-ssh)
- [Step 4 — Prepare the server](#step-4--prepare-the-server)
- [Step 5 — Install Docker](#step-5--install-docker)

**Deploy**
- [Step 6 — Push the code to GitHub](#step-6--push-the-code-to-github)
- [Step 7 — Clone the repo on the VPS](#step-7--clone-the-repo-on-the-vps)
- [Step 8 — Create the .env file](#step-8--create-the-env-file)
- [Step 9 — Build and smoke-test](#step-9--build-and-smoke-test)
- [Step 10 — Authenticate your Telegram account](#step-10--authenticate-your-telegram-account)
- [Step 11 — Press Start in the bot chat](#step-11--press-start-in-the-bot-chat)
- [Step 12 — Launch](#step-12--launch)
- [Step 13 — Test it for real](#step-13--test-it-for-real)
- [Step 14 — Set ADMIN_USER_IDS](#step-14--set-admin_user_ids)
- [Step 15 — Verify it survives a reboot](#step-15--verify-it-survives-a-reboot)

**Living with it**
- [Operations cheat sheet](#operations-cheat-sheet)
- [The update workflow](#the-update-workflow)
- [Backups](#backups)
- [Migrating from the old watcher](#migrating-from-the-old-watcher)
- [Troubleshooting matrix](#troubleshooting-matrix)
- [Hostinger-specific notes](#hostinger-specific-notes)

---

# Setup

## Step 0 — Prerequisites checklist

Tick all six before you start. Missing one halfway through is annoying.

| # | Thing | Where to get it | Looks like |
| --- | --- | --- | --- |
| 1 | hPanel login | hostinger.com | — |
| 2 | VPS IP + root password | hPanel → VPS | `203.0.113.10` |
| 3 | `api_id` and `api_hash` | <https://my.telegram.org> → API development tools | `35221038` / 32 hex chars |
| 4 | Bot token | @BotFather → `/newbot`, or `/mybots` for an existing one | `123456789:AAE...` |
| 5 | Your phone, Telegram open | — | You get a login code in Step 10 |
| 6 | A GitHub account | github.com | — |

**Have the phone within reach for Step 10.** The Telegram login code expires in
a couple of minutes and re-requesting it too often triggers a rate limit.

---

## Step 1 — Get your VPS details from hPanel

Log into hPanel and open **VPS** → your server.

**What you need from this page:**

- **IP address** — shown on the Overview page. Write it down.
- **Root password** — set when the VPS was created. If you don't remember it,
  find the SSH access / root password section in the VPS menu and reset it. The
  change applies within about a minute.
- **OS template** — the Overview page names it. This guide assumes Ubuntu 22.04
  or 24.04.

> hPanel's menu labels change between redesigns. If a name here doesn't match
> what you see, look for the equivalent — the concepts (IP, root password,
> snapshots, firewall, browser terminal) are always present somewhere in the VPS
> section.

**If you chose Hostinger's "Ubuntu with Docker" application template**, Docker is
already installed. You still run Step 5's verification commands; you just skip
the install itself.

---

## Step 2 — Take a snapshot

Before changing anything, create a rollback point: hPanel → VPS → **Snapshots** →
**Create snapshot**. It takes two or three minutes.

Hostinger keeps one manual snapshot per VPS — making a new one replaces the old.
If anything in this guide goes badly wrong, restoring it returns the server to
exactly this moment.

This is optional but costs you nothing except a few minutes.

---

## Step 3 — Connect over SSH

Windows 11 has an SSH client built in. Open **PowerShell** — the normal Windows
one, not this Claude Code terminal.

**Run:**

```bash
ssh root@YOUR_VPS_IP
```

**Expect:** on the very first connection, a fingerprint prompt:

```
The authenticity of host '203.0.113.10 (203.0.113.10)' can't be established.
ED25519 key fingerprint is SHA256:aBcDeFg...
Are you sure you want to continue connecting (yes/no/[fingerprint])?
```

Type `yes` and press Enter. Then it asks for the password:

```
root@203.0.113.10's password:
```

Type the root password. **Nothing appears as you type** — no dots, no asterisks.
That's normal, not a frozen terminal. Press Enter.

You land on a prompt like:

```
root@srv123456:~#
```

Everything from here until Step 6 is typed at that prompt, on the server.

**If it goes wrong:**

| Symptom | Fix |
| --- | --- |
| `Connection refused` | The VPS is off or still provisioning. Check hPanel shows it Running |
| `Connection timed out` | Wrong IP, or a firewall rule is blocking port 22 |
| `Permission denied (publickey,password)` | Wrong password. Reset it in hPanel |
| `WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED` | The VPS was rebuilt. Run `ssh-keygen -R YOUR_VPS_IP` on Windows, then reconnect |

### Optional: switch to key-based login

Public IPs get brute-forced constantly. Key auth is both safer and more
convenient. On **Windows**, in PowerShell:

```bash
ssh-keygen -t ed25519 -C "makima-vps"
```

Press Enter three times to accept the defaults. Then display the public key:

```bash
type $env:USERPROFILE\.ssh\id_ed25519.pub
```

Copy the whole line and add it in hPanel → VPS → **SSH Keys**. After that,
`ssh root@YOUR_VPS_IP` connects with no password.

Only once you've confirmed key login works — in a *second* PowerShell window,
keeping the first one open — you can disable passwords entirely:

```bash
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config && systemctl restart ssh
```

Keep that first session open until you've proven a fresh key login succeeds. If
you lock yourself out, the only way back in is a hPanel rebuild, which wipes the
disk.

---

## Step 4 — Prepare the server

### 4a. Update the system

**Run:**

```bash
apt update && apt upgrade -y
```

**Expect:** a few minutes of package downloads. If a purple dialog asks about
configuration files or service restarts, accept the default (Enter / "keep the
local version currently installed").

If it finishes saying a reboot is required:

```bash
reboot
```

Your SSH session drops — that's expected. Wait 30 seconds, reconnect with
`ssh root@YOUR_VPS_IP`.

### 4b. Check what you're working with

**Run:**

```bash
cat /etc/os-release | head -2 && echo "---" && free -h && echo "---" && df -h / && echo "---" && nproc
```

**Expect** something like:

```
PRETTY_NAME="Ubuntu 22.04.4 LTS"
NAME="Ubuntu"
---
               total        used        free      shared  buff/cache   available
Mem:           3.8Gi       248Mi       3.3Gi       1.0Mi       291Mi       3.3Gi
Swap:             0B          0B          0B
---
Filesystem      Size  Used Avail Use% Mounted on
/dev/sda1        49G  2.1G   45G   5% /
---
1
```

**What you need:** roughly 1 GB of free RAM and 5 GB of free disk. Any Hostinger
KVM plan clears this comfortably. MAKIMA idles at about 80–120 MB of RAM.

### 4c. Add swap if you have none and little RAM

Only needed if `free -h` showed `Swap: 0B` **and** under 2 GB total memory. The
Docker build is the only memory-hungry moment in this whole process.

```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

Verify with `free -h` — you should now see 2.0Gi of swap.

---

## Step 5 — Install Docker

**Important:** do **not** use `apt install docker-compose-plugin`. That package
does not exist in Ubuntu 22.04's repositories and the command fails with
`E: Unable to locate package docker-compose-plugin`.

### 5a. Check whether Docker is already there

```bash
docker --version
```

If that prints a version, you're on Hostinger's Docker template — skip to 5c.
If it says `command not found`, continue.

### 5b. Install

**Run:**

```bash
apt install -y git curl && curl -fsSL https://get.docker.com | sh
```

This downloads and runs Docker's official installation script, which adds
Docker's apt repository and installs the engine, the CLI, buildx and the
`docker compose` plugin — all the pieces, in one step, correctly, on both 22.04
and 24.04.

**Expect:** two to four minutes of output ending with a summary block mentioning
Docker Engine and a note about running Docker as a non-root user (which you can
ignore — you are root).

Then make sure it starts on boot:

```bash
systemctl enable --now docker
```

### 5c. Verify — do not skip this

**Run:**

```bash
docker --version && docker compose version && docker run --rm hello-world
```

**Expect:**

```
Docker version 27.3.1, build ce12230
Docker Compose version v2.29.7

Hello from Docker!
This message shows that your installation appears to be working correctly.
...
```

All three must succeed. If `docker compose version` says
`docker: 'compose' is not a docker command`, the plugin didn't install — re-run
5b. If `hello-world` fails, the daemon isn't healthy and nothing later will work;
check `systemctl status docker`.

---

# Deploy

## Step 6 — Push the code to GitHub

This part happens on **Windows**, not the VPS. Open a second PowerShell window
(leave the SSH one open).

### 6a. Create an empty repository

On github.com click **New repository**:

- Name: `telegram-watcher`
- Visibility: **Private**
- **Do not** tick "Add a README", "Add .gitignore", or "Choose a license" — the
  project already has all three, and adding them creates a conflict on first
  push.

### 6b. Push

**Run** (in PowerShell, on Windows):

```bash
cd "C:\cloude experiments\telegram-watcher" && git remote add origin https://github.com/YOUR_GITHUB_USERNAME/telegram-watcher.git && git push -u origin main
```

You'll be prompted to sign in to GitHub — a browser window opens, or Git asks
for a username and personal access token.

**Expect:**

```
Enumerating objects: 40, done.
...
To https://github.com/YOUR_GITHUB_USERNAME/telegram-watcher.git
 * [new branch]      main -> main
branch 'main' set up to track 'origin/main'.
```

### 6c. Checkpoint — verify no secrets leaked

Open the repository on GitHub in a browser and check:

- There is **no `.env` file** in the file list. (`.env.example` is fine and
  expected — it holds no values.)
- `sessions/` contains only `.gitkeep`.
- `data/` contains only `defaults/`.

**If you see a `.env` file, stop.** Delete the repository, fix `.gitignore`
locally, and push again. A `.env` in a repo means your bot token and API hash are
now in git history, and deleting the file in a later commit does not remove them.

---

## Step 7 — Clone the repo on the VPS

Back in the SSH window.

### If your repository is public

```bash
cd /opt && git clone https://github.com/YOUR_GITHUB_USERNAME/telegram-watcher.git && cd /opt/telegram-watcher
```

### If your repository is private (recommended)

A private repo needs credentials on the server. The clean way is a **deploy
key** — an SSH key that grants read-only access to this one repository, and
nothing else in your GitHub account.

**7a. Generate the key on the VPS:**

```bash
ssh-keygen -t ed25519 -C "makima-deploy" -f /root/.ssh/github_deploy -N "" && cat /root/.ssh/github_deploy.pub
```

**Expect** a single line starting `ssh-ed25519 AAAA...` and ending
`makima-deploy`. Select and copy the whole line.

**7b. Add it to GitHub:** your repo → **Settings** → **Deploy keys** → **Add
deploy key**. Title it `hostinger-vps`, paste the key, and leave **"Allow write
access" unchecked** — the server only ever needs to read.

**7c. Tell SSH to use that key for GitHub:**

```bash
printf 'Host github.com\n  IdentityFile /root/.ssh/github_deploy\n  IdentitiesOnly yes\n' >> /root/.ssh/config && chmod 600 /root/.ssh/config
```

**7d. Clone:**

```bash
cd /opt && git clone git@github.com:YOUR_GITHUB_USERNAME/telegram-watcher.git && cd /opt/telegram-watcher
```

The first connection asks you to trust `github.com` — type `yes`.

### Checkpoint (either path)

```bash
ls -la /opt/telegram-watcher && ls -la /opt/telegram-watcher/scripts
```

**Expect:** `app/`, `data/`, `docs/`, `logs/`, `scripts/`, `sessions/`,
`Dockerfile`, `docker-compose.yml`, `.env.example`, `README.md`.

The scripts should already show `-rwxr-xr-x` (executable) because the executable
bit is stored in git. If they show `-rw-r--r--` instead, fix it:

```bash
chmod +x /opt/telegram-watcher/scripts/*.sh
```

---

## Step 8 — Create the .env file

`.env` holds your credentials. It exists **only on this server** — it is
git-ignored, excluded from the Docker image, and never leaves the box.

You have two options. The second is better because it eliminates typos.

### Option A — type it on the server

```bash
cd /opt/telegram-watcher && cp .env.example .env && nano .env
```

`nano` is a simple text editor. Arrow keys move the cursor; there is no mouse.
Fill in the five values:

```
TELEGRAM_API_ID=35221038
TELEGRAM_API_HASH=your_32_character_hash_here
TELEGRAM_PHONE=+998920103240
TELEGRAM_BOT_TOKEN=123456789:AAE...
ADMIN_USER_IDS=
```

Leave `ADMIN_USER_IDS` empty — Step 14 fills it in once you know your numeric ID.

To save and quit: **Ctrl+O**, then **Enter**, then **Ctrl+X**.

### Option B — copy the one you already have (recommended)

A working `.env` already exists on your Windows machine. Copy it up instead of
retyping. **In PowerShell on Windows:**

```bash
scp "C:\cloude experiments\telegram-watcher\.env" root@YOUR_VPS_IP:/opt/telegram-watcher/.env
```

This transfers over the same encrypted SSH channel you're already using. No
retyping means no typos in a 32-character hash.

### Lock it down and verify

Back in the SSH session:

```bash
cd /opt/telegram-watcher && chmod 600 .env && grep -v '^#' .env | grep -v '^$'
```

**Expect** your five variables printed, each with a value except
`ADMIN_USER_IDS=`. Check carefully for:

- **No quotes** around values — `TELEGRAM_API_HASH=abc123` not `="abc123"`
- **No trailing spaces** after a value
- **No spaces around `=`**
- The API hash is exactly 32 characters
- The phone starts with `+` and country code, no spaces or dashes

MAKIMA validates all of this at startup and tells you specifically what's wrong,
but catching it now saves a round trip.

---

## Step 9 — Build and smoke-test

Before doing anything interactive, confirm the image builds and the code imports
cleanly. This is the first time this code runs anywhere, so it's worth its own
step.

**Run:**

```bash
cd /opt/telegram-watcher && docker compose build
```

**Expect:** three to five minutes on the first build. Docker downloads the
`python:3.12-slim` base image, then installs Telethon and python-dotenv. The last
lines look like:

```
 => => naming to docker.io/library/makima-watcher:latest
```

Now verify every module imports — this catches syntax errors and typos without
touching Telegram:

```bash
docker compose run --rm makima python -c "import app.main, app.watcher, app.bot_commands, app.alerts, app.health; print('IMPORTS OK')"
```

**Expect:** `IMPORTS OK`, and nothing else.

**If it goes wrong:** a `SyntaxError` or `ImportError` traceback names the exact
file and line. Copy the whole traceback — that's what's needed to fix it. Do not
continue to Step 10 until this prints `IMPORTS OK`.

---

## Step 10 — Authenticate your Telegram account

This is the one interactive step and the one people most often stumble on. Read
this whole section before running the command, and have your phone in your hand.

**What this does:** logs Telethon into your personal Telegram account once, and
saves the result to `sessions/user_session.session`. Every later start reuses
that file, which is why the container can restart unattended forever afterwards.

**Run:**

```bash
cd /opt/telegram-watcher && docker compose run --rm makima python -m app.auth_user
```

**Expect, first:**

```
MAKIMA - Telegram user authentication
Session file: /app/sessions/user_session.session

Requesting a login code for +998920103240 ...
Telegram has sent a code to your Telegram app (not SMS, usually).
Telegram login code:
```

### Where the code actually arrives

**It is not an SMS.** Telegram sends the login code as a message *inside the
Telegram app*, in the official chat named **"Telegram"**, on any device where
you're already signed in. Check your phone first, and Telegram Desktop if you use
it.

Only if you're signed in nowhere at all does Telegram fall back to SMS, and even
then it waits a minute or two first.

Type the code (digits only) and press Enter.

### If two-step verification is enabled

```
Two-factor authentication is enabled on this account.
2FA password (hidden):
```

Type your Telegram 2FA password. **Nothing echoes** — no dots, no asterisks.
Press Enter.

### Success

```
Signed in as Your Name (@yourname) (id 123456789).
Session saved to /app/sessions/user_session.session

Next steps:
  1. Open a private chat with your MAKIMA bot and press Start.
  2. Launch the watcher:  docker compose up -d
```

**Write down that numeric id** — `123456789` in the example. You need it in
Step 14.

### Checkpoint

```bash
ls -la /opt/telegram-watcher/sessions/ && chmod 700 /opt/telegram-watcher/sessions && chmod 600 /opt/telegram-watcher/sessions/*.session
```

**Expect** `user_session.session`, somewhere around 20–60 KB. The `chmod`
commands make it readable only by root — it is login credentials in file form.

### If Step 10 goes wrong

| Message | Cause | Fix |
| --- | --- | --- |
| `Telegram does not recognise that phone number` | Wrong format | Must be `+998920103240` — plus sign, country code, no spaces or dashes |
| `That login code is not correct` | Typo, or you used a previously-sent code | Re-run the command and use the newest code |
| `That login code expired` | Took too long | Re-run and enter it promptly |
| `Telegram is rate-limiting logins. Wait N seconds` | Too many attempts | Wait the full time. Retrying sooner increases the wait |
| `TELEGRAM_API_ID / TELEGRAM_API_HASH were rejected` | Bad credentials in `.env` | Re-copy both from my.telegram.org |
| Typing does nothing | Terminal isn't passing input | Use a real SSH client, not hPanel's browser terminal |
| Code never arrives anywhere | Signed out everywhere | Wait two minutes for the SMS fallback |

### A note about logging in from a data-centre IP

Your other Telegram devices will show a **new login notification**, and if the
account has never been used outside your home country you may see extra
verification. This is expected.

In Telegram → **Settings → Devices**, the new session appears as **MAKIMA
watcher**. Do not terminate it — that *is* your watcher. Terminating it forces
you to redo this step.

**On automation risk, honestly:** running a user account through Telethon is what
this project does by design, and passive reading is low-risk behaviour. Telegram
does limit accounts that act abnormally, but MAKIMA only *reads* groups and never
posts to them, which keeps you well inside normal usage. Don't bolt
auto-replying or bulk messaging onto it later without thinking about that.

---

## Step 11 — Press Start in the bot chat

Open Telegram on your phone. Search for your bot's username. Open the chat and
press **Start** (or send `/start`).

**This is not optional.** Telegram forbids a bot from sending the first message
to a user who has never started it. Skip this and MAKIMA runs perfectly while
delivering nothing, and the log fills with:

```
Cannot message 123456789 (UserIsBlockedError). Open a private chat with the bot and press Start.
```

At this point the bot won't reply to `/start` yet — it isn't running. You're just
opening the conversation so Telegram permits messages later.

---

## Step 12 — Launch

**Run:**

```bash
cd /opt/telegram-watcher && docker compose up -d && docker compose ps
```

**Expect:**

```
[+] Running 1/1
 ✔ Container makima-watcher  Started

NAME              IMAGE                    STATUS                    PORTS
makima-watcher    makima-watcher:latest    Up 5 seconds (starting)
```

`(starting)` becomes `(healthy)` after about 30 seconds — that's the health check
completing its first run.

**Now watch the logs:**

```bash
cd /opt/telegram-watcher && docker compose logs -f --tail=100
```

**Expect this sequence** (timestamps and names will differ):

```
2026-08-25 12:00:01 | INFO | makima.main     | MAKIMA watcher starting (v1.0.0)
2026-08-25 12:00:01 | INFO | makima.settings | Settings file /app/data/watcher_settings.json not found; creating it from defaults
2026-08-25 12:00:01 | INFO | makima.settings | Settings loaded from /app/data/watcher_settings.json
2026-08-25 12:00:01 | INFO | makima.keywords | Keyword file /app/data/keywords.txt not found; creating it from defaults
2026-08-25 12:00:01 | INFO | makima.keywords | Loaded 26 keywords from /app/data/keywords.txt
2026-08-25 12:00:03 | INFO | makima.clients  | User account authenticated
2026-08-25 12:00:04 | INFO | makima.clients  | Bot authenticated
2026-08-25 12:00:04 | INFO | makima.main     | ADMIN_USER_IDS is empty; defaulting to the watching account (id 123456789)
2026-08-25 12:00:04 | INFO | makima.alerts   | Alert recipients: [123456789]
2026-08-25 12:00:04 | INFO | makima.alerts   | Alert dispatcher started
2026-08-25 12:00:04 | INFO | makima.watcher  | Message watcher registered on the user client
2026-08-25 12:00:04 | INFO | makima.commands | Bot command handler registered (13 commands)
2026-08-25 12:00:04 | INFO | makima.main     | ==========================================================
2026-08-25 12:00:04 | INFO | makima.main     | MAKIMA TELEGRAM WATCHER ONLINE (v1.0.0)
2026-08-25 12:00:04 | INFO | makima.main     | User: Your Name (@yourname)
2026-08-25 12:00:04 | INFO | makima.main     | Bot: @makima_alerts_bot
2026-08-25 12:00:04 | INFO | makima.main     | Keywords loaded: 26
2026-08-25 12:00:04 | INFO | makima.main     | Modes: mentions=on, replies=on, keywords=on, ai=off
2026-08-25 12:00:04 | INFO | makima.main     | ==========================================================
```

The two "not found; creating it from defaults" lines appear only on the first
run. That's the live `keywords.txt` and `watcher_settings.json` being seeded from
`data/defaults/`.

**And in Telegram**, your bot messages you:

```
🟥 MAKIMA watcher is running.

Use /help in private chat.
```

Press **Ctrl+C** to stop following the logs. That stops the log view, not MAKIMA.

### If Step 12 goes wrong

**The container keeps restarting.** Check the last error before each restart:

```bash
cd /opt/telegram-watcher && docker compose logs --tail=50
```

The most common cause by far: you ran `docker compose up -d` *before* Step 10.
The container starts, finds no authorised session, exits, and `restart:
unless-stopped` starts it again in a loop. The log says exactly that:

```
Startup failed: The user session is not authorised. Run this once, interactively: ...
```

Fix it by stopping, authenticating, then starting:

```bash
cd /opt/telegram-watcher && docker compose down && docker compose run --rm makima python -m app.auth_user && docker compose up -d
```

**No startup message in Telegram** but the log shows ONLINE — you skipped
Step 11. Press Start in the bot chat, then `docker compose restart`.

---

## Step 13 — Test it for real

Verify the whole chain, not just that the process is up.

**Test 1 — the bot responds.** Send `/status` to your bot in Telegram.

**Expect:**

```
⚙️ MAKIMA STATUS

Mentions: ON
Replies: ON
Keywords: ON
Keywords loaded: 26
Max preview chars: 500
AI classification: OFF

Uptime: 0h 3m 12s
Messages inspected: 47
Alerts raised: 0
Alerts delivered: 1 (failed 0, queued 0)
Account: Your Name (@yourname)
Bot: @makima_alerts_bot
```

"Messages inspected" climbing confirms the watcher is genuinely seeing group
traffic.

**Test 2 — a keyword alert.** In one of your monitored groups, have someone post
a message containing a keyword — `inspection` or `permit` works. Your own
messages never trigger alerts, so it must come from someone else.

**Expect**, within a second or two:

```
🟥 𝐌𝐀𝐊𝐈𝐌𝐀 𝐀𝐋𝐄𝐑𝐓
━━━━━━━━━━━━━━━━━━
🕒 2026-08-25 12:04:33 UTC
🧠 Reason: 🔍 Keyword: inspection
👥 Group: Dispatch Team
🔗 Group Link: https://t.me/c/1234567890
👤 From: Alex Driver (@alexdriver)
🧷 Keyword hits: inspection
📝 Message:
Truck 155 got pulled in for a level 2 inspection.
──────────────────
👉 Message: https://t.me/c/1234567890/48213
```

Tap the message link — it should jump straight to that message in the group.

**Test 3 — a mention.** Have someone `@yourusername` you in a group. You should
get an alert with `🧠 Reason: 🟥 Mention`.

**If no alert arrives**, work down this list in order:

1. `/status` — is the bot replying at all? If not, you're not authorised or the
   bot client isn't running.
2. Did the message come from someone *else*? Your own messages are ignored by
   design.
3. Are the modes `ON` in `/status`?
4. `docker compose logs --tail=50` — every delivery failure is logged with a
   reason.
5. Was the keyword actually in the message as a whole word? `inspection` matches
   `inspection` but not `inspections` (which isn't in the default list).

---

## Step 14 — Set ADMIN_USER_IDS

Right now `ADMIN_USER_IDS` is empty, so MAKIMA defaults to the account it signed
in as. That works fine. Setting it explicitly is clearer, and it's how you add a
second person.

Use the numeric ID from Step 10. Lost it? Two ways to find it: send any command
to the bot from a *different* Telegram account (the rejection reply includes that
account's ID), or grep the log:

```bash
grep "defaulting to the watching account" /opt/telegram-watcher/logs/makima.log
```

**Edit:**

```bash
cd /opt/telegram-watcher && nano .env
```

Set the line to your ID:

```
ADMIN_USER_IDS=123456789
```

Multiple admins are comma-separated: `ADMIN_USER_IDS=123456789,987654321`. Every
listed ID **receives alerts and can run commands**, including editing your
keyword list — only add people you trust with that.

Save with Ctrl+O, Enter, Ctrl+X.

**Apply it.** An environment change needs the container recreated, not just
restarted:

```bash
cd /opt/telegram-watcher && docker compose up -d
```

**Checkpoint:** send `/status` again — you should get the full status block. The
log should now read `Alert recipients: [123456789]` with no "defaulting to"
line above it.

---

## Step 15 — Verify it survives a reboot

`restart: unless-stopped` plus an enabled Docker service means MAKIMA comes back
by itself. Prove it rather than assuming it.

```bash
systemctl is-enabled docker
```

**Expect:** `enabled`. If it says `disabled`, run `systemctl enable docker`.

Then:

```bash
reboot
```

Wait about a minute, reconnect, and check:

```bash
cd /opt/telegram-watcher && docker compose ps && docker compose logs --tail=25
```

**Expect** the container `Up` again, a fresh ONLINE banner in the log, and a new
"MAKIMA watcher is running" message in Telegram.

That startup message doubles as a reboot notification for the rest of the
server's life — if you ever receive one unexpectedly, your VPS restarted.

---

# Living with it

## Operations cheat sheet

Everything runs from `/opt/telegram-watcher`.

| Task | Command |
| --- | --- |
| Deploy new code from GitHub | `./scripts/deploy.sh` |
| Follow live logs | `./scripts/logs.sh` |
| Restart (no rebuild) | `./scripts/restart.sh` |
| Stop | `./scripts/stop.sh` |
| Start | `./scripts/start.sh` |
| Re-authenticate Telegram | `./scripts/auth.sh` |
| Health check | `docker compose exec makima python -m app.health` |
| Read the persistent log file | `tail -f logs/makima.log` |
| Search the log for errors | `grep -i error logs/makima.log \| tail -30` |
| Container status | `docker compose ps` |
| Disk used by Docker | `docker system df` |
| Clean up old images | `docker image prune -f` |

A healthy health check looks like:

```
[OK  ] environment variables - api id set, bot token set, 1 admin id(s)
[OK  ] settings file - /app/data/watcher_settings.json
[OK  ] keyword file - 26 keyword(s)
[OK  ] sessions directory writable - /app/sessions
[OK  ] logs directory writable - /app/logs
[OK  ] user session - present (28672 bytes)

HEALTHY
```

**Never run `docker system prune --volumes`.** It would delete data. `docker
image prune -f` is safe — it only removes untagged images left behind by
rebuilds.

### Two different logs

| Where | What | Retention |
| --- | --- | --- |
| `docker compose logs` | Docker's capture of stdout | 10 MB × 5 files |
| `logs/makima.log` | MAKIMA's own log file, on the host | 2 MB × 5 files |

The file survives container rebuilds; Docker's capture does not. When
investigating something that happened yesterday, the file is usually the better
source.

---

## The update workflow

Edit code on Windows → commit → push → deploy on the VPS.

On Windows:

```bash
cd "C:\cloude experiments\telegram-watcher" && git add -A && git commit -m "your message" && git push
```

On the VPS:

```bash
cd /opt/telegram-watcher && ./scripts/deploy.sh
```

That pulls, rebuilds and restarts. `.env`, `sessions/`, `data/` and `logs/` are
never touched — they're git-ignored on disk and bind-mounted into the container,
so your login, keywords and settings survive every deploy.

**If `git pull --ff-only` fails** with "local changes would be overwritten", you
edited something directly on the server. Check what:

```bash
cd /opt/telegram-watcher && git status
```

Then either discard those changes (`git checkout -- <file>`) or, if they matter,
commit and push them. The script deliberately refuses to guess which you meant.

### Most changes need no deploy at all

Keywords, modes, preview length and the alert template all change from the
Telegram chat and are written to disk instantly:

```
/addkeyword broker
/removekeyword camera
/setmentions off
/setmaxchars 800
/settemplate <your new template>
/reload
```

Only actual Python changes need `deploy.sh`.

---

## Backups

Two things are irreplaceable and neither is in git:

| Path | Why |
| --- | --- |
| `.env` | Your credentials |
| `sessions/*.session` | Your Telegram login — losing it means redoing Step 10 |

`data/` (keywords and settings) is worth keeping but easy to recreate.

**Make an archive on the VPS:**

```bash
cd /opt && tar czf makima-backup-$(date +%F).tar.gz telegram-watcher/.env telegram-watcher/sessions telegram-watcher/data && ls -la makima-backup-*.tar.gz
```

**Copy it to Windows** — run this in PowerShell on Windows, not on the VPS:

```bash
scp root@YOUR_VPS_IP:/opt/makima-backup-*.tar.gz "C:\backups\"
```

**That archive contains live credentials.** Keep it somewhere private — not a
shared or synced folder, and never in a git repo.

Hostinger's snapshots and automatic backups (hPanel → VPS) cover the whole disk
and are worth taking before any risky change. They're a whole-server restore
though, not a way to recover a single file.

---

## Migrating from the old watcher

Your old setup has its own directory, sessions and config. **Nothing is deleted
automatically.** Leave it in place until the new one has run happily for a few
days.

### Stop the old watcher first

Two processes cannot share a Telegram session. If the old one is still running
you'll get `database is locked` or `AuthKeyDuplicatedError`.

```bash
ps aux | grep -i watcher
```

Kill it by PID, or if it was a systemd service:

```bash
systemctl stop old-watcher && systemctl disable old-watcher
```

If it was in a screen or tmux session, `screen -ls` / `tmux ls` will find it.

### Bring your keywords across

This is the part actually worth migrating:

```bash
cp /opt/old-watcher/keywords.txt /opt/telegram-watcher/data/keywords.txt
```

Then send `/reload` to the bot, or run `./scripts/restart.sh`.

You can copy `watcher_settings.json` the same way — anything missing from an old
file is filled in from the defaults by the deep merge, so a partial or outdated
file won't break anything.

### The old session files

You *can* reuse them, if they were created with the same `api_id` and `api_hash`:

```bash
cp /opt/old-watcher/user_session.session /opt/telegram-watcher/sessions/user_session.session && cp /opt/old-watcher/bot_session.session /opt/telegram-watcher/sessions/bot_session.session && chmod 600 /opt/telegram-watcher/sessions/*.session
```

Honestly, though: you already authenticated cleanly in Step 10, so there's no
reason to. If the log then shows `The user session is not authorised` or an
auth-key error, the old sessions weren't compatible — delete them and re-run
Step 10.

### Retiring the old directory

Once you're confident, and **in this order**:

1. In Telegram → **Settings → Devices**, terminate the old watcher's session.
   Do this *before* deleting the files, or that login stays valid and you'll have
   no easy way to identify it later.
2. Then remove the directory:

```bash
rm -rf /opt/old-watcher
```

---

## Troubleshooting matrix

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `database is locked` | Two processes on one session | `docker compose down`, `pkill -f app.main`, then `docker compose up -d` |
| `AuthKeyDuplicatedError` | Same session used from two IPs | Stop every copy, delete `sessions/user_session.session`, redo Step 10 |
| `The user session is not authorised` | No session file, or it was terminated | Redo Step 10 |
| `TELEGRAM_BOT_TOKEN was rejected` | Wrong or revoked token | New token from @BotFather → `/mybots` → API Token |
| `Telegram rejected TELEGRAM_API_ID / TELEGRAM_API_HASH` | Typo in `.env` | Re-copy from my.telegram.org; hash is exactly 32 hex chars |
| `UserIsBlockedError` | Never pressed Start | Open the bot chat, press Start, `docker compose restart` |
| `Recipient N is unreachable` | Wrong ID in `ADMIN_USER_IDS` | Fix `.env`, then `docker compose up -d` |
| Container restart loop | Read the last error before each restart | `docker compose logs --tail=50` |
| `FloodWaitError` in logs | Telegram rate limit | Handled automatically. Constant occurrence means a restart loop — check `docker compose ps` |
| `Telegram disconnected` repeatedly | Network or outbound firewall | Check the VPS network; ensure outbound 443 isn't blocked |
| Alerts stopped, no errors | Check `/status` first | If uptime is small, it's restarting; if modes are OFF, turn them on |
| A keyword never matches | Word-boundary matching | `claim` matches `claim` but not `disclaimer` or `claims`. Add variants with `/addkeyword` |
| `Permission denied` on session files | Ownership mismatch | `chown -R root:root /opt/telegram-watcher && chmod 600 sessions/*.session` |
| `command not found: docker compose` | Compose plugin missing | Re-run Step 5b |

---

## Hostinger-specific notes

**The browser terminal.** hPanel's built-in terminal is fine for `ls` and reading
logs, but it can drop or mangle keystrokes during interactive input. Use a real
SSH client for Step 10 specifically.

**Firewall — nothing to open.** MAKIMA opens outbound connections to Telegram
and nothing listens for inbound traffic. There's no web server, no webhook, no
exposed port in `docker-compose.yml`. You do not need to change any firewall
setting for it to work.

If you *do* enable Hostinger's firewall (hPanel → VPS → Firewall):

- Keep **inbound TCP 22** allowed or you lose SSH access.
- Don't add outbound blocking rules. Telegram uses TCP 443 across a wide IP
  range; blocking outbound breaks the watcher with confusing "Telegram
  disconnected" loops.
- Hostinger's firewall applies at the network edge, so a bad rule locks you out
  even though the server itself is fine. Test new rules from a second terminal
  before closing your working session.

**Running as root.** Hostinger gives you root and this guide uses it throughout.
Files in `sessions/` and `logs/` end up root-owned, which is consistent because
the container also runs as root. If you later create a non-root user, `chown -R`
the whole project directory or you'll hit permission errors.

**Rebuilding the OS wipes everything**, including `.env` and your sessions.
hPanel's "Operating System" → rebuild is a full disk wipe. Take the backup from
the [Backups](#backups) section first.

**Automatic Ubuntu updates can reboot the box.** That's fine — MAKIMA restarts
itself and messages you. To control the timing instead:

```bash
dpkg-reconfigure -plow unattended-upgrades
```

**Monitoring.** hPanel → VPS → Monitoring shows CPU, RAM and network. MAKIMA is
tiny. Sustained CPU load means a restart loop, not an undersized VPS — check
`docker compose logs`.

**If the VPS IP ever changes** (migration, plan change), Telegram may treat the
next connection as a new location. The session keeps working; you'll just get a
notification on your other devices.
