#!/usr/bin/env bash
# One-time interactive Telegram login for the personal account.
# Creates sessions/user_session.session, which every later start reuses.

source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

require_env

info "Starting interactive Telegram authentication"
echo "You will be asked for the login code Telegram sends to your app,"
echo "and for your 2FA password if the account has one."
echo

compose run --rm makima python -m app.auth_user

info "If that succeeded, start the watcher with: ./scripts/start.sh"
