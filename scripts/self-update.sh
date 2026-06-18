#!/bin/bash
# Conductor self-update — run on the HOST (not inside the container) from cron.
#
# Restores the one piece of legacy `optv pull` that can't live in the container:
# updating the Conductor's own code. A container can't rebuild its own image, and
# the Conductor source repo isn't mounted into it, so this runs on the host where
# the checkout and the Docker daemon live.
#
#   git pull --ff-only && docker compose up -d --build
#
# `up -d --build` is idempotent: if the pull changed nothing, the image rebuild is
# fully cached and the container is NOT recreated. It only restarts the `app`
# container when code actually changed — so this is safe to run frequently.
#
# Guard: skips while a pipeline job is running (recreating the container would
# kill it). Queued jobs are file-backed and survive a restart, so only the
# in-progress job matters. Skipped runs simply retry on the next cron tick.
#
# Crontab example (every 10 min, like the legacy cron), logging to a file:
#   */10 * * * * /path/to/OpenParliamentTV-Conductor/scripts/self-update.sh >> /path/to/OpenParliamentTV-Conductor/logs/self-update.log 2>&1
#
# Requirements on the host: the user must have git access to this repo and be in
# the `docker` group. The checkout must be clean (no local edits to tracked files)
# or `git pull --ff-only` will refuse — note docker-compose.yml is tracked.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
cd "$BASE_DIR"

log() { echo "$(date -Is) $*"; }

# Pick docker compose v2 (`docker compose`) or fall back to v1 (`docker-compose`).
if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    log "ERROR: neither 'docker compose' nor 'docker-compose' found"
    exit 1
fi

# Single-instance guard so overlapping cron ticks don't race git/docker.
exec 9>".self-update.lock"
if ! flock -n 9; then
    log "Another self-update is running — skipping"
    exit 0
fi

# Skip while a pipeline job is in progress. current.json is the literal `null`
# when idle, or a job object (with a status) when one is running.
CURRENT="status/jobs/current.json"
if [ -s "$CURRENT" ] && [ "$(tr -d '[:space:]' < "$CURRENT")" != "null" ]; then
    log "A job is running — skipping update (will retry next tick)"
    exit 0
fi

BEFORE="$(git rev-parse HEAD)"
log "Pulling (current $BEFORE)…"
git pull --ff-only
AFTER="$(git rev-parse HEAD)"

if [ "$BEFORE" = "$AFTER" ]; then
    log "Already up to date — no rebuild"
    exit 0
fi

log "Updated $BEFORE -> $AFTER — rebuilding app container"
"${COMPOSE[@]}" up -d --build app
log "Done"
