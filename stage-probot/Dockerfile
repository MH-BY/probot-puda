FROM python:3.12-slim-bookworm

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1

RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Resolve the workspace from the repo root (build context is the workspace root).
# Commit a uv.lock (run `uv lock` at the workspace root) and add `--frozen` below
# for reproducible builds; without it uv resolves at build time.
COPY pyproject.toml ./
COPY stage-probot/pyproject.toml stage-probot/pyproject.toml
COPY probot_drivers/pyproject.toml probot_drivers/README.md probot_drivers/

RUN uv sync --no-dev --no-install-project --package stage-probot-edge

COPY probot_drivers/ probot_drivers/
COPY stage-probot/ stage-probot/

WORKDIR /app/stage-probot

CMD ["uv", "run", "python", "main.py"]
