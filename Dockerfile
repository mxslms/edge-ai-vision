# Ultralytics base image provides PyTorch, CUDA, and YOLO dependencies.
# This image targets desktop / discrete GPUs (linux/amd64), e.g. RTX 3070.
# For Jetson Orin Nano (JetPack 6 / arm64), use Dockerfile.jetson instead.
FROM ultralytics/ultralytics:8.4.117

WORKDIR /usr/src/app

# Install app-level dependencies first so this layer caches across code changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Shared tunables; compose / Portainer can override without rebuilding.
ENV MODEL_PATH=yolov8n.pt \
    IMG_SIZE=640 \
    CONF_THRESHOLD=0.25 \
    INFERENCE_DEVICE= \
    FRAME_SLEEP=0.06 \
    CAMERA_INDEX=0

CMD ["python", "app.py"]
