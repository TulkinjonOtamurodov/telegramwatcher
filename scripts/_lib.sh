#!/usr/bin/env bash
# Shared helpers for the MAKIMA scripts. Sourced, never run directly.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# Use the modern `docker compose` plugin, falling back to the old binary.
compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    echo "Error: neither 'docker compose' nor 'docker-compose' is available." >&2
    echo "Install it with: apt install -y docker.io docker-compose-plugin" >&2
    exit 1
  fi
}

require_env() {
  if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "Error: $PROJECT_DIR/.env is missing." >&2
    echo "Create it with:  cp .env.example .env && nano .env" >&2
    exit 1
  fi
}

info() { printf '==> %s\n' "$*"; }
