#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-xwalk-keyboards-01}"
RUN_REGION="${RUN_REGION:-us-central1}"
NETWORK="${NETWORK:-default}"
RUN_SUBNET="${RUN_SUBNET:-default}"
RUN_NETWORK_TAG="${RUN_NETWORK_TAG:-xwalk-cloud-run}"
VM_NAME="${VM_NAME:-carla-poc}"
VM_ZONE="${VM_ZONE:-us-east4-a}"
VM_PRIVATE_IP="${VM_PRIVATE_IP:-10.150.0.2}"
VIDEO_PORT="${VIDEO_PORT:-8080}"
PROBE_JOB="${PROBE_JOB:-carla-hls-vpc-probe}"
XWALK_URL="${XWALK_URL:-https://xwalk-keyboards-21826886868.us-central1.run.app}"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"${REPO_DIR}/scripts/verify-phase2-network.sh"

probe_command='set -eu; playlist="$(curl -fsS --max-time 15 "${CARLA_HLS_BASE_URL}/live/playlist.m3u8")"; segment="$(printf "%s\n" "$playlist" | sed -n "/^[^#]/p" | tail -n 1)"; test -n "$segment"; curl -fsS --max-time 15 "${CARLA_HLS_BASE_URL}/live/${segment}" --output /tmp/segment.ts; test -s /tmp/segment.ts; echo "Fetched CARLA playlist and segment ${segment} ($(wc -c </tmp/segment.ts) bytes)"'

gcloud run jobs deploy "${PROBE_JOB}" \
  --project="${PROJECT_ID}" \
  --region="${RUN_REGION}" \
  --image=curlimages/curl:8.12.1 \
  --command=sh \
  --args="-c,${probe_command}" \
  --set-env-vars="CARLA_HLS_BASE_URL=http://${VM_PRIVATE_IP}:${VIDEO_PORT}" \
  --network="${NETWORK}" \
  --subnet="${RUN_SUBNET}" \
  --network-tags="${RUN_NETWORK_TAG}" \
  --vpc-egress=private-ranges-only \
  --max-retries=0 \
  --task-timeout=60s \
  --execute-now \
  --wait

external_ip="$(
  gcloud compute instances describe "${VM_NAME}" \
    --project="${PROJECT_ID}" \
    --zone="${VM_ZONE}" \
    --format='value(networkInterfaces[0].accessConfigs[0].natIP)'
)"
if [[ -z "${external_ip}" ]]; then
  echo "VM has no public IP; public video-port check passes."
elif curl --fail --silent --show-error \
  --connect-timeout 5 \
  --max-time 8 \
  "http://${external_ip}:${VIDEO_PORT}/healthz" >/dev/null; then
  echo "Public internet unexpectedly reached ${external_ip}:${VIDEO_PORT}." >&2
  exit 1
else
  echo "Public internet cannot reach ${external_ip}:${VIDEO_PORT}."
fi

python3 - "${XWALK_URL}" <<'PY'
import sys
import urllib.parse
import urllib.request

url = f"{sys.argv[1]}/api/hls/5056/playlist.m3u8"
for _ in range(4):
    with urllib.request.urlopen(url, timeout=20) as response:
        body = response.read()
    entries = [
        line.strip()
        for line in body.decode("utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not entries:
        raise SystemExit("511NY playlist contains no URI entries")
    next_url = urllib.parse.urljoin(url, entries[-1])
    if not entries[-1].split("?", 1)[0].endswith(".m3u8"):
        with urllib.request.urlopen(next_url, timeout=20) as response:
            segment = response.read()
        if len(segment) < 1000:
            raise SystemExit(
                f"511NY media segment is unexpectedly small: {len(segment)} bytes"
            )
        print(f"Existing 511NY proxy fetched a {len(segment)}-byte media segment.")
        break
    url = next_url
else:
    raise SystemExit("511NY HLS playlist nesting exceeded four levels")
PY

echo "Phase 2 live verification passed."
