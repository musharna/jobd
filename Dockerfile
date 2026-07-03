# Pinned to a specific Debian release for reproducibility. For stricter supply-
# chain guarantees, pin by digest instead: FROM python:3.11-slim-bookworm@sha256:<digest>
FROM python:3.11-slim-bookworm

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    JOBD_CONFIG_DIR=/app/config \
    JOBD_DB_URL=sqlite:////app/data/jobd.db \
    JOBD_LOGS_DIR=/app/logs \
    JOBD_PORT=8765

# README.md is required because pyproject.toml declares `readme = "README.md"`,
# which hatchling reads during metadata generation at `pip install .`.
COPY pyproject.toml README.md ./
# Phase 1 — install third-party dependencies in a layer keyed only on
# pyproject.toml + README, so editing application source does NOT re-resolve or
# re-download them (previously `COPY src` sat above the install, busting the dep
# layer on every code change). hatchling needs the wheel-target package dirs to
# exist to build metadata, so stub them; entry points resolve at runtime, not
# install time, so an empty stub installs fine. The stub is removed before the
# real source is copied.
RUN mkdir -p src/jobd src/job_cli \
    && touch src/jobd/__init__.py src/job_cli/__init__.py \
    && pip install -U pip \
    && pip install . \
    && rm -rf src

# Phase 2 — copy the real source and reinstall just the package (deps already
# satisfied above). `--no-deps` skips dependency work; `--force-reinstall`
# replaces the stub even though the version string is unchanged. Only this layer
# reruns when source changes.
COPY src ./src
RUN pip install --no-deps --force-reinstall .

# Run as an unprivileged user; the broker never needs root. Data/logs must be
# writable by that user (the SQLite DB lives under /app/data).
RUN useradd --create-home --uid 10001 jobd \
    && mkdir -p /app/data /app/logs \
    && chown -R jobd:jobd /app
USER jobd

EXPOSE 8765

# /health is behind the bearer-token auth dependency, and the slim image has no
# curl, so the healthcheck is a plain TCP connect: a listening socket means the
# broker is up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,socket; socket.create_connection(('127.0.0.1', int(os.environ.get('JOBD_PORT','8765'))), timeout=3).close()"

CMD ["jobd"]
