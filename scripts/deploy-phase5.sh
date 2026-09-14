#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-xwalk-keyboards-01}"
ZONE="${ZONE:-us-east4-a}"
VM_NAME="${VM_NAME:-carla-poc}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

gcloud compute ssh "${VM_NAME}" \
  --project="${PROJECT_ID}" \
  --zone="${ZONE}" \
  --command='mkdir -p "${HOME}/carla-poc/data"'

gcloud compute scp \
  --recurse \
  "${REPO_DIR}/Dockerfile.driver" \
  "${REPO_DIR}/requirements-viewer.txt" \
  "${REPO_DIR}/simulation.py" \
  "${REPO_DIR}/hls_adapter.py" \
  "${REPO_DIR}/static" \
  "${REPO_DIR}/survey_crosswalks.py" \
  "${REPO_DIR}/survey_crosswalk_views.py" \
  "${REPO_DIR}/survey_navmesh_crosswalks.py" \
  "${REPO_DIR}/probe_ai_crosswalk.py" \
  "${REPO_DIR}/verify_scene.py" \
  "${REPO_DIR}/verify_viewer.py" \
  "${REPO_DIR}/verify_hls.py" \
  "${VM_NAME}:carla-poc/" \
  --project="${PROJECT_ID}" \
  --zone="${ZONE}"

gcloud compute ssh "${VM_NAME}" \
  --project="${PROJECT_ID}" \
  --zone="${ZONE}" \
  --command='set -euo pipefail
cd "${HOME}/carla-poc"
sudo docker build --file Dockerfile.driver --tag carla-poc-driver:0.10.0 .
if sudo docker inspect carla-scene >/dev/null 2>&1; then
  sudo docker stop --time=20 carla-scene >/dev/null
  sudo docker rm carla-scene >/dev/null
fi
sudo docker run --detach \
  --name=carla-scene \
  --restart=unless-stopped \
  --network=host \
  --user="$(id -u):$(id -g)" \
  --env=HOME=/tmp \
  --volume="${HOME}/carla-poc/data:/data" \
  carla-poc-driver:0.10.0 \
  --vehicles=20 \
  --pedestrians=16 \
  --pedestrian-mode=ai \
  --http-host=0.0.0.0 \
  --http-port=8080 \
  --camera-width=352 \
  --camera-height=240 \
  --camera-fps=20 \
  --camera-fov=70 \
  --jpeg-quality=75 \
  --camera-mode=static \
  --crosswalk-id=14 \
  --static-camera-x=-113.041 \
  --static-camera-y=21.270 \
  --static-camera-z=12.0 \
  --static-camera-pitch=-27.242 \
  --static-camera-yaw=-7.917 \
  --static-camera-roll=0.0 \
  --output-dir=/data'
