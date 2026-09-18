# Two-stage build: compile the frontend with Node, serve it from the Python
# backend. One image runs every role (backend, discovery, simulator), chosen by
# the command, so they cannot drift apart in their view of the data model.

FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    CN_MAP_STATIC=/app/frontend/dist

WORKDIR /app

# libpcap for the optional ARP sweep; tini so SIGTERM reaches the workers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpcap0.8 tini curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cnmap/ ./cnmap/
COPY discovery/ ./discovery/
COPY integration/ ./integration/
COPY backend/ ./backend/
COPY tests/ ./tests/
COPY pytest.ini ./
COPY --from=frontend /build/dist ./frontend/dist

RUN useradd --create-home --uid 10001 cnmap \
    && mkdir -p /app/data \
    && chown -R cnmap:cnmap /app
USER cnmap

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "backend.app"]
