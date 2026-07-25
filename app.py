import os
import time

import cv2
import numpy as np
from flask import Flask, Response, jsonify
from prometheus_client import (CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge,
                               Histogram, generate_latest)
from ultralytics import YOLO

# Shared defaults work on the RTX 3070 host and Jetson Orin Nano.
# Override per platform in compose / Portainer rather than forking the app.
MODEL_PATH = os.environ.get("MODEL_PATH", "yolov8n.pt")
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", "0"))
CONF_THRESHOLD = float(os.environ.get("CONF_THRESHOLD", "0.25"))
IMG_SIZE = int(os.environ.get("IMG_SIZE", "640"))
FRAME_SLEEP = float(os.environ.get("FRAME_SLEEP", "0.06"))


def resolve_inference_device(requested):
    """Normalize INFERENCE_DEVICE. Fall back to CPU when CUDA is requested
    but unavailable (CI arm64 runners, or a Jetson image started without
    --runtime=nvidia). Empty string leaves the choice to Ultralytics."""
    requested = (requested or "").strip()
    if not requested or requested.lower() == "auto":
        return ""
    if requested.lower() == "cpu":
        return "cpu"

    try:
        import torch
        if torch.cuda.is_available():
            return requested
    except Exception as exc:  # pragma: no cover - import/runtime edge cases
        print(f"torch/CUDA probe failed ({exc}); falling back to cpu")
        return "cpu"

    print(
        f"Requested device={requested} but CUDA is unavailable "
        f"(torch.cuda.device_count()=0); falling back to cpu"
    )
    return "cpu"


# Compose on the Orin sets INFERENCE_DEVICE=0; CI overrides / falls back to cpu.
INFERENCE_DEVICE = resolve_inference_device(os.environ.get("INFERENCE_DEVICE", ""))

app = Flask(__name__)

print(
    "Initializing vision system..."
    f" model={MODEL_PATH} imgsz={IMG_SIZE} conf={CONF_THRESHOLD}"
    f" device={INFERENCE_DEVICE or 'auto'} camera={CAMERA_INDEX}"
)
model = YOLO(MODEL_PATH)

# Per-frame inference time on the GPU
LATENCY_HISTOGRAM = Histogram(
    'edge_inference_latency_seconds',
    'Time spent running model inference on a single frame'
)

# Detection volume, partitioned by class label
DETECTIONS_COUNTER = Counter(
    'edge_detections_total',
    'Total number of objects detected by the model',
    ['class_name']
)

# Confidence of the most recent detection; tracked as a lightweight drift indicator
CONFIDENCE_GAUGE = Gauge(
    'edge_detection_confidence',
    'Confidence score of the most recent object detection'
)

# 1 when frames are coming from a real camera, 0 when serving synthetic frames.
# Alert on this: a container can be Up and healthy while blind.
CAMERA_AVAILABLE = Gauge(
    'edge_camera_available',
    'Whether a real camera is supplying frames (1) or frames are synthetic (0)'
)
CAMERA_AVAILABLE.set(0)

# Increments each time the camera drops out mid-stream, so a flaky USB
# connection is visible as a rate rather than a single state flip.
CAMERA_FAILURES = Counter(
    'edge_camera_failures_total',
    'Number of times the camera became unavailable while streaming'
)


def synthetic_frame():
    """A generated frame used when no camera is present (CI, or a test
    container running alongside prod that cannot claim the single webcam).
    Lets the full pipeline run so the HTTP layer and metrics are verifiable
    without hardware. Not real footage, so detections are not meaningful."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    frame[:] = (40, 40, 40)
    cv2.putText(frame, "NO CAMERA - SYNTHETIC FRAME", (60, 240),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 165, 255), 2)
    return frame


def infer_and_annotate(frame):
    """Run inference on one frame, update metrics, return the annotated frame."""
    predict_kwargs = {
        "verbose": False,
        "conf": CONF_THRESHOLD,
        "imgsz": IMG_SIZE,
    }
    if INFERENCE_DEVICE:
        predict_kwargs["device"] = INFERENCE_DEVICE

    with LATENCY_HISTOGRAM.time():
        results = model(frame, **predict_kwargs)

    for box in results[0].boxes:
        class_name = model.names[int(box.cls[0])]
        confidence = float(box.conf[0])
        DETECTIONS_COUNTER.labels(class_name=class_name).inc()
        CONFIDENCE_GAUGE.set(confidence)

    return results[0].plot()


def generate_frames():
    cap = cv2.VideoCapture(CAMERA_INDEX)
    camera_available = cap.isOpened()
    CAMERA_AVAILABLE.set(1 if camera_available else 0)

    if camera_available:
        print(f"Live video stream active (camera index {CAMERA_INDEX})...")
    else:
        print(f"No camera at index {CAMERA_INDEX}; serving synthetic frames.")

    try:
        while True:
            frame = None

            if camera_available:
                ret, frame = cap.read()
                if not ret:
                    # Camera disappeared mid-stream (unplugged, driver reset).
                    # Degrade to synthetic frames rather than killing the
                    # stream, but record it so the drop is visible.
                    print("Camera read failed; falling back to synthetic frames.")
                    camera_available = False
                    CAMERA_AVAILABLE.set(0)
                    CAMERA_FAILURES.inc()
                    cap.release()
                    frame = None

            if frame is None:
                frame = synthetic_frame()

            annotated_frame = infer_and_annotate(frame)

            ok, buffer = cv2.imencode('.jpg', annotated_frame)
            if not ok:
                continue

            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')

            # Cap the stream (~15 FPS at the default 0.06s sleep)
            time.sleep(FRAME_SLEEP)
    finally:
        if cap.isOpened():
            cap.release()


@app.route('/healthz')
def healthz():
    """Liveness check. Returns 200 once the app and model are loaded.
    Does not require a camera, so it is safe to probe from CI."""
    return jsonify(
        status="ok",
        model=MODEL_PATH,
        imgsz=IMG_SIZE,
        conf=CONF_THRESHOLD,
        device=INFERENCE_DEVICE or "auto",
    ), 200


@app.route('/metrics')
def metrics():
    """Prometheus scrape endpoint."""
    return Response(generate_latest(REGISTRY), mimetype=CONTENT_TYPE_LATEST)


@app.route('/video_feed')
def video_feed():
    """Annotated MJPEG stream."""
    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


if __name__ == '__main__':
    # Binding to all interfaces is intentional: this runs inside a container
    # on a private network and must be reachable by the Docker host.
    app.run(host='0.0.0.0', port=5000, threaded=True)  # nosec B104
