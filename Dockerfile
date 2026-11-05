FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2 AS python-builder
COPY --from=ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/bin/python3 \
    UV_LINK_MODE=copy
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-cache

COPY main.py ./
COPY scripts/__init__.py scripts/export_openapi.py ./scripts/
COPY frontend/openapi.json ./frontend/openapi.json
RUN .venv/bin/python -m scripts.export_openapi --check

FROM node:22-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS frontend-builder
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /frontend
RUN npm install --global pnpm@12.11.2 && npm cache clean --force
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile --store-dir /tmp/pnpm-store \
    && rm -rf /tmp/pnpm-store
COPY frontend/tsconfig.json frontend/next.config.ts frontend/postcss.config.mjs frontend/.htmlvalidate.json frontend/openapi-ts.config.ts frontend/published-assets.json ./
COPY --from=python-builder /app/frontend/openapi.json ./openapi.json
COPY frontend/src ./src
COPY frontend/public ./public
COPY frontend/scripts ./scripts
RUN pnpm build

# Debian 13's Python 3.13 matches the builder's interpreter and extension ABI.
FROM gcr.io/distroless/python3-debian13:nonroot@sha256:1c32c3720dc4c7f0c7dbe998737f32c200add6511eca7cec705008be488b87d6

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY --from=python-builder /app/.venv /app/.venv
COPY --chown=65532:65532 main.py ./
COPY --from=frontend-builder /frontend/out ./frontend/out
USER 65532:65532
EXPOSE 3000
ENTRYPOINT ["/app/.venv/bin/python"]
CMD ["main.py"]
