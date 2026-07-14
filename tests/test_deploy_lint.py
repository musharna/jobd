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
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMPOSE = _REPO_ROOT / "docker-compose.yml"
_SERVER_JSON = _REPO_ROOT / "server.json"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_DOCKERFILE = _REPO_ROOT / "Dockerfile"
_HEALTHCHECK = _REPO_ROOT / "scripts" / "healthcheck.py"

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


def test_server_json_version_matches_pyproject():
    """server.json carries its own version fields (the MCP registry schema
    requires them), but the registry rejects a re-publish of an already-listed
    version. A release that bumps pyproject but forgets server.json fails the
    `Publish to MCP Registry` workflow with `cannot publish duplicate version`
    — silently, after the tag is already cut (incident: v0.5.4, 2026-06-07).
    Pin every version field in server.json to the pyproject version so the
    drift fails CI before the tag instead of the registry after it."""
    import json
    import tomllib

    proj_version = tomllib.loads(_PYPROJECT.read_text())["project"]["version"]
    server = json.loads(_SERVER_JSON.read_text())

    versions = {("<root>", server.get("version"))}
    for i, pkg in enumerate(server.get("packages", [])):
        versions.add((f"packages[{i}]", pkg.get("version")))
    meta = server.get("_meta", {}).get("io.modelcontextprotocol.registry/publisher-provided", {})
    if "version" in meta:
        versions.add(("_meta.publisher-provided", meta["version"]))

    mismatched = {loc: v for loc, v in versions if v != proj_version}
    assert not mismatched, (
        f"server.json version field(s) {mismatched} != pyproject version "
        f"{proj_version!r}. Bump every version in server.json in lockstep with "
        "pyproject before tagging, or the MCP registry publish fails on a "
        "duplicate-version error after the release tag is already pushed."
    )


def test_mypy_checks_untyped_defs():
    """The mypy gate must keep `check_untyped_defs = true`.

    Without it, mypy skips the bodies of every un-annotated function, so most of
    the broker/worker hot path (app.py, job_worker.py — full of un-annotated
    endpoint closures and helpers) is never type-checked and "mypy green" means
    almost nothing there (audit Quality-4, 2026-07-01). Enabling it produced
    zero new errors; this test stops the flag from being silently dropped and
    the checking from quietly going vacuous again."""
    import tomllib

    mypy_cfg = tomllib.loads(_PYPROJECT.read_text()).get("tool", {}).get("mypy", {})
    assert mypy_cfg.get("check_untyped_defs") is True, (
        "[tool.mypy] check_untyped_defs must be true — without it mypy skips "
        "un-annotated function bodies and the type gate is vacuous over the "
        "broker/worker hot path (audit Quality-4)."
    )


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


def test_compose_pulls_a_registry_image_and_does_not_build():
    """Production compose must pull a registry-backed image, never build in place.

    `build: .` + `image: jobd:latest` with no registry behind the tag is what made
    `docker compose pull` a silent no-op and let a bare `up -d` after a `git pull`
    re-run the OLD image while reporting success. The mitigation was to remember
    `--build` forever — a guard against a footgun rather than removal of it. A
    registry-backed image deletes the class: `pull` now means something, the running
    version is pinned and knowable, and rollback is possible at all.

    Local builds still work, via docker-compose.build.yml — which is where `build:`
    belongs, because that tag is local and nothing else would refresh it.
    """
    cfg = yaml.safe_load(_COMPOSE.read_text())
    svc = cfg["services"]["jobd"]

    assert "build" not in svc, (
        "docker-compose.yml has a `build:` stanza again. That reintroduces the "
        "stale-image footgun: nothing backs a locally-built tag, so `pull` is a no-op "
        "and `up -d` silently reuses the old image. Put local builds in "
        "docker-compose.build.yml instead."
    )
    image = svc.get("image", "")
    # Compare the repository component exactly rather than prefix-matching the string.
    # A `startswith("ghcr.io/")` check is both weaker (it would accept any registry
    # whose name merely begins that way) and flagged by CodeQL as incomplete URL
    # sanitization. Split off the tag first: the tag is `${JOBD_TAG:-latest}`, which
    # itself contains a colon, so only the FIRST colon separates repo from tag.
    repository = image.split(":", 1)[0]
    assert repository == "ghcr.io/musharna/jobd", (
        f"jobd image {image!r} does not come from the published registry. Production "
        "must pull a published, version-pinned image so the deploy is verifiable and "
        "reversible."
    )
    assert "JOBD_TAG" in image, (
        f"jobd image {image!r} does not interpolate JOBD_TAG. Production must pin an "
        "exact version — a moving tag cannot be rolled back to and cannot tell you "
        "what is running."
    )


def test_build_overlay_exists_for_local_builds():
    """The `build:` we removed from production must still be available deliberately."""
    overlay = _REPO_ROOT / "docker-compose.build.yml"
    assert overlay.exists(), "docker-compose.build.yml is missing; local builds broke"
    cfg = yaml.safe_load(overlay.read_text())
    assert "build" in cfg["services"]["jobd"]


def _healthcheck_instruction() -> str:
    """Return the full HEALTHCHECK instruction, following backslash continuations.

    The probe body lives on the line AFTER `HEALTHCHECK --interval=...`, so grepping
    for lines containing the word HEALTHCHECK reads only the flags and none of the
    command — a guard written that way passes no matter what the probe does. (Ask me
    how I know: the first draft of this test did exactly that, and a mutation putting
    the old hardcoded-loopback probe back did not fail it.)
    """
    lines = _DOCKERFILE.read_text().splitlines()
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("HEALTHCHECK"):
            continue
        instruction = [line]
        while instruction[-1].rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            instruction.append(lines[i])
        return "\n".join(instruction)
    raise AssertionError("Dockerfile has no HEALTHCHECK instruction")


def test_healthcheck_instruction_parser_sees_the_command_body():
    """Guard the guard: the parser must capture the probe command, not just the flags.

    Without this, both healthcheck lint tests below can silently degrade into
    inspecting an empty string.
    """
    body = _healthcheck_instruction()
    assert "healthcheck.py" in body, (
        "the parsed HEALTHCHECK instruction does not contain the probe command — "
        "the continuation-line parser is broken, so the lint tests below are vacuous"
    )


def test_healthcheck_does_not_hardcode_loopback():
    """The container HEALTHCHECK must probe the address uvicorn actually binds.

    Regression guard. The original probe TCP-connected to a hardcoded 127.0.0.1,
    but the broker binds JOBD_HOST (a tailscale IP under `network_mode: host`) and
    never listens on loopback — so the probe could not reach it even in principle.
    It nonetheless reported healthy for weeks, because an unrelated container on
    gt76 happened to publish 127.0.0.1:8765 and a bare TCP connect cannot tell which
    daemon accepted the socket. Green for a false reason is worse than red.
    """
    body = _healthcheck_instruction()
    assert "127.0.0.1" not in body, (
        "The HEALTHCHECK hardcodes 127.0.0.1, but the broker binds $JOBD_HOST and "
        "does not listen on loopback in the deployed configuration. Probe "
        "$JOBD_HOST instead — see scripts/healthcheck.py."
    )


def test_healthcheck_probes_jobd_host_over_http():
    """The probe must be an HTTP request that validates jobd's own /health payload.

    A bare socket connect is structurally incapable of noticing it is talking to the
    WRONG service — which is exactly what happened. Requiring `status: ok` back from
    /health means another daemon squatting the port fails the check instead of
    satisfying it.
    """
    assert _HEALTHCHECK.exists(), f"missing {_HEALTHCHECK}"
    src = _HEALTHCHECK.read_text()

    assert "JOBD_HOST" in src, "healthcheck must probe $JOBD_HOST, not a fixed address"
    assert "/health" in src, "healthcheck must call the broker's /health endpoint"
    assert '"status"' in src or "'status'" in src, (
        "healthcheck must assert jobd's own /health payload (status == ok). Without "
        "a body check, any daemon listening on the port satisfies the probe — the "
        "exact failure this replaced."
    )
    assert "socket.create_connection" not in src, (
        "healthcheck reverted to a bare TCP connect, which cannot distinguish jobd "
        "from any other process holding the port."
    )


def test_pytest_timeout_plugin_active(request):
    """pyproject declares pytest-timeout (timeout = 300) as the CI hang guard —
    but the committed uv.lock predated the dependency for days (audit
    2026-07-05): `uv sync --frozen` installs the lock verbatim WITHOUT checking
    it against pyproject, so CI ran with the ini option silently ignored and no
    hang protection. Mechanism check: the plugin must actually be loaded in the
    environment running the tests."""
    assert request.config.pluginmanager.hasplugin("timeout"), (
        "pytest-timeout is not installed/active — regenerate uv.lock (`uv lock`) "
        "so the dev extra matches pyproject.toml"
    )


def test_uv_lock_in_sync_with_pyproject():
    """CI installs with `uv sync --frozen`, which never validates the lock
    against pyproject.toml — a stale committed lock silently drops added or
    bumped dependencies (how pytest-timeout went missing). `uv lock --check`
    is that missing gate.

    THIS GATE ONLY WORKS IF THE RUNNER DOES NOT RE-LOCK. `uv run` syncs before
    it runs, rewriting a stale uv.lock in place — so `uv run pytest` handed this
    test a lockfile it had just repaired, and the assertion below could only ever
    pass. The committed lock sat at jobd 0.5.16 through seven releases (pyproject:
    0.5.23) with CI green throughout. `UV_NO_SYNC: "1"` at the top of ci.yml is
    what keeps this honest; removing it makes this test decoration again.
    """
    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH")
    result = subprocess.run(
        ["uv", "lock", "--check"], cwd=_REPO_ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"uv.lock is stale vs pyproject.toml — run `uv lock` and commit:\n{result.stderr}"
    )
