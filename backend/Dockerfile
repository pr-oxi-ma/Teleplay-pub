# Backend Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY run_migrations.py ./run_migrations.py

# Create an unprivileged runtime user and writable runtime dirs.
RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /app/session /app/data/thumbnails \
    && chown -R app:app /app/session /app/data

# Runtime host/port fallbacks; override with SERVER_HOST, SERVER_PORT, or platform PORT.
ARG APP_PORT=8000
ENV SERVER_HOST=0.0.0.0
ENV SERVER_PORT=${APP_PORT}

EXPOSE ${APP_PORT}

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; port=os.environ.get('PORT') or os.environ.get('SERVER_PORT','8000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=5)"

USER app

# Run the application
CMD ["sh", "-c", "uvicorn app.main:app --host ${SERVER_HOST:-0.0.0.0} --port ${PORT:-${SERVER_PORT:-8000}}"]
