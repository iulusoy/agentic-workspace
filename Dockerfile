# ==== Base image ====
FROM ghcr.io/astral-sh/uv:python3.13-bookworm
# ====

# ==== Application working directory ====
WORKDIR /app
# ====

# ==== Runtime and uv configuration ====
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
# ====

# ==== Application user and agent workspace ====
# /workspace is the only writable location at runtime (FILE_TOOLS_ROOT);
# it is expected to be shadowed by a per-session volume mount.
USER root

RUN groupadd --system agent \
    && useradd --system --gid agent --home-dir /app agent \
    && mkdir /workspace \
    && chown agent:agent /app /workspace
# ====

# ==== Python dependency installation ====
USER agent

COPY --chown=agent:agent pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
# ====

# ==== Application source ====
COPY --chown=agent:agent src/backend ./src/backend
RUN uv sync --frozen --no-dev
# ====

# ==== Agent configuration ====
# Secrets are expected via *_FILE (e.g. ANTHROPIC_API_KEY_FILE pointing at a
# mounted secret), never baked into the image or passed as build args.
ENV FILE_TOOLS_ROOT=/workspace
# ====

# ==== Application startup ====
# The venv python is invoked directly (not `uv run`): uv needs a writable
# cache, which the read-only root filesystem forbids at runtime.
CMD ["/app/.venv/bin/python", "-m", "backend.client_loop"]
# ====
