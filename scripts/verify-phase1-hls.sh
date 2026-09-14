#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-xwalk-keyboards-01}"
ZONE="${ZONE:-us-east4-a}"
VM_NAME="${VM_NAME:-carla-poc}"

gcloud compute ssh "${VM_NAME}" \
  --project="${PROJECT_ID}" \
  --zone="${ZONE}" \
  --command='set -euo pipefail
sudo docker exec carla-scene \
  python3 /app/verify_hls.py --exercise-restart'
