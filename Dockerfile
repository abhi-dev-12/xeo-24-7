FROM python:3.11-slim

# Install system dependencies: ffmpeg + opus
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg libopus0 && \
    rm -rf /var/lib/apt/lists/*

# Create app folder
WORKDIR /app

# Copy your code into the container
COPY . /app

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Default command: run your bot
CMD ["python", "bot.py"]
