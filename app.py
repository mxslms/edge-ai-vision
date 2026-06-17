import cv2
import time
from flask import Flask, Response
from ultralytics import YOLO

app = Flask(__name__)

print("Initializing Vision System...")
model = YOLO('yolov8n.pt')

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

        # Run YOLO inference on the frame
        results = model(frame, verbose=False)

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

@app.route('/video_feed')
def video_feed():
    # Return the dynamic MJPEG stream
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    # Run the web server on port 5000, accessible to your local network
    app.run(host='0.0.0.0', port=5000, threaded=True)