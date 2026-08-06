FROM python:3.11-slim

# PYTHONUNBUFFERED ensures logs appear immediately in container output
# (without it, stdout is block-buffered and logs are delayed).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# postgresql-client is needed for pg_dump / psql (backup/restore).
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user for defense-in-depth.
RUN useradd -m -u 1000 botuser && chown -R botuser:botuser /app
USER botuser

# Render/Railway/etc. provide PORT via env. The HTTP keep-alive server reads it.
ENV PORT=10000

# Phase 4.2: HEALTHCHECK probes the real /healthz endpoint (Phase 0.3), which
# returns 200 ONLY when the Discord gateway is connected AND the DB pool is
# live. This is honest — a container that loses its DB connection or Discord
# gateway will be marked unhealthy and restarted by the orchestrator, instead
# of silently running in a degraded state.
#
# Uses python+urllib (already in the image) rather than curl/wget (which would
# require an extra apt package). Runs as botuser (no privilege needed).
#
# Timing rationale:
#   --start-period=15s  : grace for the gateway handshake + initial cog load
#                         (setup_hook syncs commands, which can take a few seconds)
#   --interval=30s      : poll every 30s (frequent enough to catch a wedged
#                         gateway, not so frequent it spams the health server)
#   --timeout=5s        : /healthz does a SELECT 1 + bot.is_ready() check —
#                         should return in well under 1s; 5s is a generous ceiling
#   --retries=3         : 3 consecutive failures = unhealthy (≈90s of downtime)
HEALTHCHECK --start-period=15s --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
    urllib.request.urlopen('http://localhost:' + os.environ.get('PORT','10000') + '/healthz', timeout=4).read()" \
    || exit 1

CMD ["python", "main.py"]
