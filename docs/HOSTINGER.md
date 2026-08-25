# Deploying MAKIMA on a Hostinger VPS

A complete, do-this-in-order runbook. Every command is meant to be pasted into
an SSH session as `root`. Checkpoints tell you what you should see before moving
on — if a checkpoint does not match, stop there and fix it rather than
continuing.

Total time: about 20 minutes, most of it waiting for Docker to install.

---

## Contents

- [0. What you need before you start](#0-what-you-need-before-you-start)
- [1. Find your VPS details in hPanel](#1-find-your-vps-details-in-hpanel)
- [2. Connect over SSH from Windows](#2-connect-over-ssh-from-windows)
- [3. Take a snapshot first](#3-take-a-snapshot-first)
- [4. Prepare the server](#4-prepare-the-server)
- [5. Install Docker](#5-install-docker)
- [6. Firewall](#6-firewall)
- [7. Put the code on the VPS](#7-put-the-code-on-the-vps)
- [8. Create the .env file](#8-create-the-env-file)
- [9. Authenticate your Telegram account](#9-authenticate-your-telegram-account)
- [10. Start the bot chat](#10-start-the-bot-chat)
- [11. Launch MAKIMA](#11-launch-makima)
- [12. Lock down ADMIN_USER_IDS](#12-lock-down-admin_user_ids)
- [13. Confirm it survives a reboot](#13-confirm-it-survives-a-reboot)
- [14. Day-to-day operations](#14-day-to-day-operations)
- [15. Backups](#15-backups)
- [16. Migrating from the old watcher](#16-migrating-from-the-old-watcher)
- [17. Hostinger-specific gotchas](#17-hostinger-specific-gotchas)

---

## 0. What you need before you start

| Thing | Where it comes from |
| --- | --- |
| hPanel login | hostinger.com account |
| VPS IP address and root password | hPanel → VPS |
| `api_id` + `api_hash` | <https://my.telegram.org> → API development tools |
| Bot token | @BotFather → `/newbot` or `/mybots` |
| Your phone, with Telegram open | You will receive a login code during step 9 |
| A GitHub repo containing this project | See [step 7](#7-put-the-code-on-the-vps) |

Have the phone in your hand for step 9. The login code expires quickly.

---

## 1. Find your VPS details in hPanel

Log into hPanel and open **VPS** → your server.

- The **Overview** page shows the **IP address** and the OS template.
- Root access lives in the SSH / root-password area of the VPS menu. If you do
  not remember the root password, reset it there — it applies within a minute.
- There is also a **Browser terminal** button. It works, but use a real SSH
  client for step 9; browser terminals sometimes swallow interactive input.

hPanel's exact menu labels shift between redesigns. If a name below does not
match what you see, look for the equivalent section — the concepts (IP, root
password, snapshots, firewall) are always there.

**Which OS?** This guide assumes Ubuntu 22.04 or 24.04, which is what Hostinger
installs by default. Check with `cat /etc/os-release` once you are connected.

If you chose Hostinger's **"Ubuntu with Docker"** application template, Docker is
already installed — you can skim step 5, but still run its verification commands.

---

## 2. Connect over SSH from Windows

Windows 11 ships with an SSH client. Open **PowerShell** (not the Claude Code
terminal) and run:

```powershell
ssh root@YOUR_VPS_IP
```

The first connection asks:

```
The authenticity of host '203.0.113.10' can't be established.
ED25519 key fingerprint is SHA256:xxxxxxxx...
Are you sure you want to continue connecting (yes/no/[fingerprint])?
```

Type `yes` and press Enter, then enter the root password. Nothing appears while
you type the password — that is normal.

**Checkpoint.** Your prompt should now look like `root@srv123456:~#`.

### Optional but recommended: switch to key-based login

Passwords over SSH get brute-forced constantly on public IPs. On **your Windows
machine**:

```powershell
ssh-keygen -t ed25519 -C "makima-vps"
Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub
```

Copy that public key into hPanel → VPS → **SSH Keys** → Add key. After that,
`ssh root@YOUR_VPS_IP` logs in without a password.

Once keys work, you can disable password login entirely (optional):

```bash
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh
```

Do this **only** after you have confirmed key login works in a second terminal.
Locking yourself out means a rebuild.

---

## 3. Take a snapshot first

Before installing anything, make a rollback point. hPanel → VPS → **Snapshots**
→ **Create snapshot**. It takes a couple of minutes.

Hostinger keeps one manual snapshot; creating a new one replaces the old. If
anything in this guide goes badly wrong, restoring that snapshot puts the server
back exactly as it was.

---

## 4. Prepare the server

```bash
apt update && apt upgrade -y
```

If it asks about keeping local config files, keep the current version (the
default). If it says a reboot is required:

```bash
reboot
```

Wait ~30 seconds and reconnect with `ssh root@YOUR_VPS_IP`.

Check what you are working with:

```bash
cat /etc/os-release | head -2
free -h
df -h /
nproc
```

**Checkpoint.** You want at least ~1 GB free RAM and ~5 GB free disk. Any
Hostinger KVM plan clears this easily. MAKIMA itself idles at roughly 80–120 MB.

If `free -h` shows no swap and you are on the smallest plan, add some — the
Docker build is the only memory-hungry moment:

```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

---

## 5. Install Docker

**Do not** use `apt install docker-compose-plugin` — that package does not exist
in Ubuntu 22.04's repositories. Use Docker's own installer, which works on both
22.04 and 24.04 and includes the `docker compose` plugin:

```bash
apt install -y git curl
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
```

That downloads and runs Docker's official convenience script from
`get.docker.com`. It takes a few minutes.

**Checkpoint.** Both of these must print a version:

```bash
docker --version
docker compose version
```

Expect something like `Docker version 27.x` and `Docker Compose version v2.x`.
If `docker compose version` errors with "is not a docker command", the plugin
did not install — re-run the script above.

Confirm the daemon is healthy:

```bash
docker run --rm hello-world
```

You should see "Hello from Docker!". If this fails, nothing later will work.

---

## 6. Firewall

**MAKIMA needs no inbound ports at all.** It opens outbound connections to
Telegram and nothing listens for incoming traffic. There is no web server, no
webhook, no exposed port in `docker-compose.yml`.

So: you do not need to change anything for MAKIMA to work.

If you use Hostinger's firewall (hPanel → VPS → **Firewall**), be careful:

- Keep **TCP 22 inbound** allowed, or you lose SSH access.
- Do not add outbound blocking rules. Telegram uses TCP 443 to a wide range of
  IPs; blocking outbound traffic breaks the watcher with confusing
  "Telegram disconnected" loops.

Hostinger's firewall applies at the network edge, so a bad rule can lock you out
even though the server is running. Test any new firewall rule from a second
terminal before closing your working session.

---

## 7. Put the code on the VPS

### 7a. First, push the project to GitHub from Windows

The project on your machine is at `C:\cloude experiments\telegram-watcher` and is
already a git repository with one commit. Create an **empty private repository**
on GitHub (no README, no .gitignore — the repo already has both), then in
PowerShell:

```powershell
cd "C:\cloude experiments\telegram-watcher"
git remote add origin https://github.com/YOUR_USERNAME/telegram-watcher.git
git push -u origin main
```

**Checkpoint.** Open the repo on GitHub. You must **not** see a `.env` file, and
`sessions/` must contain only `.gitkeep`. If you see either, stop and fix the
`.gitignore` before going further.

### 7b. Clone it on the VPS

For a **public** repo:

```bash
cd /opt
git clone https://github.com/YOUR_USERNAME/telegram-watcher.git
cd /opt/telegram-watcher
chmod +x scripts/*.sh
```

For a **private** repo, give the VPS a read-only deploy key:

```bash
ssh-keygen -t ed25519 -C "makima-deploy" -f /root/.ssh/github_deploy -N ""
cat /root/.ssh/github_deploy.pub
```

Copy that key, then on GitHub go to your repo → **Settings** → **Deploy keys** →
**Add deploy key**. Paste it, give it a name, and leave "Allow write access"
**unchecked**. Then tell SSH to use it:

```bash
cat >> /root/.ssh/config <<'EOF'
Host github.com
  IdentityFile /root/.ssh/github_deploy
  IdentitiesOnly yes
EOF
chmod 600 /root/.ssh/config

cd /opt
git clone git@github.com:YOUR_USERNAME/telegram-watcher.git
cd /opt/telegram-watcher
chmod +x scripts/*.sh
```

The first `git clone` over SSH asks you to trust `github.com` — type `yes`.

**Checkpoint.**

```bash
ls -la /opt/telegram-watcher
```

You should see `app/`, `data/`, `scripts/`, `docker-compose.yml`, `.env.example`,
and empty `sessions/` and `logs/` directories.

---

## 8. Create the .env file

`.env` exists **only on the server**. It is git-ignored and never enters the
Docker image.

```bash
cd /opt/telegram-watcher
cp .env.example .env
nano .env
```

Fill in these five lines:

```
TELEGRAM_API_ID=35221038
TELEGRAM_API_HASH=your_32_character_hash
TELEGRAM_PHONE=+998920103240
TELEGRAM_BOT_TOKEN=123456789:AAE...
ADMIN_USER_IDS=
```

Leave `ADMIN_USER_IDS` empty for now — step 12 fills it in properly.

In nano: **Ctrl+O**, Enter to save, **Ctrl+X** to exit.

Then restrict it so only root can read it:

```bash
chmod 600 .env
```

**Checkpoint.** No stray quotes, no trailing spaces, no blank value:

```bash
grep -c '=$' .env
```

That counts empty settings. `ADMIN_USER_IDS=` plus the three commented optional
ones are fine; if any of the four required values is empty, go back and fix it.

---

## 9. Authenticate your Telegram account

This is the one interactive step, and the one most likely to trip you up. Read
the whole section before running the command.

```bash
cd /opt/telegram-watcher
docker compose run --rm makima python -m app.auth_user
```

The first run builds the image (2–4 minutes). Then:

```
MAKIMA - Telegram user authentication
Session file: /app/sessions/user_session.session

Requesting a login code for +998920103240 ...
Telegram has sent a code to your Telegram app (not SMS, usually).
Telegram login code:
```

**Where the code arrives.** Telegram sends it as a message inside the Telegram
app on a device where you are already logged in — usually the chat named
"Telegram". It is **not** normally an SMS. Check your phone, and check Telegram
Desktop if you use it.

Type the code and press Enter. If the account has two-step verification:

```
Two-factor authentication is enabled on this account.
2FA password (hidden):
```

Nothing echoes while you type. Press Enter.

**Success looks like:**

```
Signed in as Your Name (@yourname) (id 123456789).
Session saved to /app/sessions/user_session.session

Next steps:
  1. Open a private chat with your MAKIMA bot and press Start.
  2. Launch the watcher:  docker compose up -d
```

**Write down that numeric id.** You need it in step 12.

**Checkpoint.** The session file must now exist on the host:

```bash
ls -la /opt/telegram-watcher/sessions/
```

You should see `user_session.session`, a few dozen KB. Lock it down:

```bash
chmod 700 /opt/telegram-watcher/sessions
chmod 600 /opt/telegram-watcher/sessions/*.session
```

### If step 9 goes wrong

| Symptom | Cause and fix |
| --- | --- |
| `Telegram does not recognise that phone number` | Wrong format. Must include `+` and country code, no spaces |
| `That login code is not correct` | Mistyped, or you used an old code. Re-run the command |
| `That login code expired` | You took too long. Re-run and enter it promptly |
| `Telegram is rate-limiting logins. Wait N seconds` | You retried too often. Wait the stated time — retrying makes it longer |
| Nothing happens when you type | Your terminal is not passing input. Use a real SSH client, not the browser terminal |
| Code never arrives | Check every device where you are logged into Telegram. If you are logged in nowhere, Telegram falls back to SMS after a minute or two |

**A note on logging in from a data-centre IP.** Telegram may show a "new login"
notification on your other devices, and if your account has never been used
outside your home country you may see extra verification. This is normal. Do not
delete that new session from Telegram's Devices screen — it *is* MAKIMA. It
appears there as **MAKIMA watcher**.

**On automation risk, honestly:** running a user account through Telethon is
what this project does by design, and passive reading is low-risk. But Telegram
does limit accounts that behave abnormally. MAKIMA only reads and never posts to
groups, which keeps you well inside normal behaviour — just do not add
auto-replying or mass-messaging on top of it.

---

## 10. Start the bot chat

Open Telegram on your phone, search for your bot's username, open it, and press
**Start** (or send `/start`).

This is not optional. Telegram forbids a bot from sending the first message to a
user who has never started it. Skip this and MAKIMA will run perfectly while
delivering nothing, and the log will show:

```
Cannot message 123456789 (UserIsBlockedError). Open a private chat with the bot and press Start.
```

---

## 11. Launch MAKIMA

```bash
cd /opt/telegram-watcher
docker compose up -d
docker compose ps
```

`docker compose ps` should show the container as `Up` (and after ~30 seconds,
`healthy`).

Watch the logs:

```bash
docker compose logs -f --tail=100
```

**Checkpoint.** You are looking for exactly this block:

```
MAKIMA watcher starting (v1.0.0)
Settings loaded from /app/data/watcher_settings.json
Loaded 26 keywords from /app/data/keywords.txt
User account authenticated
Bot authenticated
Alert dispatcher started
Message watcher registered on the user client
==========================================================
MAKIMA TELEGRAM WATCHER ONLINE (v1.0.0)
User: Your Name (@yourname)
Bot: @makima_alerts_bot
Keywords loaded: 26
Modes: mentions=on, replies=on, keywords=on, ai=off
==========================================================
```

And in Telegram, your bot messages you:

```
🟥 MAKIMA watcher is running.

Use /help in private chat.
```

Press **Ctrl+C** to stop following the logs — that does not stop MAKIMA.

**Now test it end to end.** Ask someone in one of your monitored groups to
mention you, or post a message containing a keyword such as `inspection` in a
group you are in. An alert should arrive within a second or two.

If nothing arrives, send `/status` to the bot and work through the
"Alerts are not arriving" checklist in the main [README](../README.md#alerts-are-not-arriving).

---

## 12. Lock down ADMIN_USER_IDS

Right now `ADMIN_USER_IDS` is empty, which means MAKIMA defaults to the account
it signed in as. That works, but setting it explicitly is safer and lets you add
a second admin.

Use the numeric id from step 9. If you did not write it down, send any command to
the bot from a different account, or just read it from the logs — every
unauthorised attempt logs the sender's id.

```bash
cd /opt/telegram-watcher
nano .env
```

Set:

```
ADMIN_USER_IDS=123456789
```

Multiple admins are comma-separated: `ADMIN_USER_IDS=123456789,987654321`.
Every listed id receives alerts **and** can run commands, so only add people you
trust with the keyword list.

Apply it — an env change needs a container recreate, not just a restart:

```bash
docker compose up -d
```

**Checkpoint.** Send `/status` to the bot. It should reply with the full status
block.

---

## 13. Confirm it survives a reboot

`restart: unless-stopped` in `docker-compose.yml` plus an enabled Docker service
means MAKIMA comes back on its own. Verify it rather than assuming:

```bash
systemctl is-enabled docker
reboot
```

Wait a minute, reconnect, and check:

```bash
cd /opt/telegram-watcher
docker compose ps
docker compose logs --tail=30
```

The container should be `Up` again and you should get a fresh
"MAKIMA watcher is running" message in Telegram. That startup message is your
reboot notification — if you ever get one unexpectedly, the VPS restarted.

---

## 14. Day-to-day operations

All of these run from `/opt/telegram-watcher`.

| Task | Command |
| --- | --- |
| Deploy new code from GitHub | `./scripts/deploy.sh` |
| Follow logs | `./scripts/logs.sh` |
| Restart (no rebuild) | `./scripts/restart.sh` |
| Stop | `./scripts/stop.sh` |
| Start | `./scripts/start.sh` |
| Health check | `docker compose exec makima python -m app.health` |
| Read the log file directly | `tail -f logs/makima.log` |
| Disk used by Docker | `docker system df` |

### The update workflow

Edit code on Windows, commit, push, then on the VPS:

```bash
cd /opt/telegram-watcher
./scripts/deploy.sh
```

That pulls, rebuilds and restarts. `.env`, `sessions/`, `data/` and `logs/` are
never touched — they are git-ignored on disk and bind-mounted into the container,
so your login, keywords and settings survive every deploy.

If `git pull --ff-only` fails with "local changes would be overwritten", you
edited something on the server. Check `git status`, then either `git stash` or
commit and push those changes properly. The script refuses to guess.

### Most changes need no deploy at all

Keywords, modes, preview length and the alert template are all changed from the
Telegram chat with `/addkeyword`, `/setmentions`, `/setmaxchars`, `/settemplate`.
Those write to disk instantly. Only actual code changes need `deploy.sh`.

### Reclaiming disk space

Old images accumulate after several deploys:

```bash
docker image prune -f
```

Safe — it only removes untagged images. Never run `docker system prune
--volumes`, which would delete data.

---

## 15. Backups

Two things are irreplaceable and neither is in git:

| Path | Why it matters |
| --- | --- |
| `.env` | Your credentials |
| `sessions/*.session` | Your Telegram login. Losing it means re-authenticating |

`data/` (keywords and settings) is nice to keep but easy to recreate.

Make a local backup archive:

```bash
cd /opt
tar czf makima-backup-$(date +%F).tar.gz \
  telegram-watcher/.env \
  telegram-watcher/sessions \
  telegram-watcher/data
ls -la makima-backup-*.tar.gz
```

Copy it to your Windows machine — run this **in PowerShell on Windows**, not on
the VPS:

```powershell
scp root@YOUR_VPS_IP:/opt/makima-backup-*.tar.gz "C:\backups\"
```

**That archive contains live credentials.** Store it somewhere private, not in a
synced folder you share, and never in a git repo.

Hostinger's own snapshots and weekly backups (hPanel → VPS → Snapshots /
Backups) cover the whole disk and are worth taking before any risky change. They
are a whole-server restore, though — not a way to recover one file.

---

## 16. Migrating from the old watcher

Your old directory has its own sessions and config. **Nothing is deleted
automatically.** Leave it in place until the new setup has run happily for a few
days.

First, stop the old watcher — two processes cannot share a Telegram session, and
if the old one is running you will get `database is locked` or
`AuthKeyDuplicatedError`:

```bash
# find whatever is running the old script
ps aux | grep -i watcher
# stop it by PID, or if it was a systemd service:
systemctl stop old-watcher && systemctl disable old-watcher
```

Bring the keywords across (this is the part worth keeping):

```bash
cp /opt/old-watcher/keywords.txt /opt/telegram-watcher/data/keywords.txt
```

Then either send `/reload` to the bot, or `./scripts/restart.sh`.

You can copy `watcher_settings.json` too — anything missing from it is filled in
from the defaults by the deep merge, so an old partial file will not break
anything.

**About the old session files.** You *can* reuse them if they were created with
the same `api_id`/`api_hash`:

```bash
cp /opt/old-watcher/user_session.session /opt/telegram-watcher/sessions/user_session.session
cp /opt/old-watcher/bot_session.session  /opt/telegram-watcher/sessions/bot_session.session
chmod 600 /opt/telegram-watcher/sessions/*.session
docker compose up -d
```

Honestly, though: you already authenticated cleanly in step 9, so there is no
reason to. A fresh session is the safer path.

Once you are confident the new setup works, retire the old one:

```bash
# in Telegram: Settings -> Devices -> terminate the old watcher's session
rm -rf /opt/old-watcher
```

Terminate the old session in Telegram **before** deleting the files — otherwise
that login stays valid and you have no easy way to identify it later.

---

## 17. Hostinger-specific gotchas

**The browser terminal and interactive input.** hPanel's built-in terminal is
fine for `ls` and `docker compose logs`, but it can drop or mangle keystrokes
during `app.auth_user`. Use a real SSH client for step 9.

**Root by default.** Hostinger gives you root, and this guide runs everything as
root. That means files in `sessions/` and `logs/` are root-owned, which is
consistent because the container also runs as root. If you later create a
non-root user, `chown -R` the whole project directory or you will hit permission
errors.

**Reinstalling the OS wipes everything.** hPanel's "Operating System" → rebuild
is a full disk wipe, including `.env` and your sessions. Take the backup from
[step 15](#15-backups) first.

**Automatic Ubuntu updates can reboot the box.** That is fine — MAKIMA restarts
itself and messages you. If you would rather control the timing:

```bash
dpkg-reconfigure -plow unattended-upgrades
```

**Monitoring.** hPanel → VPS → Monitoring shows CPU, RAM and network. MAKIMA is
tiny; if you see sustained CPU load, check `docker compose logs` for a restart
loop rather than assuming the VPS is undersized.

**IP changes.** If you ever move the VPS or change its IP, Telegram may treat
the next connection as a new location. The session keeps working; you may just
get a notification on your other devices.

**Container logs vs the log file.** `docker compose logs` reads Docker's own
capture (capped at 10 MB × 5 by `docker-compose.yml`). `logs/makima.log` is
MAKIMA's own rotating file (2 MB × 5) and persists on the host across container
rebuilds. When something went wrong yesterday, the file is usually the better
place to look:

```bash
grep -i error /opt/telegram-watcher/logs/makima.log | tail -30
```
