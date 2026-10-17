"""Ask every question to Claude Code under each arm, and score the replies.

Arms (same model, same prompt, isolated: no user settings, hooks or other MCP
servers, an empty working directory per run):

- ``bare``: no tools at all.
- ``alt``:  Bash, allowed to run only the ``job`` CLI (jobd 0.5.51 from PyPI,
  behind ``job_guard.py``) and a few read-only text tools to filter its output.
  This is what a user has without the MCP server.
- ``mcp``:  jobd-mcp (jobd[mcp] 0.5.51 from PyPI via uvx) only, without
  ``jobd_cancel``, ``jobd_preempt`` and ``jobd_worker_delete``.

SAFETY. The questions are about the LIVE broker, so every arm reaches it only
through ``readonly_proxy.py``: one loopback proxy per run, which holds the
token, refuses every mutating request with a 403, and logs each decision.
``JOBD_API_TOKEN`` is removed from the arm's environment, so a request that
skips the proxy is a 401. Mutating tools are also named in
``--disallowedTools``, and the alt arm's ``job`` refuses mutating subcommands
before they run.

Before any run, ``drift`` re-reads every job and the fleet the key was built
from; a changed answer stops the sweep (``--allow-drift`` overrides).

Each finished run is appended to ``results.jsonl`` with its score; a rerun
skips (arm, question, rep) triples already there, so an interrupted sweep
resumes. Raw stream-json transcripts go to ``runs/<arm>/<qid>-<rep>.jsonl``,
proxy decisions to ``runs/<arm>/<qid>-<rep>.proxy.jsonl``.

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

from build_questions import Broker, drift
from job_guard import write_shim
from readonly_proxy import ReadOnlyProxy
from score import score

HERE = Path(__file__).resolve().parent
MODEL = "claude-opus-5-5"
VERSION = "0.5.51"  # latest jobd on PyPI, 2026-10-16
MCP_SERVER = ["uvx", "--from", f"jobd[mcp]=={VERSION}", "jobd-mcp"]
CLI = ["uvx", "--from", f"jobd=={VERSION}", "job"]
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


def arm_flags(arm: str, tmp: Path, proxy_url: str) -> list[str]:
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
        server = {
            "command": MCP_SERVER[0],
            "args": MCP_SERVER[1:],
            # The server's call log defaults to ~/.claude/state/jobd-mcp; keep
            # benchmark calls out of the user's real one.
            "env": {"JOBD_URL": proxy_url, "JOBD_MCP_LOG_DIR": str(tmp / "mcp-log")},
        }
        cfg.write_text(json.dumps({"mcpServers": {"jobd": server}}))
        return [
            "--tools", "", "--allowedTools", "mcp__jobd",
            "--disallowedTools", ",".join(f"mcp__jobd__{t}" for t in MUTATING_MCP),
            "--strict-mcp-config", "--mcp-config", str(cfg),
        ]  # fmt: skip
    raise ValueError(arm)


def arm_env(arm: str, tmp: Path, proxy_url: str) -> dict[str, str]:
    """The parent environment minus the broker token, pointed at the proxy."""
    env = {k: v for k, v in os.environ.items() if k != "JOBD_API_TOKEN"}
    env["JOBD_URL"] = proxy_url
    if arm == "alt":
        bindir = write_shim(tmp / "bin").parent
        env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
        env["JOBD_BENCH_REAL_JOB"] = json.dumps(CLI)
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


def run_one(arm: str, q: dict[str, Any], rep: int, upstream: str, token: str) -> dict[str, Any]:
    out = HERE / "runs" / arm / f"{q['id']}-{rep}.jsonl"
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
            *arm_flags(arm, tmp, proxy.url),
        ]  # fmt: skip
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=work,
                env=arm_env(arm, tmp, proxy.url),
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
    reply = result.get("result") or ""
    return {
        "arm": arm,
        "qid": q["id"],
        "category": q["category"],
        "rep": rep,
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
        **score(q, reply),
    }


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


def prewarm() -> None:
    """Install the pinned release into uv's cache before the clock starts, so
    the first run of each arm is not charged for a download (or timed out
    waiting for the MCP server to start)."""
    for argv in (
        ["uvx", "--from", f"jobd[mcp]=={VERSION}", "python", "-c", "import jobd.mcp.server"],
        [*CLI, "--version"],
    ):
        subprocess.run(argv, check=True, capture_output=True, timeout=600)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="bare,alt,mcp")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--only", default="", help="comma-separated question ids")
    ap.add_argument("--results", default=str(HERE / "results.jsonl"))
    ap.add_argument("--allow-drift", action="store_true", help="run even if the key drifted")
    ap.add_argument(
        "--rescore",
        action="store_true",
        help="re-score every stored reply against the current questions.json, run nothing",
    )
    a = ap.parse_args()
    key = json.loads((HERE / "questions.json").read_text())
    questions = key["questions"]
    if a.rescore:
        rescore(Path(a.results), {q["id"]: q for q in questions})
        return
    upstream, token = os.environ.get("JOBD_URL"), os.environ.get("JOBD_API_TOKEN", "")
    if not upstream:
        sys.exit("set JOBD_URL (and JOBD_API_TOKEN) to the broker the key was built from")
    if a.only:
        keep = set(a.only.split(","))
        questions = [q for q in questions if q["id"] in keep]
    problems = drift(Broker(upstream, token), {**key, "questions": questions})
    if problems:
        print("the broker no longer matches questions.json:\n  " + "\n  ".join(problems))
        if not a.allow_drift:
            sys.exit(2)
    prewarm()
    results = Path(a.results)
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
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures = [pool.submit(run_one, *t, upstream, token) for t in todo]
        for n, f in enumerate(as_completed(futures), 1):
            try:
                r = f.result()
            except ProxyBypassed as e:
                pool.shutdown(wait=True, cancel_futures=True)
                sys.exit(f"STOPPED: {e}")
            with lock, results.open("a") as fh:
                fh.write(json.dumps(r) + "\n")
            print(
                f"[{n}/{len(todo)}] {r['arm']:4} {r['qid']} rep{r['rep']} {r['verdict']:9}"
                f" exit={r['exit']} {r['seconds']}s calls={len(r['tool_calls'])}"
                f" broker={r['broker_requests']} refused={len(r['refused_requests'])}",
                flush=True,
            )
    sys.exit(0)


if __name__ == "__main__":
    main()
