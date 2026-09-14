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

service="$(
  gcloud run services describe "${RUN_SERVICE}" \
    --project="${PROJECT_ID}" \
    --region="${RUN_REGION}" \
    --format=json
)"

python3 - \
  "${service}" \
  "${NETWORK}" \
  "${RUN_SUBNET}" \
  "${RUN_NETWORK_TAG}" <<'PY'
import json
import sys

service_json, expected_network, expected_subnet, expected_tag = sys.argv[1:]
service = json.loads(service_json)
annotations = service["spec"]["template"]["metadata"]["annotations"]
interfaces = json.loads(annotations.get("run.googleapis.com/network-interfaces", "[]"))
egress = annotations.get("run.googleapis.com/vpc-access-egress")
if len(interfaces) != 1:
    raise SystemExit(f"Expected one Direct VPC interface, got {interfaces!r}")
interface = interfaces[0]
if interface.get("network") != expected_network:
    raise SystemExit(f"Unexpected network: {interface!r}")
if interface.get("subnetwork") != expected_subnet:
    raise SystemExit(f"Unexpected subnet: {interface!r}")
if expected_tag not in interface.get("tags", []):
    raise SystemExit(f"Missing Cloud Run network tag: {interface!r}")
if egress != "private-ranges-only":
    raise SystemExit(f"Unexpected VPC egress mode: {egress!r}")
PY

subnet_policy="$(
  gcloud compute networks subnets get-iam-policy "${RUN_SUBNET}" \
    --project="${PROJECT_ID}" \
    --region="${RUN_REGION}" \
    --format=json
)"

python3 - "${subnet_policy}" "${GITHUB_DEPLOY_SERVICE_ACCOUNT}" <<'PY'
import json
import sys

policy, service_account = json.loads(sys.argv[1]), sys.argv[2]
expected_member = f"serviceAccount:{service_account}"
members = {
    member
    for binding in policy.get("bindings", [])
    if binding.get("role") == "roles/compute.networkUser"
    for member in binding.get("members", [])
}
if expected_member not in members:
    raise SystemExit(
        f"Subnet is missing roles/compute.networkUser for {expected_member}"
    )
PY

vm="$(
  gcloud compute instances describe "${VM_NAME}" \
    --project="${PROJECT_ID}" \
    --zone="${VM_ZONE}" \
    --format=json
)"

python3 - "${vm}" "${VM_TARGET_TAG}" <<'PY'
import json
import sys

vm, expected_tag = json.loads(sys.argv[1]), sys.argv[2]
tags = vm.get("tags", {}).get("items", [])
if expected_tag not in tags:
    raise SystemExit(f"VM is missing target tag {expected_tag}: {tags!r}")
PY

allow="$(
  gcloud compute firewall-rules describe "${ALLOW_RULE}" \
    --project="${PROJECT_ID}" \
    --format=json
)"
deny="$(
  gcloud compute firewall-rules describe "${DENY_RULE}" \
    --project="${PROJECT_ID}" \
    --format=json
)"

python3 - \
  "${allow}" \
  "${deny}" \
  "${source_cidr}" \
  "${VIDEO_PORT}" \
  "${VM_TARGET_TAG}" <<'PY'
import json
import sys

allow, deny = map(json.loads, sys.argv[1:3])
source_cidr, port, target_tag = sys.argv[3:]

def assert_rule(rule, *, action, priority, sources):
    if rule.get("disabled", False):
        raise SystemExit(f"Firewall rule is disabled: {rule.get('name')}")
    if rule.get("direction") != "INGRESS":
        raise SystemExit(f"Unexpected direction: {rule!r}")
    if rule.get("priority") != priority:
        raise SystemExit(f"Unexpected priority: {rule!r}")
    if rule.get("sourceRanges") != sources:
        raise SystemExit(f"Unexpected sources: {rule!r}")
    if rule.get("targetTags") != [target_tag]:
        raise SystemExit(f"Unexpected targets: {rule!r}")
    entries = rule.get(action, [])
    if entries != [{"IPProtocol": "tcp", "ports": [port]}]:
        raise SystemExit(f"Unexpected {action} rules: {rule!r}")

assert_rule(allow, action="allowed", priority=900, sources=[source_cidr])
assert_rule(deny, action="denied", priority=1000, sources=["0.0.0.0/0"])
PY

echo "Phase 2 network configuration is correct."
echo "Cloud Run source subnet: ${source_cidr}"
echo "CARLA origin: 10.150.0.2:${VIDEO_PORT}"
