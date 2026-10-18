# Concurrency (multislotting)

By default each worker runs **one job at a time** (`JOBD_WORKER_MAX_CONCURRENT_JOBS=1`). Raise it to let a worker bin-pack several jobs that fit side by side:

```bash
JOBD_WORKER_MAX_CONCURRENT_JOBS=3 jobd-worker
```

The matcher is resource-aware, so this is **not** blind N-up oversubscription. Each in-flight job reserves its `vram_gb` / `ram_gb` / `cpus` footprint, and the worker's heartbeat advertises only what's left. For VRAM, NVML's free figure already counts memory a running job has allocated, so only the not-yet-allocated part of the reservations is subtracted: `free_vram = nvml_free − max(0, Σ in-flight vram_gb − VRAM held by this worker's jobs)`, where `nvml_free` is GPU index 0 only. RAM and CPUs subtract the full reservations. The broker won't place a job that doesn't fit the remaining headroom. The practical payoff: **a CPU-only job and a GPU job run at the same time** — the CPU job reserves 0 VRAM, so it never blocks the GPU slot, and vice-versa. Two GPU jobs co-run only if both fit live VRAM: before starting a job it was handed, the worker re-reads free VRAM and refuses the job if it no longer fits. A job whose command contains the literal marker `# CONCURRENT_OK` skips that last check — use it when you know the requested VRAM is overstated.

`job workers` reports each worker's slot usage — `running` jobs out of `max_concurrent` — alongside the live resource ad:

```jsonc
// job workers
{ "host": "desktop", "state": "online", "running": 2, "max_concurrent": 3,
  "free_vram_gb": 9.1, "idle_cpus": 6, ... }
```

Set the limit per worker from its environment (systemd unit, shell, or `worker.yaml` env) — it's a worker-local knob, not a broker setting.

See also `JOBD_WORKER_MAX_CONCURRENT_JOBS` in [configuration.md](configuration.md).
