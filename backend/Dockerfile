# syntax=docker/dockerfile:1.7
# LexAI API image. Build from the repository root:
#   docker build -f backend/Dockerfile -t lexai-backend backend
# Models are never baked in; the corpus (data/) and evaluation files (eval/) are mounted.

ARG PYTHON_IMAGE=python:3.12.16-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32

FROM ${UV_IMAGE} AS uv

# ---------------------------------------------------------------- builder: resolve and install
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/backend/.venv
WORKDIR /app/backend
# Dependencies first (cached unless the lock file changes), then the project itself.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# ---------------------------------------------------------------- runtime: no build tools, non-root
FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.title="lexai-backend" \
      org.opencontainers.image.description="LexAI API: grounded, cited answers about Indian law" \
      org.opencontainers.image.source="https://github.com/allu0786ansari/LexAI"
RUN groupadd --system --gid 10001 lexai \
 && useradd --system --uid 10001 --gid lexai --home-dir /app --no-create-home lexai
WORKDIR /app/backend
COPY --from=builder --chown=lexai:lexai /app/backend /app/backend
COPY --chown=lexai:lexai scripts ./scripts
RUN mkdir -p /app/backend/var /app/data /app/eval && chown lexai:lexai /app/backend/var /app/data /app/eval
ENV PATH=/app/backend/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1
USER lexai
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
# One worker per container: metrics and the background probe are per process; scale with replicas.
# Only nginx reaches the API, so it may trust X-Forwarded-For (client IPs for rate limits).
CMD ["uvicorn", "lexai.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
