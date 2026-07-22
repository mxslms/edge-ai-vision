# Ultralytics base image provides PyTorch, CUDA, and YOLO dependencies
FROM ultralytics/ultralytics:latest

WORKDIR /usr/src/app

# Install app-level dependencies first so this layer caches across code changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "app.py"]
