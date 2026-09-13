#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-xwalk-keyboards-01}"
ZONE="${ZONE:-us-east4-a}"
VM_NAME="${VM_NAME:-carla-poc}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

gcloud compute scp "${REPO_DIR}/verify_carla.py" "${VM_NAME}:/tmp/verify_carla.py" \
  --project="${PROJECT_ID}" \
  --zone="${ZONE}"

gcloud compute ssh "${VM_NAME}" \
  --project="${PROJECT_ID}" \
  --zone="${ZONE}" \
  --command='set -euo pipefail
ready=false
for attempt in $(seq 1 60); do
  if python3 -c "import socket; socket.create_connection((\"127.0.0.1\", 2000), 2).close()" 2>/dev/null; then
    ready=true
    break
  fi
  if ! sudo docker inspect -f "{{.State.Running}}" carla-server | grep -qx true; then
    sudo docker logs carla-server
    exit 1
  fi
  sleep 5
done

if [ "${ready}" != true ]; then
  echo "CARLA did not accept connections on port 2000 within five minutes." >&2
  sudo docker logs --tail=200 carla-server
  exit 1
fi

wheel_path=$(sudo docker exec carla-server sh -lc "find /home/carla -path \"*/PythonAPI/carla/dist/*\" -type f \( -name \"*.whl\" -o -name \"*.egg\" \) | head -n 1")
if [ -z "${wheel_path}" ]; then
  echo "Could not find the CARLA Python distribution in the container." >&2
  exit 1
fi

client_package="/tmp/$(basename "${wheel_path}")"
sudo docker cp "carla-server:${wheel_path}" "${client_package}"
sudo chown "$(id -u):$(id -g)" "${client_package}"
python3 -m venv /tmp/carla-client-venv
/tmp/carla-client-venv/bin/pip install --quiet --upgrade pip
/tmp/carla-client-venv/bin/pip install --quiet "${client_package}"
/tmp/carla-client-venv/bin/python /tmp/verify_carla.py'
