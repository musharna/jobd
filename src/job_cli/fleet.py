"""`job fleet` — bootstrap and inspect workers across the fleet.

`fleet add` is the one-command answer to the manual closing instructions of
scripts/install-worker.sh: it pushes the installer AND update-worker.sh over a
single ssh connection (everything travels on stdin — the API token never
appears on an argv, local or remote), generates systemd user units wired to
one shared env file (the update unit's "ONE env file" doctrine), enables them,
and verifies the worker actually registered with the broker.

The installed version is pinned to the broker's own (resolved locally via
/health before any ssh happens) — a fresh worker must never run ahead of its
broker, the schema authority.

Assets: the shell scripts live canonically in scripts/ (where the deploy-lints
and CD tests read them); the wheel carries copies under jobd/fleet_assets/ via
a pyproject force-include, and _asset() falls back to the repo checkout so an
editable install works too. tests/test_fleet_cli.py builds a real wheel to
prove the force-include never silently drops.
"""

from __future__ import annotations

import subprocess
import time
from importlib import resources
from pathlib import Path

import typer

_ASSET_NAMES = ("install-worker.sh", "update-worker.sh")

fleet_app = typer.Typer(no_args_is_help=True, help="Bootstrap and inspect fleet workers.")


def _asset(name: str) -> str:
    """Read a packaged fleet asset; fall back to the repo checkout's scripts/
    (editable installs don't materialize the wheel's force-include)."""
    if name not in _ASSET_NAMES:  # pragma: no cover - programmer error
        raise ValueError(f"unknown fleet asset {name!r}")
    packaged = resources.files("jobd") / "fleet_assets" / name
    if packaged.is_file():
        return packaged.read_text()
    repo_copy = Path(__file__).resolve().parents[2] / "scripts" / name
    return repo_copy.read_text()


# --- generated systemd user units -------------------------------------------
# These mirror scripts/job-worker.service / jobd-worker-update.{service,timer},
# with the EDIT-me placeholders resolved: one shared EnvironmentFile carries
# JOBD_URL / JOBD_API_TOKEN / JOBD_WORKER_HOST (a second copy of the token is a
# second thing to rotate), and ExecStart points at the pushed updater copy —
# NOT %h/jobd/scripts/... which only exists on hosts with a git clone.
# tests/test_fleet_cli.py pins the load-bearing directives (KillMode,
# TimeoutStopSec, Restart, drain rationale) to the committed template so the
# two can't drift.

_ENV_FILE = "%h/.config/jobd/worker.env"

_WORKER_UNIT = f"""[Unit]
Description=jobd worker — polls broker, runs assigned jobs
After=network-online.target

[Service]
Type=simple
EnvironmentFile={_ENV_FILE}
Environment="PATH=/usr/local/bin:/usr/bin:/bin"
ExecStart=%h/jobd-worker/.venv/bin/jobd-worker
Restart=on-failure
RestartSec=10
# SIGTERM drain: TERM only the worker process at stop — control-group mode
# would also TERM fast-path job children at t=0, racing the drain and
# mislabeling them `failed` instead of `preempted`.
KillMode=mixed
# Stop budget = worst-case long-poll latency (~35s) + drain grace (60s) + margin.
TimeoutStopSec=150
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""

_UPDATE_SERVICE = f"""[Unit]
Description=jobd worker self-update — match the broker's version, but never mid-job
Documentation=file://%h/jobd-worker/update-worker.sh

[Service]
Type=oneshot
EnvironmentFile={_ENV_FILE}
ExecStart=%h/jobd-worker/update-worker.sh
"""

_UPDATE_TIMER = """[Unit]
Description=Check for a jobd worker update every 15 minutes
Documentation=file://%h/jobd-worker/update-worker.sh

[Timer]
OnBootSec=3min
OnUnitActiveSec=15min
# Monotonic timers: a machine asleep at the scheduled tick still gets one
# shortly after it wakes — this fleet is laptops and desktops that sleep.
AccuracySec=1min
RandomizedDelaySec=2min

[Install]
WantedBy=timers.target
"""


def _heredoc(path: str, content: str, *, mode: str | None = None) -> str:
    """Emit a quoted-heredoc write of `content` to `path`. The delimiter is
    deliberately improbable; the quoted form disables all expansion, so
    tokens/scripts pass through byte-exact."""
    delim = "JOBD_FLEET_EOF_7f3a"
    if delim in content:  # pragma: no cover - would need a hostile asset
        raise ValueError(f"heredoc delimiter collision writing {path}")
    lines = [f"cat > {path} <<'{delim}'", content.rstrip("\n"), delim]
    if mode:
        lines.append(f"chmod {mode} {path}")
    return "\n".join(lines)


def _bootstrap_script(
    *,
    broker_url: str,
    token: str,
    host_name: str,
    version: str,
    tags: str,
    systemd: bool,
) -> str:
    """The single script piped to `ssh <target> bash -s`. Secrets appear only
    inside heredoc bodies on stdin — never on an argv on either side."""
    env_file = f"JOBD_URL={broker_url}\nJOBD_API_TOKEN={token}\nJOBD_WORKER_HOST={host_name}\n"
    installer_args = f"--broker {broker_url} --host {host_name} --version {version}"
    if tags:
        installer_args += f" --tags {tags}"

    parts = [
        "set -euo pipefail",
        "mkdir -p ~/jobd-worker ~/.config/jobd ~/.config/systemd/user",
        "umask 077",
        _heredoc("~/jobd-worker/.fleet-install.sh", _asset("install-worker.sh")),
        f"bash ~/jobd-worker/.fleet-install.sh {installer_args}",
        _heredoc("~/jobd-worker/update-worker.sh", _asset("update-worker.sh"), mode="755"),
        _heredoc("~/.config/jobd/worker.env", env_file, mode="600"),
    ]
    if systemd:
        parts += [
            _heredoc("~/.config/systemd/user/job-worker.service", _WORKER_UNIT),
            _heredoc("~/.config/systemd/user/jobd-worker-update.service", _UPDATE_SERVICE),
            _heredoc("~/.config/systemd/user/jobd-worker-update.timer", _UPDATE_TIMER),
            "systemctl --user daemon-reload",
            "systemctl --user enable --now job-worker.service",
            "systemctl --user enable --now jobd-worker-update.timer",
            # Without linger the user manager — and the worker — dies at logout.
            # Enabling it needs privileges; report instead of sudo-ing.
            'linger=$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null || echo unknown)',
            'echo "fleet-add: linger=$linger"',
        ]
    parts.append('echo "fleet-add: bootstrap complete"')
    return "\n".join(parts) + "\n"


def _broker_version(client) -> str:
    r = client.get("/health")
    r.raise_for_status()
    return r.json()["version"]


@fleet_app.command("add")
def fleet_add(
    target: str = typer.Argument(
        ..., help="ssh destination for the new worker, e.g. user@host or a ssh-config alias"
    ),
    host_name: str = typer.Option(
        None,
        "--host-name",
        help="Stable worker name shown in `job workers` (default: the ssh target's hostname)",
    ),
    tags: str = typer.Option("", "--tags", help="Extra capability tags, comma-separated"),
    version: str = typer.Option(
        "", "--version", help="jobd version to install (default: the broker's own)"
    ),
    no_systemd: bool = typer.Option(
        False, "--no-systemd", help="Install the venv + config only; skip units and timers"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Run the remote detection preflight only; change nothing"
    ),
):
    """Bootstrap a jobd worker on TARGET over ssh — install pinned to the
    broker's version, systemd units + self-update timer wired to one shared
    env file, then verify the worker registers."""
    from job_cli.cli import _client

    with _client() as c:
        broker_version = version or _broker_version(c)
        broker_url = c.base_url
        token = c.token

    if not host_name:
        # `user@host` → host; a bare ssh alias is used as-is.
        host_name = target.rsplit("@", 1)[-1]

    typer.echo(f"fleet add {target}: jobd {broker_version} (pinned to broker), host={host_name}")

    if dry_run:
        # Preflight only: push the installer and run its own --dry-run
        # detection on the remote; nothing is written.
        script = "\n".join(
            [
                "set -euo pipefail",
                _heredoc("/tmp/.jobd-fleet-preflight.sh", _asset("install-worker.sh")),
                f"bash /tmp/.jobd-fleet-preflight.sh --broker {broker_url} "
                f"--host {host_name} --version {broker_version} --dry-run",
                "rm -f /tmp/.jobd-fleet-preflight.sh",
            ]
        )
    else:
        script = _bootstrap_script(
            broker_url=broker_url,
            token=token,
            host_name=host_name,
            version=broker_version,
            tags=tags,
            systemd=not no_systemd,
        )

    proc = subprocess.run(["ssh", target, "bash -s"], input=script, text=True)
    if proc.returncode != 0:
        typer.echo(f"fleet add: bootstrap on {target} failed (ssh exit {proc.returncode})")
        raise typer.Exit(1)
    if dry_run:
        return
    if no_systemd:
        typer.echo(
            "fleet add: installed without systemd units. Start manually with:\n"
            f"  ssh {target} '~/jobd-worker/.venv/bin/jobd-worker'"
        )
        return

    # The worker long-polls immediately after systemd starts it; give it a
    # minute to appear in the broker's registry before calling it a failure.
    typer.echo("fleet add: waiting for the worker to register with the broker…")
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        with _client() as c:
            r = c.get("/workers")
            r.raise_for_status()
            for w in r.json():
                if w.get("host") == host_name:
                    typer.echo(
                        f"fleet add: {host_name} registered — jobd {w.get('version')}, "
                        f"state={w.get('state')}"
                    )
                    return
        time.sleep(2.0)
    typer.echo(
        f"fleet add: {host_name} did not register within 60s. Check the remote unit:\n"
        f"  ssh {target} 'systemctl --user status job-worker.service'"
    )
    raise typer.Exit(1)


@fleet_app.command("status")
def fleet_status():
    """Fleet at a glance: every worker's version vs the broker's, state, and
    load — the drift the version_drift sweep event warns about, on demand."""
    from job_cli.cli import _client

    with _client() as c:
        broker_version = _broker_version(c)
        r = c.get("/workers")
        r.raise_for_status()
        workers = r.json()

    typer.echo(f"broker: {broker_version}")
    if not workers:
        typer.echo("no workers registered")
        return
    for w in sorted(workers, key=lambda x: x.get("host", "")):
        v = w.get("version") or "pre-0.5.22"
        drift = "" if v == broker_version else f"  ⚠ behind broker ({broker_version})"
        typer.echo(
            f"{w.get('host')}: {v}  state={w.get('state')}  "
            f"running={w.get('running')}/{w.get('max_concurrent')}{drift}"
        )
