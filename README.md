# Minimal CARLA on GCP

This repository provisions the first seven phases of the CARLA proof of concept
and the first phase of its live-camera integration:

1. A `g2-standard-8` Compute Engine VM in `us-east4-a` with one NVIDIA L4.
2. Host and container GPU verification.
3. Docker, NVIDIA Container Toolkit, and CARLA 0.10.0 in off-screen mode.
4. A Python client query that prints the live server version, map, frame, and actor count.
5. A persistent scene driver with 20 Traffic Manager vehicles on autopilot,
   16 stock AI-controlled crosswalk pedestrians, and a configurable
   vehicle-mounted or static RGB camera.
6. A FastAPI browser viewer that JPEG-encodes live camera callbacks and serves an
   MJPEG stream from VM loopback port 8080.
7. A persistent SSH tunnel from Mac loopback port 8080 to the VM's loopback-only
   viewer, with an end-to-end local browser and MJPEG verification.
8. A supervised FFmpeg adapter that converts the MJPEG feed into a bounded,
   rolling 352×240 HLS stream for XWalk Keyboards.

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
- exactly 16 stock `controller.ai.walker` pedestrians, with at least one moving
  inside the selected crosswalk observation area
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
default live view on this branch is an elevated fixed camera aimed at surveyed
crosswalk **#14**:

- crosswalk center: `(-91.110, 18.221, 0.0)`
- camera location: `(-113.041, 21.270, 12.0)`
- camera rotation: pitch `-27.242°`, yaw `-7.917°`, roll `0°`
- field of view: `70°`

The camera faces mostly perpendicular to the crossing so the stripes read as a
horizontal keyboard with a slight perspective offset. The yellow box junction is
left visible because this experiment prioritizes observing CARLA's default
pedestrian navigation and traffic-light behavior.

The navigation-mesh survey samples 30,000 pedestrian locations and ranks all 16
Town10 crosswalks by coverage through their center and on both approaches.
Crosswalk #14 ranked first. A separate live probe spawned four stock AI walkers
on its approaches; all four reached the opposite side through the crosswalk
center within 45 seconds. Run those checks inside the driver image with:

```bash
sudo docker run --rm --network=host \
  --user="$(id -u):$(id -g)" \
  --volume="${HOME}/carla-poc/data:/data" \
  --entrypoint=python3 \
  carla-poc-driver:0.10.0 \
  /app/survey_navmesh_crosswalks.py --output=/data/navmesh-crosswalks.json

sudo docker run --rm --network=host \
  --user="$(id -u):$(id -g)" \
  --volume="${HOME}/carla-poc/data:/data" \
  --entrypoint=python3 \
  carla-poc-driver:0.10.0 \
  /app/probe_ai_crosswalk.py --crosswalk-id=14 --walkers=4 --duration=45
```

Render keyboard-oriented camera variants for crosswalk #14 with:

```bash
sudo docker run --rm --network=host \
  --user="$(id -u):$(id -g)" \
  --volume="${HOME}/carla-poc/data:/data" \
  --entrypoint=python3 \
  carla-poc-driver:0.10.0 \
  /app/survey_crosswalk_views.py --crosswalk-id=14
```

The live scene places 16 invincible pedestrian actors near the two approaches and
attaches CARLA's stock `controller.ai.walker` to each one. The application only
assigns an opposite-side navigation target and a natural walking speed of
1.1–1.65 m/s. CARLA owns the route, traffic-light decisions, gait, gravity,
ground contact, and collisions; the application does not steer or rewrite walker
transforms. When an AI controller reaches its endpoint, it receives the opposite
endpoint as its next destination. The world pedestrian crossing factor is set to
`1.0` so every walker is permitted to cross roads. Live, moving, controller, and
recovery counts are exposed to the verifier and browser telemetry.

The earlier deterministic crosswalk #8 scene remains available by launching
`simulation.py` with `--pedestrian-mode=manual --crosswalk-id=8` and its original
camera coordinates. That mode is useful for repeatable keyboard-like motion;
the default deployment on this branch is the stock-AI observation experiment.

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

The scene container serves on port 8080:

- `/` — browser page
- `/stream.mjpg` — multipart MJPEG stream
- `/healthz` — live JSON health

The service binds to the VM network interface so XWalk Keyboards can reach it
through Direct VPC egress. Firewall rules permit TCP 8080 only from the Cloud
Run subnet and explicitly deny all other sources to that port. CARLA RPC ports
remain private. The SSH tunnel is still the supported way to view the origin
from this Mac:

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

## HLS output

The scene container supervises an FFmpeg child process that reads the MJPEG feed
over container loopback and writes a four-segment live HLS playlist. Completed
files are published atomically, and obsolete segments are deleted so the output
directory stays bounded. If FFmpeg exits, the supervisor reports the failure,
clears stale HLS artifacts, and starts a fresh encoder without restarting CARLA.

The viewer additionally serves on port 8080:

- `/live/playlist.m3u8` — live HLS playlist
- `/live/segment-*.ts` — MPEG-TS media segments
- `/snapshot.jpg` — current 352×240 JPEG

`/healthz` includes HLS readiness, encoder state and PID, restart count, last
exit/error details, latest-segment age, and playlist/disk segment counts. Run the
destructive encoder-only recovery check after deployment with:

```bash
./scripts/verify-phase1-hls.sh
```

The verifier checks playlist advancement, downloads and decodes every listed
segment with `ffprobe`, verifies 352×240 dimensions and the JPEG snapshot, then
terminates only FFmpeg. It must observe the unhealthy state, a new encoder PID,
continued CARLA camera frames, and a newly advancing playlist.

## Stop billing

Stop the VM when it is not in use:

```bash
gcloud compute instances stop carla-poc \
  --project=xwalk-keyboards-01 --zone=us-east4-a
```
