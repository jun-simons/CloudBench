# Gateway image: builds the web app, then runs the gateway serving API + web.
# The workbench agent runs on the lab computer, not in this image.

FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim AS gateway
COPY --from=ghcr.io/astral-sh/uv:0.9.25 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app

# Dependencies first, for layer caching.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra gateway --no-install-project

COPY src/ ./src/
RUN uv sync --frozen --no-dev --extra gateway

COPY experiments/ ./experiments/
COPY --from=web /web/dist ./web

RUN useradd --system --uid 10001 cloudbench && mkdir -p /data && chown cloudbench /data
USER cloudbench

ENV PATH=/opt/venv/bin:$PATH \
    CLOUDBENCH_DATA_DIR=/data \
    CLOUDBENCH_EXPERIMENTS_DIR=/app/experiments \
    CLOUDBENCH_WEB_DIR=/app/web \
    CLOUDBENCH_PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["cloudbench-gateway"]
