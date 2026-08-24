#!/usr/bin/env bash
# Pull the latest code from GitHub, rebuild the image and restart MAKIMA.
#
#   cd /opt/telegram-watcher && ./scripts/deploy.sh
#
# .env, sessions/, data/ and logs/ are never touched: they are git-ignored on
# disk and bind-mounted into the container.

source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

require_env

if [ -d .git ]; then
  info "Pulling latest code"
  # --ff-only refuses to create a merge commit; if this fails you have local
  # edits or a diverged branch, and it is safer to stop than to guess.
  git pull --ff-only
else
  info "Not a git checkout - skipping git pull"
fi

info "Building image"
compose build

info "Restarting container"
compose up -d

info "Current state"
compose ps

info "Done. Follow the logs with: ./scripts/logs.sh"
