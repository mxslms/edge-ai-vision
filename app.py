import cv2
import time
from flask import Flask, Response
from ultralytics import YOLO
# 1. Import the Prometheus tracking tools
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST, REGISTRY, Histogram, Counter, Gauge

app = Flask(__name__)

print("Initializing Vision System...")
model = YOLO('yolov8n.pt')

# --- INITIALIZE PROMETHEUS METRICS ---
# Latency: Measures how many seconds the RTX 3070 takes to crunch a frame
LATENCY_HISTOGRAM = Histogram(
    'edge_inference_latency_seconds', 
    'Time spent running model inference on a single frame'
)

# Class Distribution: Counts every object detected, partitioned by its class name
DETECTIONS_COUNTER = Counter(
    'edge_detections_total', 
    'Total number of objects detected by the model', 
    ['class_name']
)

# Confidence: A rolling indicator of how confident the model is in its latest prediction
CONFIDENCE_GAUGE = Gauge(
    'edge_detection_confidence', 
    'Confidence score of the most recent object detection (internal drift indicator)'
)


def generate_frames():
    # Target /dev/video0
    cap = cv2.VideoCapture(0)
    
    if not cap.isOpened():
        print("Error: Could not open video device.")
        return

    print("Live video stream active...")
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # 2. TIME THE INFERENCE: Wrap the YOLO call in the histogram's time context
        with LATENCY_HISTOGRAM.time():
            results = model(frame, verbose=False)

        # 3. PARSE DETECTIONS: Extract names and confidence scores for Prometheus
        # results[0].boxes holds all bounding boxes found in the current frame
        if len(results[0].boxes) > 0:
            for box in results[0].boxes:
                # Extract class index and look up its string name (e.g., 'person', 'dog')
                class_id = int(box.cls[0])
                class_name = model.names[class_id]
                
                # Extract confidence score (0.0 to 1.0)
                confidence = float(box.conf[0])
                
                # Increment the counter for this specific label
                DETECTIONS_COUNTER.labels(class_name=class_name).inc()
                
                # Update the gauge to reflect this current detection
                CONFIDENCE_GAUGE.set(confidence)

        # Plot the bounding boxes and labels directly onto the frame
        annotated_frame = results[0].plot()

        # Encode the frame as a JPEG
        ret, buffer = cv2.imencode('.jpg', annotated_frame)
        if not ret:
            continue
            
        frame_bytes = buffer.tobytes()

        # Stream the bytes as an MJPEG multipart chunk
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        
        # Cap the stream at ~15 FPS to keep things smooth
        time.sleep(0.06)

    cap.release()


# --- THE METRICS ENDPOINT ---
@app.route('/metrics')
def metrics():
    """
    Exposes the raw metrics text file. Prometheus scrapes this route
    to pull your latency, counts, and confidence gauges down into its database.
    """
    return Response(generate_latest(REGISTRY), mimetype=CONTENT_TYPE_LATEST)


@app.route('/video_feed')
def video_feed():
    # Return the dynamic MJPEG stream
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')


if __name__ == '__main__':
    # Run the web server on port 5000, accessible to your local network
    app.run(host='0.0.0.0', port=5000, threaded=True) # nosec B104