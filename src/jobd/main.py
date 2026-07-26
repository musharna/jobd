"""Entry point: python -m jobd or `jobd` after install."""

import argparse
import logging
import os
from pathlib import Path

import uvicorn

from jobd import __version__
from jobd.app import build_app
from jobd.auth import assert_auth_configured

_SQLITE_PREFIX = "sqlite:///"


def default_db_url() -> str:
    """Where the broker keeps its database when JOBD_DB_URL is unset.

    The Dockerfile sets JOBD_DB_URL explicitly, so this default is reached only
    by a bare `pip install jobd` — which is precisely what the README quickstart
    tells a new user to do. It used to be the container's own `/app/data`, a
    path that does not exist on a normal machine and that SQLite will not
    create, so `jobd` with no configuration crashed on boot with an opaque
    "unable to open database file". Honour XDG_DATA_HOME so the location is
    predictable and per-user.
    """
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return f"{_SQLITE_PREFIX}{base / 'jobd' / 'jobd.db'}"


def ensure_sqlite_parent(db_url: str) -> None:
    """Create the directory a SQLite file lives in, or fail with a usable error.

    SQLite does not create missing parent directories — it reports "unable to
    open database file" and leaves you to guess which of the path, the
    permissions, or the driver is at fault. Non-SQLite URLs (Postgres et al.)
    have no local directory and are left alone.
    """
    if not db_url.startswith(_SQLITE_PREFIX):
        return
    raw = db_url[len(_SQLITE_PREFIX) :].split("?", 1)[0]
    if not raw or raw == ":memory:":
        return

    parent = Path(raw).parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(
            f"jobd: cannot create the database directory {parent} ({exc.strerror}).\n"
            f"  Set JOBD_DB_URL to a writable location, e.g.\n"
            f"    JOBD_DB_URL=sqlite:///$HOME/.local/share/jobd/jobd.db jobd"
        ) from exc


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="jobd",
        description=(
            "jobd broker daemon. Takes no positional arguments; "
            "configuration is via environment variables."
        ),
        epilog=(
            "Environment:\n"
            "  JOBD_CONFIG_DIR  directory containing projects.yaml / profiles.yaml / "
            "classifier.yaml (default: /app/config)\n"
            "  JOBD_DB_URL      SQLAlchemy URL for the jobd database (default:\n"
            "                   sqlite:///$XDG_DATA_HOME/jobd/jobd.db, falling back to\n"
            "                   ~/.local/share/jobd/jobd.db. The directory is created on\n"
            "                   boot. The Docker image overrides this to /app/data.)\n"
            "  JOBD_HOST        host/interface uvicorn binds to (default: 127.0.0.1; "
            "set to a tailscale IP in production)\n"
            "  JOBD_PORT        port to bind (default: 8765)\n"
            "  JOBD_LOGS_DIR    per-job stdout/stderr log directory (default: ./logs)\n"
            "  JOBD_STATE_DIR   mutable broker state, incl. the runtime project-priority\n"
            "                   overlay written by `job projects set/nudge` (default: the\n"
            "                   SQLite DB's directory). MUST be writable; the config dir is\n"
            "                   git-owned and mounted read-only, so it is never used for this.\n"
            "  JOBD_DB_POOL_SIZE / JOBD_DB_MAX_OVERFLOW\n"
            "                   SQLAlchemy pool sizing (defaults: 20 / 60)\n"
            "\n"
            "For the CLI client see `job --help` (separate entry point)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"jobd {__version__}")
    return parser.parse_args(argv)


def run() -> None:
    _parse_args()
    logging.basicConfig(level=logging.INFO)
    assert_auth_configured()
    config_dir = os.environ.get("JOBD_CONFIG_DIR", "/app/config")
    db_url = os.environ.get("JOBD_DB_URL", "").strip() or default_db_url()
    ensure_sqlite_parent(db_url)
    app = build_app(
        db_url=db_url,
        projects_path=f"{config_dir}/projects.yaml",
        profiles_path=f"{config_dir}/profiles.yaml",
        classifier_path=f"{config_dir}/classifier.yaml",
    )
    host = os.environ.get("JOBD_HOST", "127.0.0.1")
    # proxy_headers=False, explicitly (audit 2026-07-25 S-3). uvicorn defaults
    # it to True, which lets a request from a trusted peer REWRITE
    # request.client.host out of X-Forwarded-For — and that is the exact value
    # jobd's tailnet source-IP ACL makes its decision on (auth.py
    # TailnetACLMiddleware). Today this is inert: uvicorn only trusts the
    # header from a 127.0.0.1 peer, and the shipped deployment binds directly
    # to a tailscale IP under network_mode: host, so a remote peer is never
    # loopback. It stops being inert the moment anyone puts a TLS-terminating
    # reverse proxy on localhost in front of the broker — a normal thing to do
    # — at which point the ACL silently degrades from "who connected" to
    # "what header did they send". Turning it off keeps the ACL's input the
    # real socket peer, always.
    uvicorn.run(
        app,
        host=host,
        port=int(os.environ.get("JOBD_PORT", "8765")),
        proxy_headers=False,
    )


if __name__ == "__main__":
    run()
