# Dockerfile — place at repo root
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

# Install system deps (git for pip git+ installs, ffmpeg, opus, build tools for wheels)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      git \
      build-essential \
      gcc \
      libffi-dev \
      libsodium-dev \
      ffmpeg \
      libopus0 \
      libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# Upgrade pip and install Python deps from requirements
RUN python -m pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Run the bot
CMD ["python", "bot.py"]
