#!/usr/bin/env python3
"""Container healthcheck: prove *the broker* is serving, not merely that something is.

This replaces a bare TCP connect to a hardcoded loopback address, which had two
independent defects that between them made it useless in production:

  1. **Wrong address.** The broker binds ``$JOBD_HOST`` -- a tailscale IP in the
     homelab deployment -- with ``network_mode: host``. It never listens on loopback,
     so a probe hardcoded to 127.0.0.1 could not reach it even in principle.

  2. **Wrong daemon.** A bare TCP connect only proves that *something* accepted the
     socket; it cannot tell which process did. On gt76 an unrelated container
     published ``127.0.0.1:8765``, so this healthcheck connected to *that* and
     reported green for as long as the two coexisted -- passing for a false reason
     while never once touching jobd. When that container moved off the port, the
     probe started failing and revealed it had never worked.

So: talk to the address uvicorn actually binds, over HTTP, and require jobd's own
/health payload back. A wrong daemon on that port fails the body check rather than
satisfying it. The slim image has no curl, but urllib is in the stdlib.

Exits 0 = healthy. Any non-zero exit (with a reason on stderr) = unhealthy.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    host = os.environ.get("JOBD_HOST", "127.0.0.1")
    port = os.environ.get("JOBD_PORT", "8765")
    token = os.environ.get("JOBD_API_TOKEN", "").strip()

    req = urllib.request.Request(f"http://{host}:{port}/health")
    # /health sits behind the bearer-token dependency. The broker container already
    # holds the token it validates against, so the probe can make a genuinely
    # authenticated request rather than settling for "something answered".
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status != 200:
                print(f"/health returned HTTP {resp.status}", file=sys.stderr)
                return 1
            body = json.load(resp)
    except urllib.error.HTTPError as exc:
        # Reachable, but not serving jobd's /health: a 401 means the token is wrong,
        # and a 404 means some *other* service owns this port.
        print(
            f"http://{host}:{port}/health returned HTTP {exc.code} — "
            "reachable, but this is not a healthy jobd broker",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # connection refused, DNS, timeout, bad JSON
        print(f"cannot reach the broker at {host}:{port}: {exc}", file=sys.stderr)
        return 1

    if body.get("status") != "ok":
        print(f"/health payload is not jobd's: {body!r}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
