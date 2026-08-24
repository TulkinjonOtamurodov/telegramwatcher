#!/usr/bin/env bash
# Restart MAKIMA without rebuilding. Use deploy.sh after a code change.

source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

require_env

info "Restarting MAKIMA"
compose restart
compose ps
