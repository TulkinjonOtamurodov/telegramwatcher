#!/usr/bin/env bash
# Follow the container logs. Ctrl+C to stop watching (MAKIMA keeps running).

source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

compose logs -f --tail=200
