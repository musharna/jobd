# Real-execution validation for the 2026-07-01 concurrency fixes

The fast test suite drives the broker in-process (`TestClient` + in-memory
SQLite, one event loop) and _injects_ races with monkeypatch. That proves the
logic but not the behavior of the fixes that only exist **across processes**:
signals to real children, a real worker's kill-escalation timer, real HTTP
partitions. This directory holds the real-execution layer.

## Automated (self-contained)

`test_broker_concurrency_live.py` launches a **real broker process + real worker
process** over a temp DB/config and runs **real subprocess jobs**. Gated so
normal/CI runs skip it:

```bash
JOBD_LIVE=1 pytest tests/integration/test_broker_concurrency_live.py -v
```

| Test                                                         | Fix                                 | What it proves that TestClient can't                                                                                                                |
| ------------------------------------------------------------ | ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_sanity_real_job_completes`                             | —                                   | broker+worker actually dispatch and run a job                                                                                                       |
| `test_watchdog_escalates_to_sigkill_on_sigterm_ignoring_job` | **H1**                              | a real `trap '' TERM` workload is force-**KILL**ed by the escalation timer once the idle watchdog fires — not left RUNNING forever pinning its slot |
| `test_cancel_running_job_terminates_child`                   | cancel-latency / **H3** signal path | `/cancel` on a genuinely-running job terminates the real child and lands the job `cancelled` promptly                                               |
| `test_stale_worker_reports_rejected_by_live_broker`          | **M2**                              | the live broker 409s `/complete` + `/log` carrying a foreign `X-Jobd-Worker` and leaves the running job uncorrupted; the owner's report still works |

The worker is launched with `JOBD_WORKER_WATCHDOG_KILL_GRACE_S=3` (a new env knob
on the H1 fix) so the escalation test runs in seconds instead of the 60s default.

## Manual (optional) — full M2 partition sequence end-to-end

The automated `test_stale_worker_reports_rejected_by_live_broker` validates the
M2 **mechanism** directly (foreign `X-Jobd-Worker` → 409) over real HTTP. The
full production **sequence** — partition → reclaim → re-dispatch → the original
worker's stale completion arriving — needs a genuine partition, which isn't
reliably automatable. To exercise it end-to-end by hand:

1. Start a broker and **two** workers `wA`, `wB` (`JOBD_WORKER_HOST=wA` / `wB`).
   Set a short reclaim by using an **idempotent** job (`--needs idempotent`) so
   the sweeper requeues rather than orphans.
2. Submit a long idempotent job; wait until it is `running` on `wA`.
3. Partition `wA` from the broker (drop its heartbeats):
   `sudo iptables -A OUTPUT -p tcp --dport <broker-port> -m owner --uid-owner $(id -u wA) -j DROP`
   (or run `wA` in a netns and sever it, or `kill -STOP` the worker so it stops
   heartbeating — the child keeps running).
4. Wait past the reclaim window: the broker requeues the job and `wB` claims it
   (`job.worker == wB`).
5. Heal the partition. `wA`'s original run posts `/complete` (and streams `/log`).
6. **Expected (fixed):** the broker returns **409** to `wA`'s `/complete` and
   `/log` (`X-Jobd-Worker: wA` != current owner `wB`); the job's terminal state
   and log are owned by `wB`. Before M2: `wA`'s `/complete` terminal-ized the job
   under the wrong worker and its log chunks interleaved into `wB`'s run.

## Recommended gate before deploying PR #16

Run the automated harness on the broker host, then perform the manual M2
procedure once. The unit suite covers the logic; this covers the wiring.
