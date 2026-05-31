"""Lint: docker-compose.yml must use host networking with JOBD_HOST bound to
a safe interface (loopback or Tailscale CGNAT), never 0.0.0.0 or a public IP.

Docker's default userland-proxy NATs cross-host inbound source IPs to the
bridge gateway, which defeats the source-IP ACL middleware. The broker
therefore runs with `network_mode: host` and binds uvicorn directly to
JOBD_HOST. The only thing keeping the broker off the public internet is the
JOBD_HOST value. One typo (e.g. `0.0.0.0`, or a non-tailscale interface IP)
exposes the entire fleet to remote-code-execution. This test catches that
typo at CI.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMPOSE = _REPO_ROOT / "docker-compose.yml"

# docker-compose env interpolation with a default: ${JOBD_HOST:-127.0.0.1}
_INTERP_WITH_DEFAULT = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*:-(?P<default>.*)\}$")


def _host_is_safe(host: str) -> bool:
    if not host:
        return False
    # Allow ${VAR:-default} interpolation, but the default — what binds when
    # the operator sets nothing — must itself be safe. A value supplied at
    # runtime is the operator's responsibility (the broker does not re-validate
    # at startup, by design: tailnet-only deployment is an operator contract).
    m = _INTERP_WITH_DEFAULT.match(host)
    if m:
        host = m.group("default")
        if not host:
            return False
    if host == "0.0.0.0":
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if addr.is_loopback:
        return True
    return isinstance(addr, ipaddress.IPv4Address) and addr in ipaddress.ip_network("100.64.0.0/10")


def test_compose_file_exists():
    assert _COMPOSE.exists(), f"missing {_COMPOSE}"


def test_jobd_uses_host_networking():
    cfg = yaml.safe_load(_COMPOSE.read_text())
    svc = cfg["services"]["jobd"]
    assert svc.get("network_mode") == "host", (
        "jobd service must set `network_mode: host`. Bridge networking with "
        "docker's userland-proxy NATs cross-host inbound source IPs to the "
        "bridge gateway, which defeats the tailnet-IP ACL middleware."
    )
    assert "ports" not in svc, (
        "jobd service has a `ports:` stanza, but `network_mode: host` already "
        "exposes uvicorn directly on the host network namespace — port-publish "
        "is meaningless and confusing. Remove the `ports:` block."
    )


def test_jobd_host_binds_to_safe_interface():
    cfg = yaml.safe_load(_COMPOSE.read_text())
    env = cfg["services"]["jobd"].get("environment", {})
    # compose env can be list-of-strings or dict; normalize to dict
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    host = env.get("JOBD_HOST")
    assert host is not None, (
        "JOBD_HOST must be set in the jobd service environment. With "
        "`network_mode: host`, uvicorn defaults to 127.0.0.1; setting "
        "JOBD_HOST=100.x.y.z (tailscale IP) exposes the broker to the "
        "tailnet. Leaving it unset would silently restrict to loopback."
    )
    assert _host_is_safe(host), (
        f"JOBD_HOST={host!r} is unsafe. Use either 127.0.0.1 (loopback) or a "
        "100.x.y.z address inside 100.64.0.0/10 (Tailscale CGNAT). Public "
        "bind would expose the broker to the internet."
    )
