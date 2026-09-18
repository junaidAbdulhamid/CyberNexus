# One image, several roles: the service is chosen by the command, so capture,
# ingest, scorer and api all share a layer cache and can never drift apart in
# their view of the record schema.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    CN_REDIS_URL=redis://redis:6379/0 \
    CN_MODEL_DIR=/app/artifacts/model

WORKDIR /app

# libpcap is only needed for live capture; tini gives the workers a real PID 1
# so SIGTERM reaches them and they drain instead of being killed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpcap0.8 tini curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cybernexus/ ./cybernexus/
COPY capture/ ./capture/
COPY ingest/ ./ingest/
COPY model/ ./model/
COPY api/ ./api/
COPY bench/ ./bench/
COPY tests/ ./tests/
COPY pytest.ini Makefile ./

# The model directory must exist *in the image* and be owned by the runtime
# user: Docker seeds a fresh named volume from the image path, ownership
# included, and a volume created at a path that does not exist ends up root-owned.
RUN useradd --create-home --uid 10001 cybernexus \
    && mkdir -p /app/artifacts/model \
    && chown -R cybernexus:cybernexus /app
USER cybernexus

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "api.app"]
