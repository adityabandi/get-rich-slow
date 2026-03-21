# Stage 1: Build Next.js dashboard as static export
FROM node:22-slim AS dashboard-build

RUN corepack enable && corepack prepare pnpm@10.8.1 --activate

WORKDIR /repo

# Copy everything pnpm needs to install (workspace root + both packages)
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY dashboard/package.json ./dashboard/
COPY cli/package.json ./cli/

RUN pnpm install --frozen-lockfile

# Copy source and build only the dashboard
COPY dashboard/ ./dashboard/
RUN pnpm --filter dashboard build

# Stage 2: Python API + scanner + static dashboard
FROM python:3.13.3-slim-bookworm@sha256:8bc60ca09afaa8ea0d6d1220bde073bacfedd66a4bf8129cbdc8ef0e16c8a952

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install Python dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy app code
COPY kalshi_client.py scanner.py api.py db.py espn.py ./

# Copy built dashboard static files from stage 1
COPY --from=dashboard-build /repo/dashboard/out ./dashboard_static

# Create data directory for SQLite (Railway volume mounts here)
RUN mkdir -p /data

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
