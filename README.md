# Minimal CARLA on GCP

This repository provisions the first seven phases of the CARLA proof of concept:

1. A `g2-standard-8` Compute Engine VM in `us-east4-a` with one NVIDIA L4.
2. Host and container GPU verification.
3. Docker, NVIDIA Container Toolkit, and CARLA 0.10.0 in off-screen mode.
4. A Python client query that prints the live server version, map, frame, and actor count.
5. A persistent scene driver with 20 Traffic Manager vehicles on autopilot,
   eight managed crosswalk pedestrians, and a configurable vehicle-mounted or
   static RGB camera.
6. A FastAPI browser viewer that JPEG-encodes live camera callbacks and serves an
   MJPEG stream from VM loopback port 8080.
7. A persistent SSH tunnel from Mac loopback port 8080 to the VM's loopback-only
   viewer, with an end-to-end local browser and MJPEG verification.

The project defaults to `xwalk-keyboards-01`, VM `carla-poc`, and zone `us-east4-a`.
No CARLA ports are exposed through a public firewall rule.

## Run

```bash
./scripts/create-vm.sh
gcloud compute scp ./scripts/bootstrap-vm.sh carla-poc:/tmp/bootstrap-vm.sh \
  --project=xwalk-keyboards-01 --zone=us-east4-a
gcloud compute ssh carla-poc \
  --project=xwalk-keyboards-01 --zone=us-east4-a \
  --command='chmod +x /tmp/bootstrap-vm.sh && /tmp/bootstrap-vm.sh'
./scripts/verify-remote.sh
./scripts/deploy-phase5.sh
./scripts/verify-phase5.sh
./scripts/verify-phase6.sh
./scripts/viewer-tunnel.sh start
./scripts/verify-phase7.sh
```

The final command succeeds only after a real `carla.Client` connects to the server
on `127.0.0.1:2000` and queries the current CARLA world.

The Phase 5 verifier requires all of the following from the live simulation:

- exactly 20 managed vehicles
- exactly eight managed pedestrians moving within the selected crosswalk area
- one valid vehicle-mounted or static RGB camera
- live vehicle movement under Traffic Manager
- at least ten camera frames received
- a current, valid 352×240 PNG capture

The scene driver writes its current state and camera samples on the VM under
`~/carla-poc/data/`. It runs in the `carla-scene` container with an
`unless-stopped` restart policy.

The first vehicle uses CARLA's `hero` role, and Traffic Manager runs in hybrid
physics mode so Town10 keeps the relevant map area active. Because the UE5
Town10 traffic can eventually gridlock, a watchdog relocates the hero and up to
four other stalled vehicles to separated spawn points when the camera vehicle has
not moved for ten seconds. Lost non-ego actors are replaced automatically so the
scene maintains 20 vehicles. All vehicles remain under Traffic Manager control.
Only the hero vehicle ignores traffic lights, keeping one vehicle moving while
the other 19 vehicles retain normal signal compliance.

## Static crosswalk camera

Town10 exposes 16 crosswalk polygons through `carla.Map.get_crosswalks()`. The
default live view is an elevated fixed camera aimed at surveyed crosswalk **#8**:

- crosswalk center: `(-62.529, -63.480, 0.0)`
- camera location: `(-80.066, -60.985, 9.6)`
- camera rotation: pitch `-27.242°`, yaw `-8.098°`, roll `0°`
- field of view: `70°`
- crosswalk footprint: approximately `20.33 m × 2.50 m`

The camera faces mostly perpendicular to the crossing so the stripes read as a
horizontal keyboard. A small offset along the crossing introduces a subtle
perspective taper rather than perfect symmetry. This location has no yellow box
junction in the frame. The exact geometry and rendered surveys are stored under
`artifacts/crosswalk-survey/` and `artifacts/crosswalk-view-survey-8/`. Recreate
the map-wide survey on the VM with:

```bash
sudo docker run --rm --network=host \
  --user="$(id -u):$(id -g)" \
  --volume="${HOME}/carla-poc/data:/data" \
  --entrypoint=python3 \
  carla-poc-driver:0.10.0 \
  /app/survey_crosswalks.py
```

Render the keyboard-oriented variants for crosswalk #8 with:

```bash
sudo docker run --rm --network=host \
  --user="$(id -u):$(id -g)" \
  --volume="${HOME}/carla-poc/data:/data" \
  --entrypoint=python3 \
  carla-poc-driver:0.10.0 \
  /app/survey_crosswalk_views.py --crosswalk-id=8
```

The live scene places eight invincible pedestrians in parallel lanes across
crosswalk #8. Because Town10's pedestrian navigation mesh does not span the
middle of this crossing, a constrained controller gives CARLA's walking actors
native direction and speed controls along the surveyed crosswalk axis. CARLA
retains ownership of gait, gravity, ground contact, and collisions. Their routes extend six metres beyond
both ends of the crosswalk, so pedestrians leave the camera frame before reversing
direction. Opposing walkers are staggered across the full round trip so individual
exits do not empty the camera view. A watchdog replaces invalid actors and returns
lateral wanderers to the route; a walker obstructed near an off-camera endpoint
reverses naturally instead of being teleported. Live, moving, and recovery counts
are included in the scene telemetry; live and moving counts appear in the browser viewer.

Pedestrians retain CARLA's default character-movement configuration throughout
the route. The controller never rewrites a walker's transform during normal
motion, leaving gait, gravity, ground contact, and collision response to CARLA.
The native speed input is calibrated for the CARLA 0.10 UE5 runtime's measured
movement scale so the observed speeds remain between 1.1 and 1.65 m/s.

## Camera performance profile

The browser feed matches the approximate 511NY input size used by XWalk
Keyboards: **352×240 at a 20 fps target**. JPEG quality is 75. The camera callback
only copies the latest raw frame; dedicated workers perform JPEG encoding and
periodic PNG snapshots so compression and disk I/O cannot stall CARLA's sensor
delivery thread. If encoding falls behind, stale pending frames are dropped in
favor of the newest frame to preserve low latency.

The viewer reports measured delivered FPS, target FPS, resolution, JPEG size,
total frames, and dropped frames. `./scripts/verify-phase6.sh` and
`./scripts/verify-phase7.sh` require at least 12 observed fps across a three-second
sample and validate a complete 352×240 JPEG frame.

Live measurements on the same `g2-standard-8` VM provide the useful comparison:

| Camera pipeline | Observed FPS | Typical JPEG | Encoder drops |
| --- | ---: | ---: | ---: |
| 1280×720, 10 fps target, blocking callback | 8.2 | 180–210 KB | not measured |
| 352×240, 20 fps target, worker pipeline | 12–17 | 19–22 KB | 0 |

The optimized camera follows CARLA's current simulation tick rate, which varies
with scene/render load. The worker pipeline keeps delivery regular and prevents
JPEG or snapshot work from adding callback stalls. The page automatically
reconnects its MJPEG image if the stream is interrupted.

## Browser viewer

The scene container serves:

- `http://127.0.0.1:8080/` — browser page
- `http://127.0.0.1:8080/stream.mjpg` — multipart MJPEG stream
- `http://127.0.0.1:8080/healthz` — live JSON health

The service binds only to VM loopback. No firewall rule exposes port 8080. Start
the SSH tunnel to make the page available only on this Mac:

```bash
./scripts/viewer-tunnel.sh start
open http://127.0.0.1:8080/
./scripts/viewer-tunnel.sh status
```

The tunnel binds to Mac loopback, uses an SSH control socket under `/tmp`, and
keeps the viewer off the public internet. Verify the page, advancing health
counter, complete MJPEG frame, JPEG dimensions, and local listener with:

```bash
./scripts/verify-phase7.sh
```

Stop the tunnel when it is no longer needed:

```bash
./scripts/viewer-tunnel.sh stop
```

## Stop billing

Stop the VM when it is not in use:

```bash
gcloud compute instances stop carla-poc \
  --project=xwalk-keyboards-01 --zone=us-east4-a
```
