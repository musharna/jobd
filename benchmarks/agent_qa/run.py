"""Ask every question to Claude Code under each arm, and score the replies.

Arms (same model, same prompt, isolated: no user settings, hooks or other MCP
servers, an empty working directory per run):

- ``bare``: no tools at all.
- ``alt``:  Bash, allowed to run only the ``job`` CLI (jobd ``VERSION`` from
  PyPI, behind ``job_guard.py``) and a few read-only text tools to filter its
  output. This is what a user has without the MCP server.
- ``mcp``:  jobd-mcp (jobd[mcp] ``VERSION`` from PyPI via uvx) only, without
  ``jobd_cancel``, ``jobd_preempt`` and ``jobd_worker_delete``.

SAFETY. The questions are about the LIVE broker, so every arm reaches it only
through ``readonly_proxy.py``: one loopback proxy per run, which holds the
token, refuses every mutating request with a 403, and logs each decision.
``JOBD_API_TOKEN`` is removed from the arm's environment, so a request that
skips the proxy is a 401. Mutating tools are also named in
``--disallowedTools``, and the alt arm's ``job`` refuses mutating subcommands
before they run.

Before any run, ``drift`` re-reads every job and the fleet the key was built
from; a changed answer stops the sweep (``--allow-drift`` overrides). A broker
on another version than the key's stops it too, unless
``--accept-broker <live version>`` names that exact version; then the key must
show no drift at all, and both versions go into ``versions.json``.

``--model`` runs every arm on another Claude model (default ``MODEL``). Its
transcripts and results default to ``runs-<model>/`` and
``results-<model>.jsonl``, so the default model's are never overwritten; each
row records the model the init event says served it, and a run served by any
other model stops the sweep.

Each finished run is appended to ``results.jsonl`` with its score; a rerun
skips (arm, question, rep) triples already there, so an interrupted sweep
resumes. Nothing is retried: a timeout or a harness error is recorded as that
run's row (``exit``), in every arm alike, and ``analyze.py`` counts it invalid.
Raw stream-json transcripts go to ``runs/<arm>/<qid>-<rep>.jsonl``, proxy
decisions to ``runs/<arm>/<qid>-<rep>.proxy.jsonl``. All of it lives under
``ARTIFACTS`` (default ``~/jobd-artifacts/agent_qa``), outside the checkout;
``--key`` and ``--runs`` point at another key and transcript directory (the
2026-10-18 re-run uses ``ARTIFACTS/rerun-2026-10-18``).

``--server-spec git+https://github.com/musharna/jobd@<40-hex sha>`` runs the
mcp arm on an unreleased commit instead of the PyPI pin; ``run.py`` checks the
installed package came from that commit, and records the spec in every row
and in ``versions.json``. ``--exclude-newer`` bounds every other dependency's
resolution by date, in both arms.

Usage gate (the runs share the account's limits with its owner): after every
run, the transcript's ``rate_limit_event`` is read. A five-hour utilization at
or above ``PAUSE_AT`` or a ``rejected`` status pauses the sweep until that
window's ``resetsAt``; each pause is appended to ``pauses.jsonl``. A seven-day
utilization at or above ``STOP_AT`` stops the sweep. ``--drop-api-failures``
moves rows the API ended before the model answered (``api_failure``) to
``<results>.api_failures.jsonl`` so a resumed sweep runs each once more.

    JOBD_URL=... JOBD_API_TOKEN=... uv run python benchmarks/agent_qa/run.py --reps 3 --workers 1
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import build_hard
from build_questions import ARTIFACTS, Broker, drift
from job_guard import write_shim
from readonly_proxy import ReadOnlyProxy
from score import oracle_reply, score, wrong_reply

MODEL = "claude-opus-5-5"
DEFAULT_MODEL = MODEL
VERSION = "0.5.52"  # latest jobd on PyPI, 2026-10-20 (0.5.51 for the 10-16 and 10-18 sweeps)
SERVER = f"jobd[mcp]=={VERSION}"
GIT_SPEC = re.compile(r"^git\+https://github\.com/musharna/jobd@([0-9a-f]{40})$")
MCP_SERVER = ["uvx", "--from", SERVER, "jobd-mcp"]
CLI = ["uvx", "--from", f"jobd=={VERSION}", "job"]
PAUSE_AT = 0.85  # five-hour utilization that pauses the sweep until its reset
STOP_AT = 0.80  # seven-day utilization that stops it
RUN_TIMEOUT_S = 900
# Every arm gets this in place of Claude Code's own system prompt, which
# describes tools even when --tools "" removes them all.
SYSTEM = "You are a helpful assistant. Answer the user's question."
# The first sentence is for the alt arm: in the pilot it looked for a CLI
# named `jobd` (the broker's server binary), was denied, and declined. A user
# knows the client's name; every arm gets the sentence, so the prompt stays
# identical across arms.
PROMPT = (
    "jobd is a self-hosted job broker; its command-line client is called `job`.\n\n"
    "{question}\n\nGive the answer as {answer_format}. Keep your reply short, and end it "
    "with one line of the form\nANSWER: <answer>\nIf you cannot determine the answer, end "
    "with\nANSWER: UNKNOWN"
)
MUTATING_MCP = ["jobd_cancel", "jobd_preempt", "jobd_worker_delete"]
READ_TEXT_TOOLS = ["grep", "head", "tail", "wc", "sort", "uniq", "cut", "tr", "jq", "date", "expr"]
DENIED_BASH = [
    *(f"job {c}" for c in ("cancel", "preempt", "preempt-blockers", "adopt", "delete-worker",
                           "projects set", "projects nudge", "fleet", "gpu-holders")),
    "jobd", "jobd-worker", "jobd-mcp",  # the broker, a worker, the server: never started here
    "curl", "wget", "python", "python3", "uv", "uvx", "ssh", "nc", "bash", "sh", "env",
]  # fmt: skip


def server_cmd(spec: str, exclude_newer: str | None) -> list[str]:
    """uvx command for the mcp arm: the PyPI pin, or jobd[mcp] at a git commit."""
    pkg = spec if spec == SERVER else f"jobd[mcp] @ {spec}"
    return ["uvx", *(["--exclude-newer", exclude_newer] if exclude_newer else []),
            "--from", pkg, "jobd-mcp"]  # fmt: skip


def cli_cmd(exclude_newer: str | None) -> list[str]:
    return ["uvx", *(["--exclude-newer", exclude_newer] if exclude_newer else []), *CLI[1:]]


def check_server_spec(spec: str) -> str | None:
    """The PyPI pin, or a git spec naming a full commit; returns the commit."""
    if spec == SERVER:
        return None
    m = GIT_SPEC.match(spec)
    if not m:
        sys.exit(f"--server-spec must be {SERVER!r} or "
                 f"git+https://github.com/musharna/jobd@<40-hex sha>; got {spec!r}")  # fmt: skip
    return m.group(1)


def arm_flags(arm: str, tmp: Path, proxy_url: str, server: list[str] = MCP_SERVER) -> list[str]:
    empty = tmp / "no-mcp.json"
    empty.write_text('{"mcpServers": {}}')
    if arm == "bare":
        return ["--tools", "", "--strict-mcp-config", "--mcp-config", str(empty)]
    if arm == "alt":
        allowed = ",".join(f"Bash({c}:*)" for c in ["job", *READ_TEXT_TOOLS])
        denied = ",".join(f"Bash({c}:*)" for c in DENIED_BASH)
        return [
            "--tools", "Bash", "--allowedTools", allowed, "--disallowedTools", denied,
            "--strict-mcp-config", "--mcp-config", str(empty),
        ]  # fmt: skip
    if arm == "mcp":
        cfg = tmp / "mcp.json"
        cfg_server = {
            "command": server[0],
            "args": server[1:],
            # The server's call log defaults to ~/.claude/state/jobd-mcp; keep
            # benchmark calls out of the user's real one.
            "env": {"JOBD_URL": proxy_url, "JOBD_MCP_LOG_DIR": str(tmp / "mcp-log")},
        }
        cfg.write_text(json.dumps({"mcpServers": {"jobd": cfg_server}}))
        return [
            "--tools", "", "--allowedTools", "mcp__jobd",
            "--disallowedTools", ",".join(f"mcp__jobd__{t}" for t in MUTATING_MCP),
            "--strict-mcp-config", "--mcp-config", str(cfg),
        ]  # fmt: skip
    raise ValueError(arm)


def arm_env(arm: str, tmp: Path, proxy_url: str, cli: list[str] = CLI) -> dict[str, str]:
    """The parent environment minus the broker token, pointed at the proxy,
    with a TMPDIR of the run's own."""
    env = {k: v for k, v in os.environ.items() if k != "JOBD_API_TOKEN"}
    env["JOBD_URL"] = proxy_url
    (tmp / "tmp").mkdir()
    env["TMPDIR"] = str(tmp / "tmp")
    if arm == "alt":
        bindir = write_shim(tmp / "bin").parent
        env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
        env["JOBD_BENCH_REAL_JOB"] = json.dumps(cli)
        env["JOBD_BENCH_PROXY_URL"] = proxy_url
        # The Bash tool's zsh would source ~/.zshenv, which can export the live
        # broker's URL and token; an empty ZDOTDIR keeps the user's startup
        # files out. (job_guard.child_env does not rely on this.)
        zdot = tmp / "zdotdir"
        zdot.mkdir()
        env["ZDOTDIR"] = str(zdot)
    return env


# A `job` subcommand that talks to the broker. If an alt run used one and the
# proxy saw nothing, the CLI found a way around the proxy: stop everything.
_BROKER_CMD = re.compile(
    r"(?:^|[;&|(\s])job\s+(?:list|status|logs|workers|ping|graph|audit|wait|classify|projects|submit)\b"
)


class ProxyBypassed(RuntimeError):
    pass


class WrongModel(RuntimeError):
    pass


def set_model(model: str) -> None:
    """--model: every arm runs on ``model``."""
    global MODEL
    MODEL = model


def set_stop_at(level: float) -> None:
    """--stop-at: the seven-day utilization at which Gate stops the sweep."""
    global STOP_AT
    STOP_AT = level


def model_paths(model: str) -> tuple[Path, Path]:
    """Default transcript dir and results file for ``model``: the default
    model keeps ``runs/`` and ``results.jsonl``; any other gets its own."""
    if model == DEFAULT_MODEL:
        return ARTIFACTS / "runs", ARTIFACTS / "results.jsonl"
    return ARTIFACTS / f"runs-{model}", ARTIFACTS / f"results-{model}.jsonl"


def check_broker(live: str | None, key: str, accept: str | None) -> str | None:
    """Why the live broker cannot serve this key, or None. The answers depend
    on the broker's semantics, so its version must be the key's, or the one
    version ``--accept-broker`` names after a check that nothing changed."""
    if live == key or (accept is not None and live == accept):
        return None
    if accept is not None:
        return f"broker is {live}; --accept-broker names {accept}, the key was built on {key}"
    return f"broker is {live}, the key was built on {key}"


class Opts:
    """Per-sweep settings run_one needs (one object, so the pool call stays short)."""

    def __init__(self, runs: Path, spec: str, exclude_newer: str | None) -> None:
        self.runs = runs
        self.spec = spec
        self.exclude_newer = exclude_newer
        self.server = server_cmd(spec, exclude_newer)
        self.cli = cli_cmd(exclude_newer)


def run_one(
    arm: str, q: dict[str, Any], rep: int, upstream: str, token: str, o: Opts
) -> dict[str, Any]:
    out = o.runs / arm / f"{q['id']}-{rep}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    plog = out.with_suffix(".proxy.jsonl")
    plog.unlink(missing_ok=True)
    prompt = PROMPT.format(question=q["question"], answer_format=q["answer_format"])
    with (
        tempfile.TemporaryDirectory(prefix=f"agentqa-{arm}-") as d,
        ReadOnlyProxy(upstream, token, log=plog) as proxy,
    ):
        tmp = Path(d)
        work = tmp / "cwd"
        work.mkdir()
        cmd = [
            "claude", "-p", prompt, "--model", MODEL, "--setting-sources", "",
            "--output-format", "stream-json", "--verbose", "--system-prompt", SYSTEM,
            *arm_flags(arm, tmp, proxy.url, o.server),
        ]  # fmt: skip
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=work,
                env=arm_env(arm, tmp, proxy.url, o.cli),
                capture_output=True,
                text=True,
                timeout=RUN_TIMEOUT_S,
            )
            stdout, rc = proc.stdout, proc.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = (
                (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else exc.stdout or ""
            )
            rc = "timeout"
        seconds = round(time.monotonic() - t0, 1)
    out.write_text(stdout)
    events = [json.loads(ln) for ln in stdout.splitlines() if ln.startswith("{")]
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    result = next((e for e in events if e.get("type") == "result"), {})
    tools = [
        b["name"]
        for e in events
        if e.get("type") == "assistant"
        for b in e["message"].get("content", [])
        if b.get("type") == "tool_use"
    ]
    decisions = [json.loads(ln) for ln in plog.read_text().splitlines()] if plog.exists() else []
    commands = [
        b["input"].get("command", "")
        for e in events
        if e.get("type") == "assistant"
        for b in e["message"].get("content", [])
        if b.get("type") == "tool_use" and b["name"] == "Bash"
    ]
    if arm == "alt" and not decisions and any(_BROKER_CMD.search(c) for c in commands):
        raise ProxyBypassed(f"{arm} {q['id']} rep{rep}: `job` ran but the proxy saw no request; "
                            f"see {out}")  # fmt: skip
    if init.get("model") and init["model"] != MODEL:
        raise WrongModel(f"{arm} {q['id']} rep{rep}: asked for {MODEL}, served by "
                         f"{init['model']}; see {out}")  # fmt: skip
    reply = result.get("result") or ""
    return {
        "arm": arm,
        "qid": q["id"],
        "category": q["category"],
        "rep": rep,
        "server_spec": o.spec if arm == "mcp" else None,
        "exit": rc,
        "seconds": seconds,
        "claude_code": init.get("claude_code_version"),
        "model": init.get("model"),
        "mcp_servers": init.get("mcp_servers"),
        "tools_available": sorted(init.get("tools") or []),
        "tool_calls": tools,
        "broker_requests": len(decisions),
        "refused_requests": [f"{d['method']} {d['path']}" for d in decisions if not d["allowed"]],
        "cost_usd": result.get("total_cost_usd"),
        "reply": reply,
        "api_failure": api_failure(events, rc),
        "rate_limit": rate_limit(events),
        **score(q, reply),
    }


def rate_limit(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The run's last rate_limit_event, reduced to what the gate reads."""
    info = None
    for e in events:
        if e.get("type") == "rate_limit_event":
            info = e.get("rate_limit_info") or {}
    if info is None:
        return None
    w = info.get("unifiedWindows") or {}
    return {
        "status": info.get("status"),
        "five_hour": (w.get("five_hour") or {}).get("utilization"),
        "five_hour_resets": (w.get("five_hour") or {}).get("resetsAt") or info.get("resetsAt"),
        "seven_day": (w.get("seven_day") or {}).get("utilization"),
        "seven_day_resets": (w.get("seven_day") or {}).get("resetsAt"),
    }


def api_failure(events: list[dict[str, Any]], rc: object) -> str | None:
    """Why the API, not the model, ended this run before it answered, or None.

    Read from the transcript alone, never from the reply or its score, so the
    same rule picks rows in every arm: a rate-limit rejection with no
    successful result after it, or a result whose terminal reason is an API
    error. A timeout is not one: it stays in as an invalid run.
    """
    result = next((e for e in events if e.get("type") == "result"), None)
    ok = result is not None and result.get("subtype") == "success" and not result.get("is_error")
    rejected = any(
        e.get("type") == "rate_limit_event"
        and (e.get("rate_limit_info") or {}).get("status") == "rejected"
        for e in events
    )
    if rejected and not ok:
        return "rate_limited"
    if result is not None and result.get("terminal_reason") == "api_error":
        return "api_error"
    return None


def spend_stop() -> str | None:
    """Why an API-billed sweep must stop before its next run, or None.

    Set ``BENCH_SPEND_GATE`` to the shared spend meter's script and each run
    first asks it (``check``, track from ``BENCH_TRACK``); any non-zero exit
    (spend cap, billing violation, checkpoint) stops the sweep cleanly.
    """
    gate = os.environ.get("BENCH_SPEND_GATE")
    if not gate:
        return None
    p = subprocess.run([sys.executable, gate, "check"], capture_output=True, text=True, check=False)
    return None if p.returncode == 0 else (p.stderr.strip() or f"spend gate exit {p.returncode}")


class Gate:
    """Pauses the sweep when the account's five-hour window runs high, and
    stops it when the seven-day window does. Every pause is recorded."""

    def __init__(self, log: Path, sleep=time.sleep, now=time.time) -> None:
        self.log, self.sleep, self.now = log, sleep, now
        self.until = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        why = spend_stop()
        if why:
            raise UsageStop(why)
        while True:
            with self.lock:
                left = self.until - self.now()
            if left <= 0:
                return
            self.sleep(min(left, 300))

    def update(self, rl: dict[str, Any] | None, after: str) -> None:
        if not rl:
            return
        if (rl.get("seven_day") or 0) >= STOP_AT:
            raise UsageStop(f"seven_day utilization {rl['seven_day']} >= {STOP_AT} after {after}")
        hot = rl.get("status") == "rejected" or (rl.get("five_hour") or 0) >= PAUSE_AT
        if not hot:
            return
        resets = float(rl.get("five_hour_resets") or 0)
        until = max(resets + 60, self.now() + 60)
        with self.lock:
            if until <= self.until:
                return
            self.until = until
        entry = {"ts": self.now(), "after": after, **rl, "pause_until": until,
                 "pause_s": round(until - self.now())}  # fmt: skip
        with self.log.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
        print(f"PAUSE until {time.strftime('%H:%M:%S %Z', time.localtime(until))}: "
              f"five_hour={rl.get('five_hour')} status={rl.get('status')}", flush=True)  # fmt: skip


class UsageStop(RuntimeError):
    pass


def drop_api_failures(results: Path) -> None:
    """Move every row the API ended before an answer out of ``results`` into
    ``<results>.api_failures.jsonl``, so a resumed sweep runs those triples
    once more. A triple already moved once is not moved again: its re-run
    stands, and counts as invalid if the API ended it too."""
    moved_path = results.with_suffix(".api_failures.jsonl")
    before = set()
    if moved_path.exists():
        before = {(r["arm"], r["qid"], r["rep"])
                  for r in map(json.loads, moved_path.read_text().splitlines())}  # fmt: skip
    rows = [json.loads(ln) for ln in results.read_text().splitlines()]
    keep, moved = [], []
    for r in rows:
        t = (r["arm"], r["qid"], r["rep"])
        (moved if r.get("api_failure") and t not in before else keep).append(r)
    results.write_text("".join(json.dumps(r) + "\n" for r in keep))
    with moved_path.open("a") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in moved)
    by_arm: dict[str, int] = {}
    for r in moved:
        by_arm[r["arm"]] = by_arm.get(r["arm"], 0) + 1
    print(f"moved {len(moved)} rows to {moved_path.name}: {by_arm}; resume to re-run them once")


def rescore(results: Path, by_id: dict[str, dict[str, Any]]) -> None:
    """Score each stored reply again, so a truth or scorer fix reaches runs
    made before it; prints every verdict that changed."""
    rows = [json.loads(ln) for ln in results.read_text().splitlines()]
    for r in rows:
        new = score(by_id[r["qid"]], r["reply"])
        if new["verdict"] != r["verdict"] or new["unknown_host"] != r["unknown_host"]:
            print(f"{r['arm']:4} {r['qid']} rep{r['rep']} {r['verdict']} -> {new['verdict']}"
                  f" ({new['value']!r})")  # fmt: skip
        r.update(new)
    results.write_text("".join(json.dumps(r) + "\n" for r in rows))


_ORIGIN = (
    "import importlib.metadata as m, json; d = m.distribution('jobd'); "
    "u = d.read_text('direct_url.json'); "
    "print(json.dumps({'jobd': d.version, "
    "'commit': json.loads(u).get('vcs_info', {}).get('commit_id') if u else None}))"
)


def prewarm(o: Opts | None = None) -> dict[str, Any]:
    """Install both arms' packages into uv's cache before the clock starts, so
    the first run of each arm is not charged for a download (or timed out
    waiting for the MCP server to start), and check each is what the README
    names. Returns what was found, for versions.json."""
    o = o or Opts(ARTIFACTS / "runs", SERVER, None)
    server_py = [*o.server[:-1], "python", "-c"]
    subprocess.run([*server_py, "import jobd.mcp.server"], check=True, capture_output=True,
                   timeout=900)  # fmt: skip
    got = subprocess.run([*o.cli, "--version"], check=True, capture_output=True, text=True,
                         timeout=600).stdout  # fmt: skip
    if VERSION not in got:
        sys.exit(f"the pinned CLI reports {got.strip()!r}, not {VERSION}")
    origin = json.loads(
        subprocess.run(
            [*server_py, _ORIGIN], check=True, capture_output=True, text=True, timeout=600
        )
        .stdout.strip()
        .splitlines()[-1]  # fmt: skip
    )
    commit = check_server_spec(o.spec)
    if commit is None and origin["jobd"] != VERSION:
        sys.exit(f"the pinned MCP server is jobd {origin['jobd']}, not {VERSION}")
    if commit is not None and origin["commit"] != commit:
        sys.exit(f"the mcp arm's jobd came from {origin['commit']}, not {commit}")
    return {"cli": got.strip(), "mcp": origin, "server_spec": o.spec,
            "exclude_newer": o.exclude_newer}  # fmt: skip


def oracle_check(questions: list[dict[str, Any]]) -> None:
    """Before any run: the key's own answers must all score right and a near
    miss for each must score wrong, on THIS key. Otherwise the sweep would
    measure the scorer, not the arms."""
    bad = [q["id"] for q in questions if score(q, oracle_reply(q))["verdict"] != "right"]
    bad += [q["id"] for q in questions if score(q, wrong_reply(q))["verdict"] != "wrong"]
    if bad:
        sys.exit(f"oracle check failed on {sorted(set(bad))}: fix the key or the scorer first")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="bare,alt,mcp")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--only", default="", help="comma-separated question ids")
    ap.add_argument("--key", default=str(ARTIFACTS / "questions.json"))
    ap.add_argument("--model", default=MODEL, help=f"claude --model (default {MODEL})")
    ap.add_argument("--runs", default=None, help="transcript directory (default: per model)")
    ap.add_argument("--results", default=None, help="results file (default: per model)")
    ap.add_argument(
        "--accept-broker",
        default=None,
        help="the one live broker version to accept for a key built on another; needs zero drift",
    )
    ap.add_argument(
        "--stop-at",
        type=float,
        default=STOP_AT,
        help=f"seven-day utilization that stops the sweep (default {STOP_AT})",
    )
    ap.add_argument(
        "--server-spec",
        default=SERVER,
        help="mcp arm's jobd: the PyPI pin, or git+https://github.com/musharna/jobd@<sha>",
    )
    ap.add_argument(
        "--exclude-newer",
        default=None,
        help="uv --exclude-newer for both arms' dependencies (ISO date)",
    )
    ap.add_argument("--allow-drift", action="store_true", help="run even if the key drifted")
    ap.add_argument(
        "--rescore",
        action="store_true",
        help="re-score every stored reply against the current questions.json, run nothing",
    )
    ap.add_argument(
        "--drop-api-failures",
        action="store_true",
        help="move rows the API ended before an answer aside (once), run nothing",
    )
    a = ap.parse_args()  # fmt: skip
    set_model(a.model)
    set_stop_at(a.stop_at)
    default_runs, default_results = model_paths(a.model)
    a.runs = a.runs or str(default_runs)
    a.results = a.results or str(default_results)
    key = json.loads(Path(a.key).read_text())
    questions = key["questions"]
    oracle_check(questions)
    results = Path(a.results)
    if a.rescore:
        rescore(results, {q["id"]: q for q in questions})
        return
    if a.drop_api_failures:
        drop_api_failures(results)
        return
    check_server_spec(a.server_spec)
    upstream, token = os.environ.get("JOBD_URL"), os.environ.get("JOBD_API_TOKEN", "")
    if not upstream:
        sys.exit("set JOBD_URL (and JOBD_API_TOKEN) to the broker the key was built from")
    if a.only:
        keep = set(a.only.split(","))
        questions = [q for q in questions if q["id"] in keep]
    broker = Broker(upstream, token)
    live_version = broker.get("/health").get("version")
    # The answers depend on the broker's semantics (routing rule, what a
    # terminal state is), so a broker upgrade invalidates the key unless
    # --accept-broker names it.
    why = check_broker(live_version, key["broker_version"], a.accept_broker)
    if why:
        sys.exit(why)
    check = build_hard.drift if key.get("version") == build_hard.VERSION else drift
    problems = check(broker, {**key, "questions": questions})
    if problems:
        print("the broker no longer matches the key:\n  " + "\n  ".join(problems))
        if live_version != key["broker_version"]:
            sys.exit("--accept-broker needs a key with no drift")
        if not a.allow_drift:
            sys.exit(2)
    o = Opts(Path(a.runs), a.server_spec, a.exclude_newer)
    versions = prewarm(o)
    versions |= {"broker": live_version, "key": a.key, "key_built": key["built"],
                 "key_broker": key["broker_version"], "model": MODEL,
                 "stop_at": STOP_AT}  # fmt: skip
    vfile = results.with_name("versions.json")
    if vfile.exists():
        old = json.loads(vfile.read_text())
        same = ("server_spec", "mcp", "cli", "exclude_newer", "broker", "model")
        if {k: old.get(k) for k in same} != {k: versions.get(k) for k in same}:
            sys.exit(f"{vfile} records other versions than this run would use: {old}")
    results.parent.mkdir(parents=True, exist_ok=True)
    vfile.write_text(json.dumps(versions, indent=1) + "\n")
    done = set()
    if results.exists():
        for ln in results.read_text().splitlines():
            r = json.loads(ln)
            done.add((r["arm"], r["qid"], r["rep"]))
    todo = [
        (arm, q, rep)
        for rep in range(1, a.reps + 1)
        for q in questions
        for arm in a.arms.split(",")
        if (arm, q["id"], rep) not in done
    ]
    print(f"{len(todo)} runs to do ({len(done)} already in {results.name})", flush=True)
    gate = Gate(results.with_name("pauses.jsonl"))

    def task(arm: str, q: dict[str, Any], rep: int) -> dict[str, Any]:
        # Inside the worker, so the next run cannot start before this run's
        # reading has been applied.
        gate.wait()
        r = run_one(arm, q, rep, upstream, token, o)
        gate.update(r["rate_limit"], f"{arm} {q['id']} rep{rep}")
        return r

    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures = [pool.submit(task, *t) for t in todo]
        for n, f in enumerate(as_completed(futures), 1):
            try:
                r = f.result()
            except (ProxyBypassed, UsageStop, WrongModel) as e:
                pool.shutdown(wait=True, cancel_futures=True)
                sys.exit(f"STOPPED: {e}")
            with lock, results.open("a") as fh:
                fh.write(json.dumps(r) + "\n")
            rl = r["rate_limit"] or {}
            print(
                f"[{n}/{len(todo)}] {r['arm']:4} {r['qid']} rep{r['rep']} {r['verdict']:9}"
                f" exit={r['exit']} {r['seconds']}s calls={len(r['tool_calls'])}"
                f" broker={r['broker_requests']} refused={len(r['refused_requests'])}"
                f" ${r['cost_usd']} 5h={rl.get('five_hour')} 7d={rl.get('seven_day')}"
                f"{' API:' + r['api_failure'] if r['api_failure'] else ''}",
                flush=True,
            )
    sys.exit(0)


if __name__ == "__main__":
    main()
