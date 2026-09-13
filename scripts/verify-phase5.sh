#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-xwalk-keyboards-01}"
ZONE="${ZONE:-us-east4-a}"
VM_NAME="${VM_NAME:-carla-poc}"

gcloud compute ssh "${VM_NAME}" \
  --project="${PROJECT_ID}" \
  --zone="${ZONE}" \
  --command='set -euo pipefail
cd "${HOME}/carla-poc"
sudo docker run --rm \
  --network=host \
  --env=HOME=/tmp \
  --volume="${HOME}/carla-poc/data:/data:ro" \
  --entrypoint=python3 \
  carla-poc-driver:0.10.0 \
  /app/verify_scene.py'
