FROM python:3.12-slim-bookworm

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1
# Headless matplotlib backend (measurement routines import pyplot).
ENV MPLBACKEND=Agg

RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Resolve the workspace from the repo root (build context is the workspace root).
# Commit a uv.lock (run `uv lock` at the workspace root) and add `--frozen` below
# for reproducible builds; without it uv resolves at build time.
COPY pyproject.toml ./
COPY probot-smu-keysight/pyproject.toml probot-smu-keysight/pyproject.toml
COPY probot_drivers/pyproject.toml probot_drivers/README.md probot_drivers/

RUN uv sync --no-dev --no-install-project --package probot-smu-keysight-edge

COPY probot_drivers/ probot_drivers/
COPY probot-smu-keysight/ probot-smu-keysight/

WORKDIR /app/probot-smu-keysight

CMD ["uv", "run", "python", "main.py"]
