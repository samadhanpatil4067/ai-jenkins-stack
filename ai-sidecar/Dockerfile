# ── AI Sidecar — Dockerfile ──────────────────────────────────
# Multi-stage build: keeps final image lean (~180MB)
# python:3.11-slim base — no unnecessary OS packages

FROM python:3.11-slim AS builder

WORKDIR /build

# Install build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps into /build/deps — we'll copy this layer only
COPY requirements.txt .
RUN pip install --prefix=/build/deps --no-cache-dir -r requirements.txt


# ── Final image ───────────────────────────────────────────────
FROM python:3.11-slim

LABEL org.opencontainers.image.title="ARIA AI Sidecar"
LABEL org.opencontainers.image.description="AI-powered intelligence layer for Jenkins pipelines"
LABEL org.opencontainers.image.version="2.0.0"

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /build/deps /usr/local

# Copy application code
COPY main.py .

# Non-root user — security best practice
RUN groupadd -r aria && useradd -r -g aria aria
USER aria

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--log-level", "info"]
