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

CMD ["python", "main.py"]
