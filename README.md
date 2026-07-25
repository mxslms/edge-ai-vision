# Edge AI Vision

Real-time object detection with production-style observability. A webcam feed runs through YOLOv8 inference on a GPU, streams annotated video over HTTP, and exposes custom Prometheus metrics so model behavior — latency, detection distribution, confidence drift, camera health — is visible on a Grafana dashboard alongside GPU telemetry.

Built and tested in CI, published to GitHub Container Registry, and deployed to a home GPU server (RTX 3070, Ubuntu, Docker) via Portainer. The same app also runs on an **NVIDIA Jetson Orin Nano** (JetPack 6) for edge / field use; the motivating application is wildlife and fish detection.

![Grafana dashboard showing inference latency percentiles, pipeline throughput, per-class detection timeline, and model confidence gauge](docs/grafana-dashboard.png)

## One app, two platforms

**Do not fork into a second app.** `app.py` is shared. Platform differences live in the container base image and compose files:

| | Home server (x86) | Jetson Orin Nano |
|---|---|---|
| Hardware | RTX 3070 discrete GPU | Orin iGPU (JetPack 6.x) |
| Dockerfile | `Dockerfile` → `ultralytics/ultralytics:latest` | `Dockerfile.jetson` → `ultralytics/ultralytics:latest-jetson-jetpack6` |
| Image tag | `ghcr.io/mxslms/edge-ai-vision:latest` | `ghcr.io/mxslms/edge-ai-vision:jetson` |
| Compose | `docker-compose.yml` | `docker-compose.jetson.yml` |
| GPU runtime | NVIDIA Container Toolkit (`deploy.devices`) | `runtime: nvidia` |
| GPU metrics | `dcgm-exporter` | `jtop` / `tegrastats` (DCGM is for discrete GPUs) |
| CI | Build + smoke on `ubuntu-latest` (amd64) | Build + smoke on `ubuntu-24.04-arm` (arm64), push `:jetson` |

Why separate images: desktop CUDA wheels and Jetson L4T / JetPack CUDA are different ABIs. Pulling the x86 image onto the Orin will not work.

CI builds the Jetson image natively on GitHub’s arm64 runners (thin app layer on Ultralytics’ JetPack 6 base). That proves the image boots and serves HTTP; it does **not** prove Orin GPU / TensorRT performance — still validate once on the device.

## Architecture

```
/dev/video0 (webcam)
      |
      v
OpenCV capture --> YOLOv8n inference --> annotated frames --> Flask MJPEG stream (:5000/video_feed)
                        |
                        v
              Prometheus metrics (:5000/metrics)

x86:    dcgm-exporter (:9400) --> GPU utilization / temp / power
Jetson: jtop / tegrastats     --> GPU / power (host-side)
```

Containers join an external `monitoring-net` Docker network and are scraped by the Prometheus instance in [homelab-infrastructure](https://github.com/mxslms/homelab-infrastructure), which feeds Grafana and Loki.

## Pipeline

Every push and pull request runs the same lint gate, then two image jobs in parallel. Only merges to `main` publish.

```
push / PR  -->  flake8 + Bandit
                      |
          +-----------+-----------+
          |                       |
          v                       v
   build amd64 image        build arm64 Jetson image
   (ubuntu-latest)          (ubuntu-24.04-arm)
          |                       |
          v                       v
   smoke /healthz etc.      smoke /healthz etc.
          |                       |
   (main only)              (main only)
          |                       |
          v                       v
   push :latest             push :jetson
```

A job that fails its smoke test does not publish that tag. The two platforms are independent: an amd64 failure does not block `:jetson`, and vice versa.

## Endpoints

| Route | Purpose |
|---|---|
| `/video_feed` | Annotated MJPEG stream |
| `/metrics` | Prometheus scrape endpoint |
| `/healthz` | Liveness check; returns 200 once the model is loaded, plus active model/device settings |

## Custom metrics

| Metric | Type | Purpose |
|---|---|---|
| `edge_inference_latency_seconds` | Histogram | Per-frame inference time; drives p95/p99 panels |
| `edge_detections_total{class_name}` | Counter | Detection volume partitioned by class |
| `edge_detection_confidence` | Gauge | Most recent detection confidence, tracked as a drift indicator |
| `edge_camera_available` | Gauge | 1 when frames come from a real camera, 0 when synthetic |
| `edge_camera_failures_total` | Counter | Times the camera dropped out mid-stream |

The last two exist because a container can be `Up` and healthy while blind. If the camera is unplugged mid-run, the stream degrades to synthetic frames rather than dying, and the drop shows up as a metric instead of as footage nobody is watching.

## Tunables (environment)

Same variables on both platforms:

| Variable | Default | Notes |
|---|---|---|
| `MODEL_PATH` | `yolov8n.pt` | Any Ultralytics weights / exported engine path |
| `IMG_SIZE` | `640` | Drop to `320` on Orin Nano for more FPS / less power |
| `CONF_THRESHOLD` | `0.25` | Minimum confidence counted and drawn |
| `INFERENCE_DEVICE` | empty (auto) / `0` on Jetson compose | Ultralytics device string |
| `CAMERA_INDEX` | `0` | OpenCV index for `/dev/videoN` |
| `FRAME_SLEEP` | `0.06` | ~15 FPS cap |

## Running on the home server (x86)

Production requires the NVIDIA Container Toolkit and a V4L2 camera at `/dev/video0`. The device mapping is a hard requirement by design: if the camera is missing, the container refuses to start rather than silently serving placeholder frames.

```bash
docker network create monitoring-net
docker compose up -d
```

A test variant runs the same image without claiming the camera, so it can run alongside production on the same host:

```bash
docker compose -f docker-compose.test.yml up -d   # serves synthetic frames on :5001
```

## Running on Jetson Orin Nano

### Recommended deploy approach

**Docker Compose + GHCR image + systemd** on the Orin itself.

| Approach | Use? | Why |
|---|---|---|
| Compose pull `:jetson` + systemd | **Yes (default)** | Matches the x86 Docker model, boots on power-up, low ops overhead |
| Portainer on the Jetson | No (for now) | Fine on the home server; extra moving parts on a single edge box |
| Bare-metal pip / venv | No | JetPack CUDA / PyTorch drift becomes your problem |
| Build every release on-device | Only as fallback | Slow; use when GHCR is unreachable or CI image is missing |

The repo is **private**, so the Orin needs a GitHub PAT (`read:packages`) to pull from GHCR.

### One-shot install

1. Flash **JetPack 6.x** (L4T r36)
2. Install Docker + NVIDIA Container Runtime, then verify:

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
docker run --rm --runtime=nvidia ultralytics/ultralytics:latest-jetson-jetpack6 \
  python -c "import torch; print(torch.cuda.is_available())"   # expect True
```

3. Plug in a USB webcam (`/dev/video0`) or set `CAMERA_INDEX` later
4. Clone and install:

```bash
git clone https://github.com/mxslms/edge-ai-vision.git
cd edge-ai-vision
export GHCR_USER=your-github-username
export GHCR_TOKEN=ghp_xxxxxxxx          # read:packages
./scripts/install-jetson.sh
```

What the installer does:

- logs into `ghcr.io`
- copies compose + `.env` to `/opt/edge-ai-vision`
- creates `monitoring-net`
- pulls `ghcr.io/mxslms/edge-ai-vision:jetson` (or `--build-fallback` to build on-device)
- installs/enables `edge-ai-vision.service` so it starts on boot

```bash
curl -sf http://localhost:5000/healthz
# stream: http://<jetson-ip>:5000/video_feed
```

Tunables live in `/opt/edge-ai-vision/.env` (from `deploy/jetson/env.example`). After edits:

```bash
sudo systemctl restart edge-ai-vision
```

### Manual / optional paths

```bash
# compose only, no systemd
./scripts/install-jetson.sh --no-systemd

# if :jetson is not in GHCR yet
./scripts/install-jetson.sh --build-fallback

# local rebuild / push helpers
./scripts/build-jetson.sh
docker compose -f docker-compose.jetson.test.yml up -d   # synthetic frames on :5001
```

### Performance notes

- Start with `yolov8n.pt` and `IMG_SIZE=640`. If latency is high, set `IMG_SIZE=320` in `.env`.
- For best FPS later, export a TensorRT engine **on the Jetson** (`yolo export model=yolov8n.pt format=engine`) and point `MODEL_PATH` at the `.engine` file. Engines are not portable across GPU architectures.
- CSI cameras need GStreamer / nvargus paths; USB V4L2 works with the current OpenCV capture.
- Power / thermal: use `jtop` on the host; do not expect DCGM to work on Jetson.

## Roadmap

- PIR motion triggering and battery-aware duty cycling on the Orin Nano
- Fine-tune a custom detection model for the target species
- Optional TensorRT export helper + bake `.engine` into the Jetson image
- Prometheus alert rules on `edge_camera_available` and latency drift
