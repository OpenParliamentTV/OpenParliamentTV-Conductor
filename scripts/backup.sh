#!/usr/bin/env bash
# Back up config/ + status/ to a timestamped tarball.
# Usage: scripts/backup.sh [destination-dir]
#   destination-dir defaults to ./backups

set -euo pipefail

CALLDIR="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-${CALLDIR}/backups}"
mkdir -p "$DEST"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${DEST}/optv-backup-${STAMP}.tar.gz"

cd "$CALLDIR"
tar --exclude='config/secrets.env' \
    -czf "$OUT" \
    config/ status/ 2>/dev/null

echo "Wrote $OUT"
echo "Note: config/secrets.env is excluded — back up separately."
