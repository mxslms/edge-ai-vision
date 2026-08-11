import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

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

# Motion gating -- the capture loop grabs frames continuously (cheap) but
# only runs YOLO while a downscaled frame-difference exceeds threshold,
# plus a short trailing window after the last motion. PIR is deliberately
# not used here: unreliable for small subjects at close range.
MOTION_ENABLED = os.environ.get("MOTION_ENABLED", "true").lower() == "true"
MOTION_THRESHOLD = float(os.environ.get("MOTION_THRESHOLD", "0.005"))
MOTION_TRAILING_SECONDS = float(os.environ.get("MOTION_TRAILING_SECONDS", "4.0"))
SHOW_MOTION_OVERLAY = os.environ.get("SHOW_MOTION_OVERLAY", "true").lower() == "true"

CAMERA_RETRY_SECONDS = float(os.environ.get("CAMERA_RETRY_SECONDS", "5"))

# Training-data capture. SAVE_CONF_FLOOR is deliberately lower than
# CONF_THRESHOLD -- uncertain detections are the most valuable training
# data, and are never shown on the live stream or counted in
# edge_detections_total, only saved to disk.
SAVE_ENABLED = os.environ.get("SAVE_ENABLED", "true").lower() == "true"
# Default matches the in-container mount point in docker-compose.jetson.yml
# (the host-side path is separately configurable there via
# CAPTURE_DIR_HOST) -- change one, change both, or captures silently land
# somewhere that isn't actually persisted.
CAPTURE_DIR = os.environ.get("CAPTURE_DIR", "/data/captures")
SAVE_CONF_FLOOR = float(os.environ.get("SAVE_CONF_FLOOR", "0.25"))
SAVE_MIN_INTERVAL_SECONDS = float(os.environ.get("SAVE_MIN_INTERVAL_SECONDS", "2.5"))
SAVE_CROPS = os.environ.get("SAVE_CROPS", "true").lower() == "true"

# Retention -- both caps enforced oldest-date-partition-first. Bytes cap
# exists because days alone doesn't actually bound disk (a long, busy day
# can be enormous); this is what actually protects an unattended device.
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "5"))
RETENTION_MAX_BYTES = int(os.environ.get("RETENTION_MAX_BYTES", str(10 * 1024**3)))
RETENTION_INTERVAL_SECONDS = float(os.environ.get("RETENTION_INTERVAL_SECONDS", "3600"))


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
    f" motion_gate={MOTION_ENABLED} save={SAVE_ENABLED}"
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

# Training-data capture metrics
FRAMES_SAVED_COUNTER = Counter(
    'edge_frames_saved_total',
    'Total number of frames saved to disk as training data'
)
FRAME_SAVE_FAILURES = Counter(
    'edge_frame_save_failures_total',
    'Number of times writing a captured frame/label to disk failed'
)
CAPTURE_DIR_BYTES = Gauge(
    'edge_capture_dir_bytes',
    'Total size of the capture directory in bytes'
)
MOTION_DETECTED_COUNTER = Counter(
    'edge_motion_detected_total',
    'Number of frames in which frame-differencing motion exceeded threshold'
)

# Set once per capture-loop iteration. time() - this > ~60s means the loop
# is wedged (still "running" but not making progress) rather than dead --
# a plain except-and-continue loop can hide that without a heartbeat.
LOOP_HEARTBEAT = Gauge(
    'edge_loop_heartbeat_timestamp',
    'Unix timestamp of the most recent capture loop iteration'
)


class FrameBuffer:
    """Thread-safe single-slot 'latest frame' broadcast to N readers.

    The capture loop is the sole producer; any number of /video_feed
    requests are readers. A Condition (not a Queue) is used deliberately:
    a Queue is consume-once, so two open browser tabs would steal frames
    from each other. Readers block until a newer frame is published (or a
    timeout elapses, so a wedged producer can't hang a viewer forever) and
    each independently tracks the last sequence number it has seen, so
    slow readers simply skip intermediate frames rather than falling
    behind in a growing queue.
    """

    def __init__(self):
        self._condition = threading.Condition()
        self._jpeg = None
        self._sequence = 0

    def publish(self, jpeg_bytes):
        with self._condition:
            self._jpeg = jpeg_bytes
            self._sequence += 1
            self._condition.notify_all()

    def get_latest(self, last_seen_sequence, timeout=5.0):
        """Block until sequence advances past last_seen_sequence, or
        timeout. Returns (jpeg_bytes_or_None, current_sequence)."""
        with self._condition:
            if self._sequence == last_seen_sequence:
                self._condition.wait(timeout=timeout)
            return self._jpeg, self._sequence


frame_buffer = FrameBuffer()


class MotionDetector:
    """Cheap frame-differencing motion gate.

    Downscales to a small grayscale thumbnail, blurs it (so sensor noise
    and lighting flicker don't constantly trip the gate), and diffs
    against the previous thumbnail. Motion is "active" for a trailing
    window after the last frame that exceeded threshold, not just the
    instant it happened, so a subject that pauses mid-frame doesn't cut
    inference off mid-detection.
    """

    def __init__(self, threshold, trailing_seconds):
        self.threshold = threshold
        self.trailing_seconds = trailing_seconds
        self._prev_small = None
        self._last_motion_time = 0.0

    def reset(self):
        """Call after reopening the camera -- otherwise the first frame
        after a reconnect diffs against a stale pre-drop thumbnail and
        reads as a large, meaningless motion spike."""
        self._prev_small = None
        self._last_motion_time = 0.0

    def update(self, frame):
        """Feed one frame. Returns True if inference should run this
        frame (motion now, or within the trailing window of the last
        motion)."""
        small = cv2.cvtColor(cv2.resize(frame, (160, 90)), cv2.COLOR_BGR2GRAY)
        small = cv2.GaussianBlur(small, (5, 5), 0)

        motion_now = False
        if self._prev_small is not None:
            diff = cv2.absdiff(small, self._prev_small)
            _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
            changed_fraction = cv2.countNonZero(thresh) / thresh.size
            motion_now = changed_fraction > self.threshold

        self._prev_small = small

        now = time.time()
        if motion_now:
            self._last_motion_time = now
            MOTION_DETECTED_COUNTER.inc()

        return (now - self._last_motion_time) < self.trailing_seconds


_last_save_time = 0.0


def synthetic_frame():
    """A generated frame used when no camera is present (CI, or a test
    container running alongside prod that cannot claim the single webcam).
    Lets the full pipeline run so the HTTP layer and metrics are verifiable
    without hardware. Includes a moving element deliberately: a static
    synthetic frame produces zero frame-to-frame difference, which would
    make the motion gate silently stop feeding it to inference at all once
    motion gating shipped -- the capture-loop, motion-gate, and metrics
    code would then go completely unexercised in CI. Not real footage, so
    detections from it are not meaningful, and the capture loop never
    saves from synthetic frames (gated on camera_available)."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    frame[:] = (40, 40, 40)
    x = int((time.time() * 80) % 600)
    cv2.rectangle(frame, (x, 380), (x + 30, 420), (0, 165, 255), -1)
    cv2.putText(frame, "NO CAMERA - SYNTHETIC FRAME", (60, 240),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 165, 255), 2)
    return frame


def infer_and_annotate(frame):
    """Run inference on one frame at the display confidence threshold,
    update the display-facing metrics, return the annotated frame.
    Unchanged from the original per-request version -- the save path
    below runs its own separate, lower-confidence inference pass instead
    of sharing this one, so nothing here affects what viewers see."""
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


def write_capture_files(frame, boxes, class_names):
    """Write one clean (un-annotated) frame + YOLO-format label file +
    JSON metadata sidecar + per-detection crops. Never raises -- a full
    disk or permissions problem must not take down the capture loop."""
    try:
        h, w = frame.shape[:2]
        ts = datetime.now(timezone.utc)
        date_dir = ts.strftime("%Y-%m-%d")
        uid = f"{ts.strftime('%Y%m%dT%H%M%S')}-{int(ts.microsecond / 1000):03d}"

        base = Path(CAPTURE_DIR) / date_dir
        images_dir = base / "images"
        labels_dir = base / "labels"
        meta_dir = base / "meta"
        crops_dir = base / "crops"
        for d in (images_dir, labels_dir, meta_dir):
            d.mkdir(parents=True, exist_ok=True)
        if SAVE_CROPS:
            crops_dir.mkdir(parents=True, exist_ok=True)

        cv2.imwrite(str(images_dir / f"{uid}.jpg"), frame)

        label_lines = []
        detections_meta = []
        for i, box in enumerate(boxes):
            cls_id = int(box.cls[0])
            conf = float(box.conf[0])
            xc, yc, bw, bh = box.xywhn[0].tolist()
            label_lines.append(f"{cls_id} {xc:.6f} {yc:.6f} {bw:.6f} {bh:.6f}")
            detections_meta.append({
                "class_id": cls_id,
                "class_name": class_names[cls_id],
                "confidence": conf,
            })

            if SAVE_CROPS:
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                x1, y1 = max(0, int(x1)), max(0, int(y1))
                x2, y2 = min(w, int(x2)), min(h, int(y2))
                if x2 > x1 and y2 > y1:
                    crop_name = f"{uid}__{i}__{class_names[cls_id]}.jpg"
                    cv2.imwrite(str(crops_dir / crop_name), frame[y1:y2, x1:x2])

        (labels_dir / f"{uid}.txt").write_text(
            "\n".join(label_lines) + "\n", encoding="utf-8"
        )
        (meta_dir / f"{uid}.json").write_text(json.dumps({
            "timestamp": ts.isoformat(),
            "image_width": w,
            "image_height": h,
            "model": MODEL_PATH,
            "conf_threshold_display": CONF_THRESHOLD,
            "conf_floor_save": SAVE_CONF_FLOOR,
            "detections": detections_meta,
        }, indent=2), encoding="utf-8")

        FRAMES_SAVED_COUNTER.inc()
    except OSError as exc:
        FRAME_SAVE_FAILURES.inc()
        print(f"capture save failed: {exc}")


def maybe_save_capture(frame):
    """Called at most once per active/motion frame. Internally rate
    limited on wall-clock time *before* running any inference, so the
    extra low-confidence model call only actually happens roughly once
    per SAVE_MIN_INTERVAL_SECONDS -- not on every frame of a motion burst.
    Runs its own inference pass at SAVE_CONF_FLOOR rather than reusing the
    display pass's results, so the lower floor never affects what's drawn
    on the live stream or counted in edge_detections_total."""
    global _last_save_time
    now = time.time()
    if now - _last_save_time < SAVE_MIN_INTERVAL_SECONDS:
        return

    predict_kwargs = {
        "verbose": False,
        "conf": SAVE_CONF_FLOOR,
        "imgsz": IMG_SIZE,
    }
    if INFERENCE_DEVICE:
        predict_kwargs["device"] = INFERENCE_DEVICE
    results = model(frame, **predict_kwargs)
    boxes = results[0].boxes
    if len(boxes) == 0:
        return

    _last_save_time = now
    write_capture_files(frame, boxes, model.names)


def retention_sweep():
    """Delete oldest date-partitions first until both the day-count and
    byte-size caps are satisfied. Whole-day granularity, not per-file --
    keeps this cheap enough to run on a schedule without walking every
    file on every sweep just to decide what to delete."""
    try:
        base = Path(CAPTURE_DIR)
        if not base.is_dir():
            CAPTURE_DIR_BYTES.set(0)
            return

        date_dirs = sorted(d for d in base.iterdir() if d.is_dir())

        if RETENTION_DAYS > 0 and len(date_dirs) > RETENTION_DAYS:
            for d in date_dirs[:-RETENTION_DAYS]:
                shutil.rmtree(d, ignore_errors=True)
            date_dirs = date_dirs[-RETENTION_DAYS:]

        if RETENTION_MAX_BYTES > 0:
            def dir_size(d):
                return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())

            sizes = {d: dir_size(d) for d in date_dirs}
            total = sum(sizes.values())
            for d in date_dirs:  # oldest first, already sorted by name
                if total <= RETENTION_MAX_BYTES:
                    break
                shutil.rmtree(d, ignore_errors=True)
                total -= sizes[d]

        CAPTURE_DIR_BYTES.set(
            sum(f.stat().st_size for f in base.rglob("*") if f.is_file())
        )
    except Exception as exc:
        print(f"retention sweep failed: {exc}")


def retention_loop():
    """Runs in its own low-frequency thread so a slow directory walk on a
    large capture tree never blocks the capture loop."""
    while True:
        retention_sweep()
        time.sleep(RETENTION_INTERVAL_SECONDS)


def capture_loop():
    """Owns the camera exclusively. Starts once at process startup and
    runs continuously and independently of any HTTP request -- this is
    the fix for the original bug, where inference (and even opening the
    camera) only happened while /video_feed was being actively read.

    Safe to start exactly once, unconditionally, because this app runs on
    Flask's single-process dev server (see Dockerfile.jetson CMD). Under
    a multi-worker WSGI server (gunicorn etc.) this would need to move
    behind a "only start in one worker" guard, or every worker would open
    its own competing handle on /dev/video0.
    """
    cap = None
    camera_available = False
    motion = MotionDetector(MOTION_THRESHOLD, MOTION_TRAILING_SECONDS)

    def open_camera():
        nonlocal cap, camera_available
        c = cv2.VideoCapture(CAMERA_INDEX)
        if c.isOpened():
            cap = c
            camera_available = True
            CAMERA_AVAILABLE.set(1)
            motion.reset()
            print(f"Camera opened (index {CAMERA_INDEX})")
        else:
            c.release()
            cap = None
            camera_available = False
            CAMERA_AVAILABLE.set(0)

    open_camera()
    if not camera_available:
        print(f"No camera at index {CAMERA_INDEX} at startup; will keep retrying.")

    last_retry = 0.0

    while True:
        try:
            LOOP_HEARTBEAT.set(time.time())

            frame = None
            if camera_available:
                ret, frame = cap.read()
                if not ret:
                    print("Camera read failed; releasing, will retry.")
                    cap.release()
                    cap = None
                    camera_available = False
                    CAMERA_AVAILABLE.set(0)
                    CAMERA_FAILURES.inc()
                    frame = None

            if frame is None:
                if not camera_available and (time.time() - last_retry) > CAMERA_RETRY_SECONDS:
                    last_retry = time.time()
                    open_camera()
                frame = synthetic_frame()

            active = motion.update(frame) if MOTION_ENABLED else True

            if active:
                out_frame = infer_and_annotate(frame)
                if SAVE_ENABLED and camera_available:
                    maybe_save_capture(frame)
            else:
                # Publish the raw frame, not a frozen last-annotated one --
                # a frozen frame is indistinguishable from a dead camera.
                out_frame = frame.copy() if SHOW_MOTION_OVERLAY else frame

            if SHOW_MOTION_OVERLAY:
                label = "MOTION" if active else "IDLE"
                color = (0, 200, 0) if active else (160, 160, 160)
                cv2.putText(out_frame, label, (10, 25),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

            ok, buffer = cv2.imencode('.jpg', out_frame)
            if ok:
                frame_buffer.publish(buffer.tobytes())

            time.sleep(FRAME_SLEEP)
        except Exception as exc:
            # Must never let the loop die -- this is the sole camera owner
            # and sole source of frames/metrics for the whole app.
            print(f"capture loop error (continuing): {exc}")
            time.sleep(1.0)


def generate_frames():
    """Reads the shared frame buffer; does not touch the camera or run
    inference. Each call (one per HTTP request) tracks its own last-seen
    sequence number, so multiple concurrent viewers don't steal frames
    from each other."""
    last_seq = -1
    while True:
        jpeg, last_seq = frame_buffer.get_latest(last_seq)
        if jpeg is not None:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpeg + b'\r\n')
        else:
            time.sleep(0.1)


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
    """Annotated MJPEG stream. Reads the most recent frame from the
    background capture loop's shared buffer -- does not open the camera
    or run inference itself."""
    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


if __name__ == '__main__':
    threading.Thread(target=capture_loop, name="capture-loop", daemon=True).start()
    threading.Thread(target=retention_loop, name="retention-loop", daemon=True).start()

    # Binding to all interfaces is intentional: this runs inside a container
    # on a private network and must be reachable by the Docker host.
    app.run(host='0.0.0.0', port=5000, threaded=True)  # nosec B104
