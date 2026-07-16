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


# --- worker CD: the idle gate ---------------------------------------------
#
# The workers had no deployment story at all while the broker self-deployed, so
# they drifted — and a fix that never reaches a worker is not a fix. The
# `cwd_missing` pre-dispatch check shipped in v0.5.10 and did nothing for twelve
# days because the workers were still on 0.5.3/0.5.7. Nine jobs died of the exact
# fault it prevents. CI was green throughout.
#
# scripts/update-worker.sh closes that. Its one dangerous power is `systemctl
# restart`, which SIGTERMs any in-flight job (60s drain, then worker_shutdown --
# 13 jobs died that way on 2026-07-13/14, every one killed by a human upgrading a
# worker). So the idle gate is the whole safety property, and these tests guard it.

_UPDATE_WORKER = _REPO_ROOT / "scripts" / "update-worker.sh"


def test_update_worker_script_exists_and_is_executable():
    assert _UPDATE_WORKER.exists(), "scripts/update-worker.sh is missing"
    assert _UPDATE_WORKER.stat().st_mode & 0o111, "update-worker.sh is not executable"


def test_update_worker_never_restarts_without_checking_idle_first():
    """THE safety property. Every `systemctl restart` must be dominated by a
    running-count check, or an upgrade silently kills whatever the worker is
    doing — which is precisely the accident this script was written to stop."""
    lines = _UPDATE_WORKER.read_text().splitlines()
    restart_lines = [i for i, ln in enumerate(lines) if "systemctl" in ln and "restart" in ln]
    assert restart_lines, "no restart in update-worker.sh — did the script change shape?"

    gate_lines = [i for i, ln in enumerate(lines) if "running=$(busy || true)" in ln]
    assert gate_lines, "the idle gate (`running=$(busy || true)`) is gone from update-worker.sh"

    for r in restart_lines:
        assert any(g < r for g in gate_lines), (
            f"line {r + 1} restarts the worker with no preceding idle check. A restart "
            "SIGTERMs the in-flight job (60s grace, then worker_shutdown)."
        )


def test_update_worker_rechecks_idle_after_the_pip_install():
    """pip takes seconds, and the worker long-polls — it can claim a job in that
    window. One check before the install is not enough; the gate must be re-read
    immediately before the restart."""
    body = _UPDATE_WORKER.read_text()
    # Anchor on the INVOCATION, not the word "pip" — which also appears in the
    # comments above, before the first gate. Anchoring on the substring made this
    # test pass with the recheck deleted: the slice still contained the *first*
    # gate. A guard that inspects the wrong text is not a guard (see the
    # HEALTHCHECK parser above, same mistake).
    install_at = body.index("\ninstall_jobd\n")  # the CALL, not the function body
    restart_at = body.index("systemctl --user restart")
    assert install_at < restart_at, "the install must precede the restart"
    between = body[install_at:restart_at]
    assert "running=$(busy || true)" in between, (
        "update-worker.sh installs, then restarts, without re-checking idle. The "
        "worker long-polls and can claim a job during the pip install — and would "
        "then lose it to the restart, which is the exact accident this script exists "
        "to prevent."
    )


def test_update_worker_gate_reads_job_rows_not_the_heartbeat_gauge():
    """The /workers `running` gauge is refreshed only by the worker's own
    heartbeat — a job claimed via long-poll right after a heartbeat is invisible
    to it for up to ~5s, which is a real gate-miss window. Job rows are written
    synchronously by the /next-job claim, so the gate must count those
    (audit 2026-07-15 F2)."""
    body = _UPDATE_WORKER.read_text()
    assert "/jobs?state_filter=" in body, (
        "the idle gate no longer reads job rows at all — the heartbeat gauge lags "
        "claims by up to a heartbeat interval and will miss one"
    )
    assert "count_state assigned" in body and "count_state running" in body, (
        "the gate must count BOTH assigned and running rows: an assigned job is a "
        "claim the restart would kill just as dead as a running one"
    )
    assert "/workers" not in body, (
        "update-worker.sh consults /workers again — that gauge is heartbeat-lagged; "
        "the gate must read job rows"
    )


def test_no_script_passes_the_bearer_token_on_argv():
    """`curl -H "Authorization: Bearer $TOKEN"` puts the token in argv, which any
    local user can read from /proc/*/cmdline for the lifetime of every
    timer-driven curl. The scripts pass it via a /dev/fd curl config instead
    (audit 2026-07-15)."""
    for s in sorted((_REPO_ROOT / "scripts").glob("*.sh")):
        code = "\n".join(ln for ln in s.read_text().splitlines() if not ln.lstrip().startswith("#"))
        assert '-H "Authorization' not in code, (
            f"{s.name} passes the bearer token on curl's argv — readable in "
            "/proc/*/cmdline by any local user. Use `-K <(printf 'header = ...')`."
        )


def test_update_worker_targets_the_broker_version_not_pypi_latest():
    """A worker must never run ahead of its broker: the broker is the schema
    authority. Pulling 'latest' from PyPI would upgrade workers past it."""
    body = _UPDATE_WORKER.read_text()
    assert "/health" in body, "update-worker.sh must read its target from the broker's /health"
    assert "pip install --upgrade jobd[worker]\n" not in body, (
        "unpinned upgrade — the worker would jump to PyPI latest, ahead of the broker"
    )
    assert '=="${target}"' in body or "==${target}" in body, (
        "the install must PIN the broker's exact version"
    )


def test_update_worker_handles_both_pip_and_uv_venvs():
    """This fleet has TWO venv shapes and no single install tool spans them:
      - the laptop's venv is uv-managed and has NO pip binary;
      - the other three carry their own pip and two of them have no uv at all.
    The script broke TWICE on this — first assuming pip everywhere (127 on the
    laptop), then assuming uv everywhere (127 on the three pip hosts). Both were
    invisible to the text lints here and only surfaced on real execution (see
    real-execution-testing). The correct installer tries the venv's pip and
    falls back to uv, so BOTH invocations must be present.
    """
    code = "\n".join(
        ln for ln in _UPDATE_WORKER.read_text().splitlines() if not ln.lstrip().startswith("#")
    )
    assert '"$VENV/bin/pip" install' in code, (
        "the installer must use the venv's own pip when present — three of four "
        "hosts have a pip venv and no uv, and a uv-only install 127s on them."
    )
    assert '"$UV" pip install' in code, (
        "the installer must fall back to uv for a pip-less (uv-managed) venv — the "
        "laptop's venv has no pip, and a pip-only install 127s on it."
    )


def test_committed_units_do_not_use_unexpanded_shell_vars_in_environment():
    """systemd does NOT do shell expansion in Environment= — `$HOME/x` is passed
    to the process as the literal 6 characters `$HOME/`, not the home directory.
    A deploy helper injected `Environment=JOBD_WORKER_VENV=$HOME/jobd-worker/.venv`
    into three workers' update units; the script then looked for jobd under a
    literal `$HOME` path, found nothing (installed=none), and — with the pip bug —
    127'd. Use an absolute path or a systemd specifier (%h) instead. Guard every
    committed unit so this gotcha cannot re-enter through one.
    """
    import re

    # *.timer files carry Environment= just as legally as *.service files, and
    # the .timer half of this pipeline already shipped one bug (an inert
    # Persistent=, dropped in 8651b2d) — lint both (audit 2026-07-15).
    units = sorted(_REPO_ROOT.glob("scripts/*.service")) + sorted(
        _REPO_ROOT.glob("scripts/*.timer")
    )
    assert units, "no systemd units under scripts/ — did they move?"
    for unit in units:
        for ln in unit.read_text().splitlines():
            s = ln.strip()
            if s.startswith("#") or not s.startswith("Environment"):
                continue
            value = s.split("=", 1)[1] if "=" in s else ""
            assert not re.search(r"\$\{?[A-Za-z_]", value), (
                f"{unit.name}: `{s}` puts a shell variable in Environment=. systemd "
                "will NOT expand it — use an absolute path or a %h/%i specifier."
            )


def test_the_worker_host_is_not_a_systemd_hostname_specifier():
    """%l is the machine hostname (SCAR18); the worker's identity to the broker is
    a stable name (laptop). The idle gate looks the worker up BY THAT NAME — get it
    wrong and the lookup matches nothing, reads running=0, and restarts a busy host.
    A gate that cannot find itself always says 'idle'."""
    unit = (_REPO_ROOT / "scripts" / "jobd-worker-update.service").read_text()
    assert "JOBD_WORKER_HOST=%l" not in unit and "JOBD_WORKER_HOST=%H" not in unit, (
        "JOBD_WORKER_HOST is being set from a systemd hostname specifier. It must come "
        "from the shared env file, or the idle gate silently matches no worker."
    )
    assert "EnvironmentFile" in unit, "the update unit must share the worker's env file"


def test_every_shell_script_actually_parses():
    """`bash -n` on everything in scripts/.

    The deploy-lint tests above read these scripts as TEXT — they grep for an idle
    gate, a pinned version, a bind address. Not one of them would notice that the
    file is not a valid shell script at all. update-worker.sh shipped with an
    apostrophe inside a `${VAR:?word}` expansion (bash does quote processing in
    there, so it opened a quote that never closed); every text-matching guard was
    green and the script died at line 1 with `unexpected EOF`.

    Grepping a script is not the same as knowing it runs.
    """
    scripts = sorted((_REPO_ROOT / "scripts").glob("*.sh"))
    assert scripts, "no shell scripts found — did scripts/ move?"
    broken = []
    for s in scripts:
        r = subprocess.run(["bash", "-n", str(s)], capture_output=True, text=True)
        if r.returncode != 0:
            broken.append(f"{s.name}: {r.stderr.strip().splitlines()[-1] if r.stderr else '?'}")
    assert not broken, "shell scripts with syntax errors:\n  " + "\n  ".join(broken)


# --- release/supply-chain (audit 2026-07-15 T-2 / Sec-B / Sec-C) -------------

_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_REQ_DOCKER = _REPO_ROOT / "requirements-docker.txt"


def _req_lines(text: str) -> list[str]:
    """Requirement lines only — the autogenerated header embeds the -o path, so
    comments must not participate in the comparison."""
    return [
        ln.rstrip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
    ]


def test_requirements_docker_in_sync_with_uv_lock(tmp_path):
    """The image installs requirements-docker.txt (hashed, exported from
    uv.lock). If it drifts from the lock, the shipped image again carries
    dependency versions CI never tested — the exact hole it exists to close."""
    out = tmp_path / "req.txt"
    result = subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "-o",
            str(out),
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"uv export failed:\n{result.stderr}"
    assert _req_lines(out.read_text()) == _req_lines(_REQ_DOCKER.read_text()), (
        "requirements-docker.txt is stale vs uv.lock — regenerate with:\n"
        "  uv export --frozen --no-dev --no-emit-project --format requirements-txt "
        "-o requirements-docker.txt"
    )


def test_dockerfile_installs_only_hashed_pretested_dependencies():
    """`pip install .` resolved every dependency fresh from PyPI at build time —
    untested versions in the shipped image, and dependency-hijack exposure on
    every release build. The image must install the hash-pinned export, and any
    install of the project itself must be --no-deps."""
    body = _DOCKERFILE.read_text()
    assert "--require-hashes" in body and "requirements-docker.txt" in body, (
        "the Dockerfile no longer installs the hash-pinned requirements export"
    )
    for ln in body.splitlines():
        s = ln.strip()
        if s.startswith("#"):
            continue  # prose about pip install is not an install
        if "pip install" in s and s.rstrip("\\").rstrip().endswith(" ."):
            assert "--no-deps" in s, (
                f"`{s}` can resolve dependencies at build time — every "
                "project install in the image must be --no-deps"
            )


def test_dockerfile_copies_every_wheel_force_include_before_project_install():
    """hatchling's force-include sources must exist in the image build context
    when `pip install .` generates metadata — a missing one is a hard
    FileNotFoundError. This is invisible to the PR suite (the wheel test builds
    from a full checkout) and killed the v0.5.29 GHCR publish at the one moment
    it could: on the tag. Every force-include source must be COPY'd into the
    Dockerfile before the project install line."""
    import tomllib

    fi = (
        tomllib.loads(_PYPROJECT.read_text())
        .get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
        .get("force-include", {})
    )
    lines = _DOCKERFILE.read_text().splitlines()
    install_at = next(
        i for i, line in enumerate(lines) if line.strip().startswith("RUN pip install --no-deps .")
    )
    copied_before = "\n".join(line for line in lines[:install_at] if line.startswith("COPY"))
    missing = [src for src in fi if src not in copied_before]
    assert not missing, (
        f"pyproject force-include sources not COPY'd into the Dockerfile before "
        f"`pip install --no-deps .`: {missing} — the image build will "
        f"FileNotFoundError during metadata generation (v0.5.29 incident)."
    )


def test_dockerfile_base_image_is_digest_pinned():
    from_lines = [ln for ln in _DOCKERFILE.read_text().splitlines() if ln.startswith("FROM ")]
    assert from_lines, "no FROM in the Dockerfile?"
    for ln in from_lines:
        assert "@sha256:" in ln, (
            f"`{ln}` pins a mutable tag — a re-pushed base tag changes the image "
            "under us silently. Pin the digest (audit 2026-07-15 Sec-B)."
        )


def test_no_workflow_disables_image_provenance():
    """provenance:false strips the one counter-signal a tag-following automated
    deploy has (audit Sec-C)."""
    for wf in sorted(_WORKFLOWS.glob("*.yml")):
        assert "provenance: false" not in wf.read_text(), (
            f"{wf.name} disables buildx provenance attestations"
        )


def test_release_publishers_gate_on_tests_and_version_lockstep():
    """T-2: a tag pushed against a stale pyproject must not publish a mislabeled
    image (a poisoned rollback target); and a release must not build from a
    commit whose tests never ran (this repo shipped a red main to a tag's reach
    once already)."""
    wf = yaml.safe_load((_WORKFLOWS / "release.yml").read_text())
    jobs = wf["jobs"]
    assert "verify" in jobs, "release.yml lost the tag↔pyproject verify job"
    assert "test" in jobs, "release.yml lost the test job — releases would ship untested"
    for publisher in ("pypi", "docker"):
        needs = jobs[publisher].get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        assert "test" in needs, f"release.yml `{publisher}` no longer waits for tests"
        # verify gates transitively through build; require at least one path.
        build_needs = jobs["build"].get("needs", [])
        build_needs = [build_needs] if isinstance(build_needs, str) else build_needs
        assert "verify" in needs or ("build" in needs and "verify" in build_needs), (
            f"release.yml `{publisher}` can publish without the version-lockstep check"
        )


def test_ci_enforces_a_branch_coverage_floor():
    """The coverage gate is one edited line away from being decorative: drop
    `--cov-fail-under` and CI still prints a pretty report while gating
    nothing. Require the floor AND `--cov-branch` (line coverage alone lets an
    untested `else` arm count as covered because its `if` line ran)."""
    wf = yaml.safe_load((_WORKFLOWS / "ci.yml").read_text())
    runs = [
        step.get("run") or ""
        for job in (wf.get("jobs") or {}).values()
        for step in job.get("steps") or []
    ]
    gated = [r for r in runs if "--cov-fail-under=" in r]
    assert gated, "ci.yml lost the coverage floor (--cov-fail-under)"
    assert any("--cov-branch" in r for r in gated), (
        "the coverage gate no longer measures branch coverage"
    )


def test_workflow_run_blocks_do_not_splice_github_expressions():
    """`${{ inputs.* }}` or `${{ github.event.* }}` inside a run: script is
    shell injection into a job that may hold packages:write — route untrusted
    values through env: instead (audit 2026-07-15 LOW-1)."""
    import re as _re

    pat = _re.compile(r"\$\{\{\s*(inputs|github\.event)\.")
    for wf in sorted(_WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(wf.read_text())
        for jname, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                run = step.get("run")
                if run and pat.search(run):
                    raise AssertionError(
                        f"{wf.name} job `{jname}` splices a GitHub expression into a "
                        f"run: script — use env: and a shell variable instead"
                    )
