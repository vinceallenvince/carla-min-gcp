#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-xwalk-keyboards-01}"
ZONE="${ZONE:-us-east4-a}"
VM_NAME="${VM_NAME:-carla-poc}"
LOCAL_PORT="${LOCAL_PORT:-8080}"
REMOTE_PORT="${REMOTE_PORT:-8080}"
CONTROL_SOCKET="${CONTROL_SOCKET:-/tmp/carla-viewer-${VM_NAME}-${LOCAL_PORT}.sock}"
ACTION="${1:-status}"

gcloud_control() {
  gcloud compute ssh "${VM_NAME}" \
    --project="${PROJECT_ID}" \
    --zone="${ZONE}" \
    -- -S "${CONTROL_SOCKET}" "$@"
}

is_running() {
  [[ -S "${CONTROL_SOCKET}" ]] && gcloud_control -O check >/dev/null 2>&1
}

show_status() {
  if ! is_running; then
    echo "CARLA viewer tunnel is stopped."
    return 1
  fi

  echo "CARLA viewer tunnel is running:"
  echo "  http://127.0.0.1:${LOCAL_PORT}/ -> ${VM_NAME}:127.0.0.1:${REMOTE_PORT}"
  curl --fail --silent --show-error \
    --connect-timeout 5 \
    "http://127.0.0.1:${LOCAL_PORT}/healthz"
  echo
}

case "${ACTION}" in
  start)
    if is_running; then
      show_status
      exit 0
    fi

    if [[ -e "${CONTROL_SOCKET}" ]]; then
      rm -f -- "${CONTROL_SOCKET}"
    fi

    if lsof -nP -iTCP:"${LOCAL_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
      echo "Local port ${LOCAL_PORT} is already in use." >&2
      lsof -nP -iTCP:"${LOCAL_PORT}" -sTCP:LISTEN >&2
      exit 1
    fi

    gcloud compute ssh "${VM_NAME}" \
      --project="${PROJECT_ID}" \
      --zone="${ZONE}" \
      -- \
      -M \
      -S "${CONTROL_SOCKET}" \
      -fNT \
      -L "127.0.0.1:${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" \
      -o ExitOnForwardFailure=yes \
      -o ServerAliveInterval=30 \
      -o ServerAliveCountMax=3

    for _ in {1..15}; do
      if curl --fail --silent --output /dev/null \
        --connect-timeout 2 \
        "http://127.0.0.1:${LOCAL_PORT}/healthz"; then
        show_status
        exit 0
      fi
      sleep 1
    done

    echo "Tunnel started, but the viewer did not become ready." >&2
    exit 1
    ;;
  stop)
    if is_running; then
      gcloud_control -O exit >/dev/null
      echo "CARLA viewer tunnel stopped."
    else
      [[ ! -e "${CONTROL_SOCKET}" ]] || rm -f -- "${CONTROL_SOCKET}"
      echo "CARLA viewer tunnel is already stopped."
    fi
    ;;
  status)
    show_status
    ;;
  *)
    echo "Usage: $0 {start|status|stop}" >&2
    exit 2
    ;;
esac
