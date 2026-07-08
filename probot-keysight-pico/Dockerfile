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
COPY probot-keysight-pico/pyproject.toml probot-keysight-pico/pyproject.toml

RUN uv sync --no-dev --no-install-project --package probot-keysight-pico-edge

COPY probot-keysight-pico/ probot-keysight-pico/

WORKDIR /app/probot-keysight-pico

CMD ["uv", "run", "python", "main.py"]
