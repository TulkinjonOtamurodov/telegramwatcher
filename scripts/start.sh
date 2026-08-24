#!/usr/bin/env bash
# Start MAKIMA in the background.

source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

require_env

if [ ! -f sessions/user_session.session ]; then
  echo "Warning: sessions/user_session.session is missing." >&2
  echo "Authenticate once first:  ./scripts/auth.sh" >&2
fi

info "Starting MAKIMA"
compose up -d
compose ps
