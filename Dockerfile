# Use the official, pre-built Ultralytics YOLO environment
FROM ultralytics/ultralytics:latest

# Set the working directory
WORKDIR /usr/src/app

# Copy your app.py into the container
COPY . .

# Run the application
CMD ["python", "app.py"]