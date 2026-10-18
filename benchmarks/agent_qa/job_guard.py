"""The `job` the alt arm finds on its PATH: the real CLI, minus every mutation.

The benchmark asks read-only questions about the LIVE broker, so the alt arm
gets a `job` that runs only subcommands that cannot change anything there.
It is an allowlist: a subcommand not named below is refused, so a mutating
command added to the CLI later is refused too until someone lists it here.

- Allowed: ``list``, ``status``, ``logs``, ``workers``, ``ping``,
  ``classify``, ``graph``, ``audit``, ``wait``, ``projects list``,
  ``--help``/``--version``.
- ``submit`` only with ``--explain`` (POST /resolve: prints the resolved
  config, enqueues nothing). The guard re-inserts ``--explain`` as the first
  token after ``submit``, so it cannot be swallowed as another option's value
  or hidden after ``--``. The CLI has no ``--dry-run``.
- Refused (exit 3, nothing executed): ``cancel``, ``preempt``,
  ``preempt-blockers``, ``adopt``, ``delete-worker``, ``projects set``,
  ``projects nudge``, ``fleet …``, ``gpu-holders`` (runs a probe on the broker
  host), ``--install-completion`` (writes a shell rc file), and anything else.

The read-only proxy (``readonly_proxy.py``) refuses the same mutations again at
the HTTP layer, and the arm's ``--disallowedTools`` names them a third time.

The real CLI to run is ``$JOBD_BENCH_REAL_JOB``, a JSON list (default: the
pinned PyPI release through uvx). Tests point it at a stub that records calls.
It runs with ``JOBD_URL`` set to ``$JOBD_BENCH_PROXY_URL`` and without
``JOBD_API_TOKEN``, whatever the calling shell exported (``child_env``).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

REFUSED_EXIT = 3
DEFAULT_REAL = ["uvx", "--from", "jobd==0.5.51", "job"]
READ_ONLY = {"list", "status", "logs", "workers", "ping", "classify", "graph", "audit", "wait"}
GLOBAL_OK = {"--help", "-h", "--version", "-V"}
NEVER = {"--install-completion", "--show-completion"}


def decide(argv: list[str]) -> tuple[bool, list[str] | str]:
    """(True, argv to run) or (False, why it was refused)."""
    if any(a in NEVER or a.startswith(("--install-completion", "--show-completion")) for a in argv):
        return False, "shell-completion options write to your shell config"
    if not argv:
        return True, []
    head = argv[0]
    if head.startswith("-"):
        return (True, argv) if head in GLOBAL_OK else (False, f"unknown global option {head}")
    if head in READ_ONLY:
        return True, argv
    if head == "projects":
        if len(argv) == 1 or argv[1] in ("list", "--help", "-h"):
            return True, argv
        return False, f"`job projects {argv[1]}` changes project priorities on the broker"
    if head == "submit":
        rest = argv[1:]
        if rest in (["--help"], ["-h"]):
            return True, argv
        opts = rest[: rest.index("--")] if "--" in rest else rest
        if "--explain" not in opts:  # after `--` it is part of the command, not a flag
            return False, "`job submit` would enqueue a job; only `job submit --explain` is allowed"
        return True, ["submit", "--explain", *rest]
    return False, f"`job {head}` is not a read-only command"


def child_env(env: dict[str, str]) -> dict[str, str] | None:
    """The environment the real CLI runs with: pointed at the proxy, no token.

    Not inherited, because the shell Claude Code's Bash tool runs in re-reads
    the user's startup files: in the pilot ~/.zshenv re-exported the live
    JOBD_URL and JOBD_API_TOKEN over the ones run.py set, and `job` reached the
    broker without the proxy seeing it. The proxy URL travels in a variable no
    startup file sets. None when it is missing: then nothing runs."""
    proxy = env.get("JOBD_BENCH_PROXY_URL")
    if not proxy:
        return None
    out = {k: v for k, v in env.items() if k != "JOBD_API_TOKEN"}
    out["JOBD_URL"] = proxy
    return out


def main() -> None:
    allowed, out = decide(sys.argv[1:])
    if not allowed:
        sys.stderr.write(f"job: refused by the benchmark's read-only guard: {out}\n")
        sys.exit(REFUSED_EXIT)
    assert isinstance(out, list)
    env = child_env(dict(os.environ))
    if env is None:
        sys.stderr.write(
            "job: refused by the benchmark's read-only guard: no JOBD_BENCH_PROXY_URL\n"
        )
        sys.exit(REFUSED_EXIT)
    real = json.loads(env.get("JOBD_BENCH_REAL_JOB") or json.dumps(DEFAULT_REAL))
    os.execvpe(real[0], [*real, *out], env)


def write_shim(bindir: Path, python: str = sys.executable) -> Path:
    """Write ``bindir/job``: a shell shim that runs a copy of this guard. Put
    ``bindir`` first on PATH and every bare ``job`` goes through ``decide``.
    The copy sits beside the shim, so reading the shim does not point an
    agent at the benchmark's directory."""
    bindir.mkdir(parents=True, exist_ok=True)
    guard = bindir / "job_guard.py"
    shutil.copyfile(Path(__file__).resolve(), guard)
    shim = bindir / "job"
    shim.write_text(f'#!/bin/sh\nexec "{python}" -I "{guard}" "$@"\n')
    shim.chmod(0o755)
    return shim


if __name__ == "__main__":
    main()
