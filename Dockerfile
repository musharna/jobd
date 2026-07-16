# Digest-pinned (audit 2026-07-15 Sec-B): a tag is mutable and can be re-pushed
# under the same name; the digest is the image. This is python:3.11-slim-bookworm
# as of 2026-07-15 — bump deliberately, with the tag comment kept in sync.
FROM python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    JOBD_CONFIG_DIR=/app/config \
    JOBD_DB_URL=sqlite:////app/data/jobd.db \
    JOBD_LOGS_DIR=/app/logs \
    JOBD_PORT=8765

# Phase 1 — third-party dependencies, EXACTLY the versions CI tested
# (audit 2026-07-15 Sec-B). `pip install .` used to re-resolve everything fresh
# from PyPI at build time, so the shipped image carried dependency versions no
# test had ever seen — and was open to a dependency hijack at every release
# build. requirements-docker.txt is exported from uv.lock (hashes included) and
# kept in lockstep by a deploy-lint check; --require-hashes makes substitution
# fail closed. The layer keys on the requirements file alone, so editing
# application source still does not re-download dependencies.
COPY requirements-docker.txt ./
RUN pip install -U pip \
    && pip install --no-deps --require-hashes -r requirements-docker.txt

# Phase 2 — the application itself. `--no-deps` because phase 1 installed the
# complete transitive set; the project wheel carries no third-party code.
# README.md is required because pyproject.toml declares `readme = "README.md"`,
# which hatchling reads during metadata generation.
COPY pyproject.toml README.md ./
COPY src ./src
# The wheel force-includes these (job fleet add pushes them at runtime) —
# hatchling's metadata generation FileNotFoundErrors without them, which is
# exactly how the v0.5.29 GHCR publish died: the PR suite builds the wheel
# from a full checkout and cannot see a missing COPY. A deploy-lint now keeps
# every force-include source COPY'd here before this install.
COPY scripts/install-worker.sh scripts/update-worker.sh ./scripts/
RUN pip install --no-deps .

COPY scripts/healthcheck.py /app/healthcheck.py

# Run as an unprivileged user; the broker never needs root. Data/logs must be
# writable by that user (the SQLite DB lives under /app/data).
RUN useradd --create-home --uid 10001 jobd \
    && mkdir -p /app/data /app/logs \
    && chown -R jobd:jobd /app
USER jobd

EXPOSE 8765

# The probe must reach the address uvicorn actually binds ($JOBD_HOST — a tailscale
# IP under network_mode: host, NOT loopback) and must require jobd's own /health
# payload back. The previous version did neither: it TCP-connected to a hardcoded
# 127.0.0.1, which the broker never listens on, and a bare connect cannot tell WHICH
# daemon accepted it. On gt76 an unrelated container held 127.0.0.1:8765 and this
# healthcheck passed against that for weeks — green for a false reason. See
# scripts/healthcheck.py.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]

CMD ["jobd"]
