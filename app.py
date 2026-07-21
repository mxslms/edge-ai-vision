import time

import cv2
from flask import Flask, Response
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


def generate_frames():
    cap = cv2.VideoCapture(0)  # /dev/video0

    if not cap.isOpened():
        print("Error: could not open video device.")
        return

    print("Live video stream active...")
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        with LATENCY_HISTOGRAM.time():
            results = model(frame, verbose=False)

        for box in results[0].boxes:
            class_name = model.names[int(box.cls[0])]
            confidence = float(box.conf[0])
            DETECTIONS_COUNTER.labels(class_name=class_name).inc()
            CONFIDENCE_GAUGE.set(confidence)

        annotated_frame = results[0].plot()

        ret, buffer = cv2.imencode('.jpg', annotated_frame)
        if not ret:
            continue

        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')

        # Cap the stream at ~15 FPS
        time.sleep(0.06)

    cap.release()


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
