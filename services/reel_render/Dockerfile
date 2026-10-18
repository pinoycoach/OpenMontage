# OpenMontage reel render service (for ViralReel Studio's "HD Reel" export).
# Build from the repo root:
#   docker build -f services/reel_render/Dockerfile -t reel-render .
FROM node:22-bookworm-slim

# ffmpeg/ffprobe, Python, and the shared libraries Remotion's headless Chrome needs.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg python3 python3-venv ca-certificates fonts-liberation \
      libnss3 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
      libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
      libasound2 libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY remotion-composer/package.json remotion-composer/package-lock.json remotion-composer/
RUN cd remotion-composer && npm ci --no-audit --no-fund && npx remotion browser ensure

COPY services/reel_render/requirements.txt services/reel_render/requirements.txt
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir -r services/reel_render/requirements.txt
# Download the speech model at build time so the first render doesn't wait for it.
ENV HF_HOME=/models WHISPER_MODEL=base
RUN /venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8'); WhisperModel('small', device='cpu', compute_type='int8')"

COPY remotion-composer remotion-composer
COPY services services

ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "/venv/bin/uvicorn services.reel_render.app:app --host 0.0.0.0 --port ${PORT}"]
