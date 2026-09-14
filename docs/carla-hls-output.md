# CARLA HLS Output Plan

## Goal

Make the fixed CARLA Town10 crosswalk camera behave like another live traffic
camera registered with XWalk Keyboards. The existing browser pipeline should
continue to own playback, Roboflow WebRTC person detection, stripe assignment,
visual highlights, and Web Audio output.

The CARLA VM should remain stopped while work that does not require the GPU is
underway. Restart it only for focused integration and capture windows.

## Proposed architecture

```text
CARLA RGB camera
  -> local MJPEG stream
  -> FFmpeg HLS adapter on carla-poc
  -> private VM address 10.150.0.2
  -> XWalk Keyboards Cloud Run HLS proxy
  -> browser video.captureStream()
  -> existing Roboflow detection
  -> stripe highlights and keyboard audio
```

The XWalk Keyboards Cloud Run service should reach the VM over its private
address using Direct VPC egress with `private-ranges-only`. The CARLA video
origin should not be exposed through a public firewall rule. Direct VPC egress
does not require a continuously running Serverless VPC Access connector.

Reference: [Google Cloud Direct VPC egress documentation](https://docs.cloud.google.com/run/docs/configuring/vpc-direct-vpc)

## Phase 1: Produce HLS on the CARLA VM

Add FFmpeg to the scene-driver image and supervise an HLS adapter alongside the
existing FastAPI viewer. The adapter should read the loopback-only MJPEG stream
and maintain a short rolling HLS window.

Serve these endpoints:

- `/live/playlist.m3u8`
- `/live/segment-*.ts`
- `/snapshot.jpg`
- `/healthz`

Keep only several recent segments so disk use stays bounded. Add HLS readiness,
latest-segment age, and encoder-process state to health telemetry. Preserve the
initial 352 x 240 frame size so playback and inference can be compared directly
with the 511NY inputs.

Verification:

- The playlist advances continuously.
- Every listed segment exists and decodes.
- The decoded dimensions are 352 x 240.
- Restarting the adapter recovers without restarting CARLA.
- HLS failure is visible through `/healthz`.

## Phase 2: Keep the origin private

The stopped `carla-poc` VM currently has private address `10.150.0.2` on the
project's `default` VPC in `us-east4`. The XWalk Keyboards Cloud Run service is
in `us-central1` and does not currently have VPC egress configured.

Configure Cloud Run Direct VPC egress against a subnet in `us-central1`, routing
only private address ranges through the VPC. Add a narrowly scoped firewall rule
that permits the Cloud Run source range or network tag to reach the CARLA video
port. Do not expose CARLA RPC ports or the HLS origin to the public internet.

The concrete configuration uses the `default` subnet in `us-central1`
(`10.128.0.0/20`) as the allowed source and the `carla-hls-origin` VM target
tag. An allow rule at priority 900 permits that subnet to TCP 8080; a deny rule
at priority 1000 rejects every other source to TCP 8080. The explicit deny is
required because the default VPC already has a lower-priority broad internal
allow rule. The XWalk service uses the `xwalk-cloud-run` revision tag and
`private-ranges-only` Direct VPC egress.

Apply and inspect the configuration without starting the GPU VM:

```bash
./scripts/configure-phase2-vpc.sh
./scripts/verify-phase2-network.sh
```

Once the VM is running with the HLS origin deployed, run the live gate:

```bash
./scripts/verify-phase2-live.sh
```

The live verifier executes an on-demand Cloud Run job on the same Direct VPC
path, fetches both the private CARLA playlist and a listed media segment, proves
the VM's public address cannot reach port 8080, and confirms the existing 511NY
proxy still returns a real media segment. The probe job remains idle between
manual verification runs and does not keep a Cloud Run instance active.

### Phase 2 completion record (2026-09-13)

- Cloud Run revision `xwalk-keyboards-00083-jq8` was configured with the
  `default` network and subnet, the `xwalk-cloud-run` tag, and
  `private-ranges-only` egress.
- Probe execution `carla-hls-vpc-probe-tlmfv` fetched the private playlist and
  `segment-000031.ts` (357,576 bytes) from `10.150.0.2:8080` and exited
  successfully.
- During that same running-VM window, the public request to
  `34.86.250.127:8080` timed out while the existing camera 5056 proxy returned
  a 141,376-byte MPEG-TS segment.
- The GitHub deployment identity has subnet-scoped `roles/compute.networkUser`;
  the deployment workflow supplies and verifies the Direct VPC settings on
  every revision. A no-op deployment of the exact production image without
  VPC flags created revision `xwalk-keyboards-00084-6n2`, after which the
  network verifier still passed; this also confirms the pre-change workflow's
  existing-service deployment behavior preserves the settings.
- `carla-poc` was returned to `TERMINATED` after the live gate passed.

Verification:

- Cloud Run can fetch the private HLS playlist and segments.
- A request from the public internet cannot reach the VM video port.
- Existing 511NY HLS proxy requests continue to work normally.
- Subsequent GitHub Actions deployments preserve the Cloud Run VPC settings.

## Phase 3: Register CARLA in XWalk Keyboards

Reserve a non-511NY numeric camera ID, provisionally `90014`, for Town10
crosswalk 14. Suggested metadata:

- camera key: `camera_90014`
- location: `Town10 - Crosswalk 14`
- status label: `CARLA TOWN10 @ XWALK 14`
- source ID: `carla-town10-crosswalk-14`
- base anchor: `C4`

Keep the private upstream URL in a server-only environment variable such as
`CARLA_HLS_BASE_URL`. Do not place the VM address in data imported by client
components. Generalize the HLS proxy's server-side source resolution so the
existing 511NY records continue using their public URLs while camera 90014 uses
the private CARLA origin.

The existing Realtime page should continue to request a same-origin URL:

```text
/api/hls/90014/playlist.m3u8
```

No parallel inference path should be introduced. The existing `hls.js` player,
`video.captureStream()`, Roboflow WebRTC connection, client-side detection
mapping, canvas overlay, and Web Audio instrument should process CARLA exactly
as they process a 511NY source.

Verification:

- Camera 90014 appears in the registered live-camera list.
- `/realtime/90014` is generated and unknown IDs still return 404.
- Path traversal and unregistered source requests remain rejected.
- CARLA origin failures become the existing feed-reconnecting/down states.
- Tests prove that private upstream configuration is absent from client code.

### Phase 3 completion record (2026-09-13)

- XWalk branch `feature/carla-hls-source` registers camera `90014` with the
  planned key, location, status label, source ID, and `C4` base anchor.
- Client camera metadata no longer contains any upstream HLS URL. A
  `server-only` resolver owns all four 511NY directories and reads CARLA only
  from `CARLA_HLS_BASE_URL`.
- The allowlisted proxy rejects unknown cameras and traversal paths, returns a
  retryable service error when CARLA is unconfigured, and maps connection
  failures to its existing bad-gateway behavior.
- The production build generated `/realtime/90014`; browser tests received 200
  for that route, 404 for `/realtime/99999`, and observed the existing
  `FEED RECONNECTING` state when the CARLA proxy returned 502.
- Production Cloud Run revision `xwalk-keyboards-00085-gq9` carries
  `CARLA_HLS_BASE_URL` as a server-only environment value and retained the
  Phase 2 Direct VPC settings. The CARLA application changes remain on their
  feature branch pending the normal review and merge deployment.
- Production 511NY camera 5056 still returned a valid 141,188-byte media
  segment after the environment update. The CARLA GPU VM remained stopped.

## Phase 4: Calibrate the simulated crosswalk

Capture one clean native 352 x 240 CARLA frame and run it through the existing
calibration agent. Review and correct every stripe polygon, with special
attention to the center island and the two independently hulled crosswalk runs.

Commit a `calibration-fallback-90014.json` file to XWalk Keyboards. Because the
CARLA camera transform is static, this fallback should remain aligned and a
scheduled drift-calibration job is unnecessary. The existing debug-panel
recalibration upload can still be used during testing because it submits the
browser's current frame directly to the calibration agent.

Verification:

- Every visible stripe is represented in left-to-right order.
- The center island is not playable.
- Foot points slightly beyond the painted stripe still land in the intended
  expanded hit region.
- The resulting keyboard ascends chromatically from the configured `C4` anchor.

### Phase 4 completion record (2026-09-13)

- A native `352 x 240` frame was selected from five live samples and submitted
  to the deployed calibration agent as camera `90014`. The archived source is
  `gs://xwalk-keyboards-01/calibration/history/camera_90014/run-20260913T211548Z-0ede54.jpg`.
- The agent detected all 20 visible painted stripes. A 4x overlay review showed
  every polygon aligned with its stripe; the only required correction was to
  split the generic agent's single run at the center island.
- XWalk now carries `public/calibration-fallback-90014.json` with ten ordered
  stripes in `segment0` and ten in `segment1`. The reviewed file was also
  written to the live GCS object and read back byte-for-byte as a 20-stripe,
  two-segment calibration.
- Fixture tests exercise the actual fallback through XWalk's production
  polygon processing and detection mapping. They prove the two segment hulls
  leave the island silent, the expanded geometry accepts a foot point just
  beyond the paint, and the 20 notes rise one semitone at a time from `C4`.
- No scheduled recalibration was added for this static camera. The on-demand
  debug upload remains available for testing. The `carla-poc` GPU VM was
  stopped after capture.

## Phase 5: Test without the GPU where possible

Use a fixture HLS stream for XWalk Keyboards unit and integration work while the
CARLA VM remains stopped. Add tests for the registry entry, server-only origin
resolution, playlist/segment proxy behavior, calibration parsing, and route
generation.

Run the XWalk Keyboards validation suite:

```bash
pnpm test
pnpm lint
pnpm build
```

Use its Playwright scenarios to verify feed startup, feed loss, route cleanup,
fullscreen behavior, and the five-minute inference pause without consuming L4
time.

### Phase 5 completion record (2026-09-13)

- XWalk includes a finite four-second HLS fixture under `e2e/fixtures/hls`.
  Chromium consumes its real manifest and MPEG-TS media through camera 90014's
  same-origin paths and reports the expected native `352 x 240` video size.
- The CARLA lifecycle scenarios prove natural hls.js startup, explicit origin
  failure handling, player teardown on client-side route change, pseudo-
  fullscreen entry and Escape exit, and the real five-minute inference timer
  using Playwright's deterministic clock. The feed remains live while
  inference is paused.
- Unit coverage proves camera registration and route generation, server-only
  origin resolution, manifest and binary-segment proxy responses, malformed
  source/path rejection, fallback calibration parsing, independent segment
  hulls, expanded hit regions, and chromatic note assignment.
- `pnpm test` passed 82 tests, `pnpm lint` and TypeScript checking passed, the
  production build generated camera 90014's route, and Playwright passed all
  21 scenarios. `carla-poc` remained terminated throughout this phase.

## Phase 6: Focused end-to-end window

Restart `carla-poc` only after offline validation passes. Deploy the CARLA HLS
adapter, confirm private connectivity, and open `/realtime/90014`.

Confirm all of the following:

- HLS video starts and remains stable.
- The browser captures the native 352 x 240 frame.
- Roboflow returns person detections for CARLA pedestrians.
- Detection foot points fall inside the calibrated crosswalk regions.
- Occupied stripes illuminate and trigger the expected notes.
- Camera and inference recovery remain independent.
- The current viewport and default AI pedestrian behavior are suitable for the
  final screen recording.

Adjust the camera transform or calibration as necessary, then record the final
screen captures while the complete keyboard pipeline is running.

## Phase 7: Pre-production test and POC shutdown

Treat the temporary CARLA integration as a controlled pre-production test on
`xwalkkeyboards.app`. Implement and review the work as two pull requests:

1. `carla-min-gcp`: HLS adapter, private origin, telemetry, and verification.
2. `xwalk-keyboards`: source registry, private proxy resolution, calibration,
   tests, and documentation.

Merge and deploy both pull requests for a focused test window. While
`carla-poc` is running, verify the production path end to end:

1. Camera 90014 is selectable on `xwalkkeyboards.app` and its HLS feed starts.
2. Roboflow detections drive the calibrated stripe glow and keyboard audio.
3. Feed, inference, fullscreen, and five-minute pause behavior match the
   existing traffic-camera experience.
4. The desired screen recordings are captured.

At the end of the test window, stop `carla-poc` and verify its state is
`TERMINATED`. Leave camera 90014 registered and retain its server-side source,
Direct VPC egress, firewall rules, probe job, IAM binding, and VM tag for now.
With the POC stopped, XWalk Keyboards' existing calibration availability check
should detect that camera 90014 is unavailable; no special CARLA-only fallback
or immediate registry removal is required.

This shutdown ends L4 GPU billing while preserving a reviewable, restartable
integration for a later test window. Retain or delete the persistent disk and
reserved address deliberately because they can continue to incur small charges.
Remove the registry entry and supporting private-network configuration only in a
separate cleanup change if the experiment is retired permanently.

## Success criteria

The pre-production test is complete when CARLA appears as a normal selectable
Realtime camera on `xwalkkeyboards.app`, reaches the browser only through the
existing same-origin HLS proxy, feeds the unchanged Roboflow detection path, and
produces accurate stripe glow and audio. After recording, `carla-poc` is
terminated and camera 90014 transitions through the site's normal unavailable-
camera behavior without affecting the four existing 511NY cameras.
