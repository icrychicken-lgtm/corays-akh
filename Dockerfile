# ── Nova Community Bot ────────────────────────────────────────
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# FFmpeg (Musik), libopus/libsodium (Voice), postgresql-client (pg_dump für Backups)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libopus0 libsodium23 postgresql-client curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN useradd --create-home --uid 1000 nova \
 && mkdir -p /app/logs /app/backups /app/data \
 && chown -R nova:nova /app
USER nova

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://localhost:8080/healthz || exit 1

CMD ["python", "-m", "app"]
