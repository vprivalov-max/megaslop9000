# Series Writer — production image.
# Built from python:3.12-slim with ffmpeg installed.

FROM python:3.12-slim

# ffmpeg is required for the montage render endpoint.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first so docker layer cache survives source-only changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App source.
COPY app.py gunicorn.conf.py ./
COPY static ./static
COPY templates ./templates

# Runtime data goes here. Mount a host volume in docker-compose.
ENV DATA_ROOT=/data
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

# Health check — Caddy/orchestrator can poll this.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8080/healthz || exit 1

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
