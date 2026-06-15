# Use a lightweight, official Python runtime as a parent image
FROM python:3.10-slim

# Install system dependencies required for OpenCV and USB cameras
RUN apt-get update && apt-get install -y \
    libgl1-mesa-glx \
    libglib2.0-0 \
    v4l-utils \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory inside the container
WORKDIR /usr/src/app

# Pre-install core AI packages so they are baked into the image
RUN pip install --no-cache-dir ultralytics opencv-python

# Copy the local code into the container
COPY . .

# Run the application
CMD ["python", "app.py"]