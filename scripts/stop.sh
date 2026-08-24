#!/usr/bin/env bash
# Stop MAKIMA. Volumes (sessions, data, logs) are left alone.

source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

info "Stopping MAKIMA"
compose down
info "Stopped. sessions/, data/ and logs/ are untouched."
