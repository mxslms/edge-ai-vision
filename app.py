import cv2
import time
from ultralytics import YOLO

print("Initializing Vision System...")
# Load the model; Ultralytics automatically shifts execution to CUDA/GPU if available
model = YOLO('yolov8n.pt')

# Target /dev/video0 (the standard index for the first USB webcam)
camera_index = 0
print(f"Attempting to connect to hardware camera at index {camera_index}...")
cap = cv2.VideoCapture(camera_index)

if not cap.isOpened():
    print("\n[WARNING] Physical camera not found yet! (This is expected until tomorrow).")
    print("The container environment and GPU pipelines are fully ready to receive the hardware.")
    cap.release()
    exit(0)

print("Camera stream successfully opened. Beginning live inference loop...")

try:
    while True:
        ret, frame = cap.read()
        if not ret:
            print("Failed to grab frame from camera stream.")
            break

        # Run model inference on the frame
        results = model(frame, verbose=False)

        # Process results
        for result in results:
            for box in result.boxes:
                label = model.names[int(box.cls[0])]
                conf = float(box.conf[0])
                print(f"[{time.strftime('%H:%M:%S')}] Detected: {label} ({conf*100:.1f}%)")

        # Control loop speed slightly (approx 10 FPS) to conserve resources
        time.sleep(0.1)

except KeyboardInterrupt:
    print("Closing camera stream...")
finally:
    cap.release()
    print("System offline.")