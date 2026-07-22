# Edge AI Vision

Real-time object detection service with production-style observability. A webcam feed runs through YOLOv8 inference on a GPU, streams annotated video over HTTP, and exposes custom Prometheus metrics so model behavior (latency, detection distribution, confidence drift) is visible on a Grafana dashboard alongside GPU telemetry.

Currently running on a home GPU server (RTX 3070, Ubuntu, Docker). Deployment target is an NVIDIA Jetson Orin Nano for battery-powered field use; the motivating application is wildlife and fish detection.

## Architecture

```
/dev/video0 (webcam)
      |
      v
OpenCV capture --> YOLOv8n inference --> annotated frames --> Flask MJPEG stream (:5000/video_feed)
                        |
                        v
              Prometheus metrics (:5000/metrics)

dcgm-exporter (:9400) --> GPU utilization / temp / power
```

Both containers join an external `monitoring-net` Docker network and are scraped by the Prometheus instance in my [homelab-infrastructure](https://github.com/mxslms/homelab-infrastructure) repo, which feeds Grafana and Loki.

## Custom metrics

| Metric | Type | Purpose |
|---|---|---|
| `edge_inference_latency_seconds` | Histogram | Per-frame inference time |
| `edge_detections_total{class_name}` | Counter | Detection volume partitioned by class |
| `edge_detection_confidence` | Gauge | Most recent detection confidence, tracked as a lightweight drift indicator |

<!-- TODO: add Grafana dashboard screenshot here -->

## Running it

Requires the NVIDIA Container Toolkit, a V4L2 webcam at `/dev/video0`, and the external network:

```bash
docker network create monitoring-net
docker compose up -d --build
```

- Live annotated stream: `http://<host>:5000/video_feed`
- Metrics: `http://<host>:5000/metrics`
- GPU telemetry: `http://<host>:9400/metrics`

## CI

GitHub Actions runs on every push and PR: flake8 (fail on syntax/undefined names), Bandit security scan of the application code.

## Roadmap

- Deploy to Jetson Orin Nano (8GB) with PIR motion triggering for battery operation
- Fine-tune a custom detection model for the target species
- Alerting rules on latency and confidence-drift metrics
