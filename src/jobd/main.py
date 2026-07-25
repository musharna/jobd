"""Entry point: python -m jobd or `jobd` after install."""

import argparse
import logging
import os

import uvicorn

from jobd import __version__
from jobd.app import build_app
from jobd.auth import assert_auth_configured


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
            "  JOBD_DB_URL      SQLAlchemy URL for the jobd database "
            "(default: sqlite:////app/data/jobd.db)\n"
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
    db_url = os.environ.get("JOBD_DB_URL", "sqlite:////app/data/jobd.db")
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
