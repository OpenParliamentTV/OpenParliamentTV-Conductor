#!/usr/bin/env bash
# Compatibility check — verifies that running a stage via `POST /api/jobs`
# produces byte-identical `processed/` output as the legacy `./optv update` path.
#
# Usage:
#   scripts/compat_check.sh <session-id>
# Requires:
#   - manager running at BASE_URL (default http://localhost:8000)
#   - a valid bearer cookie in $COMPAT_COOKIE
#   - diff, rsync, curl, jq
#
# Exits non-zero on divergence.

set -euo pipefail

SESSION="${1:-}"
BASE_URL="${BASE_URL:-http://localhost:8000}"
COOKIE="${COMPAT_COOKIE:-}"

if [[ -z "$SESSION" ]]; then
  echo "usage: $0 <session-id>" >&2
  exit 2
fi
if [[ -z "$COOKIE" ]]; then
  echo "COMPAT_COOKIE env var (value of optv_token cookie) is required" >&2
  exit 2
fi

CALLDIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${CALLDIR}/OpenParliamentTV-Data-DE"
SNAPSHOT_DIR="$(mktemp -d)"
trap 'rm -rf "$SNAPSHOT_DIR"' EXIT

snapshot() {
  rsync -a --delete "${DATA_DIR}/processed/" "${SNAPSHOT_DIR}/$1/" 2>/dev/null || true
}

echo "[1/3] Running legacy path..."
pushd "$CALLDIR" > /dev/null
./optv update
popd > /dev/null
snapshot legacy

echo "[2/3] Clearing processed/ and running via manager..."
rm -rf "${DATA_DIR}/processed"
JOB_RESP="$(curl -s -X POST "${BASE_URL}/api/jobs" \
  -H "Content-Type: application/json" \
  -H "Cookie: optv_token=${COOKIE}" \
  -d "{\"parliament\":\"DE\",\"stages\":[\"merge\",\"nel\",\"align\"],\"session_filter\":\"^${SESSION}$\"}")"
JOB_ID="$(echo "$JOB_RESP" | jq -r '.job_id')"
echo "  job_id=${JOB_ID}"

while :; do
  STATUS="$(curl -s -H "Cookie: optv_token=${COOKIE}" "${BASE_URL}/api/jobs/${JOB_ID}" | jq -r '.status')"
  case "$STATUS" in
    success|completed) break ;;
    failed|partial|cancelled) echo "manager job ended as $STATUS" >&2; exit 1 ;;
    *) sleep 2 ;;
  esac
done
snapshot manager

echo "[3/3] Diffing outputs..."
if diff -r "${SNAPSHOT_DIR}/legacy" "${SNAPSHOT_DIR}/manager"; then
  echo "OK — outputs are identical."
else
  echo "DIVERGENCE — see diff above." >&2
  exit 1
fi
