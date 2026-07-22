import time

import cv2
import numpy as np
from flask import Flask, Response, jsonify
from prometheus_client import (CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge,
                               Histogram, generate_latest)
from ultralytics import YOLO

app = Flask(__name__)

print("Initializing vision system...")
model = YOLO('yolov8n.pt')

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
    with LATENCY_HISTOGRAM.time():
        results = model(frame, verbose=False)

    for box in results[0].boxes:
        class_name = model.names[int(box.cls[0])]
        confidence = float(box.conf[0])
        DETECTIONS_COUNTER.labels(class_name=class_name).inc()
        CONFIDENCE_GAUGE.set(confidence)

    return results[0].plot()


def generate_frames():
    cap = cv2.VideoCapture(0)  # /dev/video0
    camera_available = cap.isOpened()
    CAMERA_AVAILABLE.set(1 if camera_available else 0)

    if camera_available:
        print("Live video stream active...")
    else:
        print("No camera at /dev/video0; serving synthetic frames.")

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

            # Cap the stream at ~15 FPS
            time.sleep(0.06)
    finally:
        if cap.isOpened():
            cap.release()


@app.route('/healthz')
def healthz():
    """Liveness check. Returns 200 once the app and model are loaded.
    Does not require a camera, so it is safe to probe from CI."""
    return jsonify(status="ok"), 200


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