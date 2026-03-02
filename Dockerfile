FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Create app user (don't run as root)
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        build-essential \
        libpq-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p logs recordings && \
    chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Expose port (Render sets PORT dynamically)
EXPOSE 8000

# Create database tables on startup, then run the app
# Render injects PORT env var; shell form CMD expands $PORT
CMD sh -c "python -c 'import asyncio; from app.database import init_db; asyncio.run(init_db())' && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --ws-max-size 16777216 --timeout-keep-alive 120"
