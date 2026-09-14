#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-xwalk-keyboards-01}"
RUN_SERVICE="${RUN_SERVICE:-xwalk-keyboards}"
RUN_REGION="${RUN_REGION:-us-central1}"
NETWORK="${NETWORK:-default}"
RUN_SUBNET="${RUN_SUBNET:-default}"
VM_NAME="${VM_NAME:-carla-poc}"
VM_ZONE="${VM_ZONE:-us-east4-a}"
VIDEO_PORT="${VIDEO_PORT:-8080}"
VM_TARGET_TAG="${VM_TARGET_TAG:-carla-hls-origin}"
RUN_NETWORK_TAG="${RUN_NETWORK_TAG:-xwalk-cloud-run}"
GITHUB_DEPLOY_SERVICE_ACCOUNT="${GITHUB_DEPLOY_SERVICE_ACCOUNT:-github-deploy@${PROJECT_ID}.iam.gserviceaccount.com}"
ALLOW_RULE="${ALLOW_RULE:-carla-hls-from-cloud-run}"
DENY_RULE="${DENY_RULE:-carla-hls-deny-other}"

source_cidr="$(
  gcloud compute networks subnets describe "${RUN_SUBNET}" \
    --project="${PROJECT_ID}" \
    --region="${RUN_REGION}" \
    --format='value(ipCidrRange)'
)"

if [[ -z "${source_cidr}" ]]; then
  echo "Could not determine the CIDR for ${RUN_SUBNET} in ${RUN_REGION}." >&2
  exit 1
fi

gcloud compute networks subnets add-iam-policy-binding "${RUN_SUBNET}" \
  --project="${PROJECT_ID}" \
  --region="${RUN_REGION}" \
  --member="serviceAccount:${GITHUB_DEPLOY_SERVICE_ACCOUNT}" \
  --role=roles/compute.networkUser >/dev/null

gcloud compute instances add-tags "${VM_NAME}" \
  --project="${PROJECT_ID}" \
  --zone="${VM_ZONE}" \
  --tags="${VM_TARGET_TAG}"

if gcloud compute firewall-rules describe "${ALLOW_RULE}" \
  --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud compute firewall-rules update "${ALLOW_RULE}" \
    --project="${PROJECT_ID}" \
    --priority=900 \
    --action=ALLOW \
    --rules="tcp:${VIDEO_PORT}" \
    --source-ranges="${source_cidr}" \
    --target-tags="${VM_TARGET_TAG}" \
    --enable
else
  gcloud compute firewall-rules create "${ALLOW_RULE}" \
    --project="${PROJECT_ID}" \
    --network="${NETWORK}" \
    --priority=900 \
    --action=ALLOW \
    --rules="tcp:${VIDEO_PORT}" \
    --source-ranges="${source_cidr}" \
    --target-tags="${VM_TARGET_TAG}"
fi

# The default VPC has a broad allow-internal rule. This scoped deny makes the
# allow rule above the only path to the CARLA video port while leaving SSH and
# CARLA RPC ports unchanged.
if gcloud compute firewall-rules describe "${DENY_RULE}" \
  --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud compute firewall-rules update "${DENY_RULE}" \
    --project="${PROJECT_ID}" \
    --priority=1000 \
    --action=DENY \
    --rules="tcp:${VIDEO_PORT}" \
    --source-ranges=0.0.0.0/0 \
    --target-tags="${VM_TARGET_TAG}" \
    --enable
else
  gcloud compute firewall-rules create "${DENY_RULE}" \
    --project="${PROJECT_ID}" \
    --network="${NETWORK}" \
    --priority=1000 \
    --action=DENY \
    --rules="tcp:${VIDEO_PORT}" \
    --source-ranges=0.0.0.0/0 \
    --target-tags="${VM_TARGET_TAG}"
fi

gcloud run services update "${RUN_SERVICE}" \
  --project="${PROJECT_ID}" \
  --region="${RUN_REGION}" \
  --network="${NETWORK}" \
  --subnet="${RUN_SUBNET}" \
  --network-tags="${RUN_NETWORK_TAG}" \
  --vpc-egress=private-ranges-only

echo "Configured ${RUN_SERVICE} to reach ${VM_NAME}:${VIDEO_PORT} from ${source_cidr}."
