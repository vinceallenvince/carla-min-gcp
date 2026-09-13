#!/usr/bin/env bash
set -euo pipefail

LOCAL_PORT="${LOCAL_PORT:-8080}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_FILE="${OUTPUT_FILE:-${REPO_DIR}/artifacts/phase7-tunnel-frame.jpg}"

LISTENERS="$(lsof -nP -iTCP:"${LOCAL_PORT}" -sTCP:LISTEN)"
if ! grep -Fq "127.0.0.1:${LOCAL_PORT}" <<<"${LISTENERS}"; then
  echo "No IPv4 loopback listener found on local port ${LOCAL_PORT}." >&2
  printf '%s\n' "${LISTENERS}" >&2
  exit 1
fi
if grep -Eq "(\*|0\.0\.0\.0):${LOCAL_PORT}" <<<"${LISTENERS}"; then
  echo "Local tunnel is listening beyond loopback." >&2
  printf '%s\n' "${LISTENERS}" >&2
  exit 1
fi

mkdir -p "$(dirname "${OUTPUT_FILE}")"
python3 "${REPO_DIR}/verify_viewer.py" \
  --phase=7 \
  --base-url="http://127.0.0.1:${LOCAL_PORT}" \
  --output="${OUTPUT_FILE}"

echo "Local listener:"
printf '%s\n' "${LISTENERS}"
echo "Verified frame: ${OUTPUT_FILE}"
