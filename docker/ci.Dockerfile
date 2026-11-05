# A disposable Linux development environment, separate from the Distroless runtime.
FROM node:22-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS node
FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 /uv /usr/local/bin/uv
ENV UV_PYTHON=/usr/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_CACHE_DIR=/tmp/uv-cache \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    NEXT_TELEMETRY_DISABLED=1 \
    CI=true
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates make git libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install --global pnpm@12.11.2 \
    && npm cache clean --force
WORKDIR /workspace
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-cache
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./frontend/
RUN pnpm --dir frontend install --frozen-lockfile --store-dir /tmp/pnpm-store \
    && rm -rf /tmp/pnpm-store \
    && pnpm --dir frontend exec playwright install --with-deps chromium firefox webkit \
    && rm -rf /var/lib/apt/lists/*
COPY . .
CMD ["make", "check-unit", "test-frontend"]
