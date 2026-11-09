"""The eligibility views must agree with dispatch on the GPU opt-out.

v0.5.56 taught `fits_on_worker` that a worker set to `gpu: false` takes no VRAM
request and no `any-gpu` pin. The views that explain a queue did not learn it.
`eligible_workers` and `submit_preflight` only see `(requires, host_pin)`, so
for job 9030 (2026-11-09: `--host laptop --vram-required 1`, SCAR18 opted out)
the submit said "will queue behind job 9017 on laptop" and the sweeper said
"laptop is holding the only eligible worker", while the dispatcher passed
the job over on every poll. A job that can never run was reported as merely
waiting.
"""

from types import SimpleNamespace

from jobd.matcher import (
    WorkerSnapshot,
    eligible_workers,
    selectors_only_match,
    submit_preflight,
)


def _w(host: str, *, gpu: bool) -> WorkerSnapshot:
    return WorkerSnapshot(
        host=host,
        host_aliases=["any", "any-gpu"],
        free_vram_gb=23.56,
        unregistered_vram_gb=0.0,
        free_ram_gb=40.0,
        idle_cpus=20,
        arch="x86_64",
        os="linux",
        gpu=gpu,
        tags=[],
    )


LAPTOP = _w("laptop", gpu=False)  # opted out
GT76 = _w("gt76", gpu=True)


def _hosts(ws):
    return [w.host for w in ws]


def test_eligible_workers_drops_an_opted_out_host_for_gpu_requests():
    assert _hosts(eligible_workers(None, "any", [LAPTOP, GT76], vram_gb=1.0)) == ["gt76"]
    assert _hosts(eligible_workers(None, "any-gpu", [LAPTOP, GT76], vram_gb=0.0)) == ["gt76"]
    assert eligible_workers(None, "laptop", [LAPTOP, GT76], vram_gb=1.0) == []
    # Control: CPU work still sees both hosts, and a pin to the laptop still fits.
    assert _hosts(eligible_workers(None, "any", [LAPTOP, GT76], vram_gb=0.0)) == [
        "laptop",
        "gt76",
    ]
    assert _hosts(eligible_workers(None, "laptop", [LAPTOP, GT76], vram_gb=0.0)) == ["laptop"]


def test_preflight_flags_a_gpu_request_pinned_to_an_opted_out_host():
    msg = submit_preflight(None, "laptop", [LAPTOP, GT76], vram_gb=1.0)
    assert msg is not None and "gpu: false" in msg, msg
    # Controls: CPU work pinned there, and GPU work left to `any`, stay silent.
    assert submit_preflight(None, "laptop", [LAPTOP, GT76], vram_gb=0.0) is None
    assert submit_preflight(None, "any", [LAPTOP, GT76], vram_gb=1.0) is None


def test_selectors_only_match_counts_the_opt_out_as_a_capability():
    vram_job = SimpleNamespace(requires=None, vram_gb=1.0, host_pin="any")
    cpu_job = SimpleNamespace(requires=None, vram_gb=0.0, host_pin="any")
    assert not selectors_only_match(vram_job, LAPTOP)
    assert selectors_only_match(vram_job, GT76)
    assert selectors_only_match(cpu_job, LAPTOP)


# ---- through the real submit endpoint ------------------------------------


def _heartbeat(client, host: str, *, gpu: bool, free_vram: float):
    client.post(
        "/heartbeat",
        json={
            "host": host,
            "free_vram_gb": free_vram,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 40,
            "idle_cpus": 20,
            "arch": "x86_64",
            "os": "linux",
            "gpu": gpu,
            "tags": [],
            "host_aliases": ["any", "any-gpu"],
        },
    )


def test_submit_does_not_say_queued_behind_for_a_job_that_cannot_run_there(client):
    _heartbeat(client, "laptop", gpu=False, free_vram=23.56)
    _heartbeat(client, "gt76", gpu=True, free_vram=5.5)
    busy = client.post(
        "/submit",
        json={"cmd": ["sleep", "60"], "cwd": "/tmp", "project": "project-a", "host_pin": "laptop"},
    ).json()
    claimed = client.post(
        "/next-job",
        json={
            "host": "laptop",
            "free_vram_gb": 23.56,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 40,
            "idle_cpus": 20,
        },
    ).json()
    assert claimed["id"] == busy["id"]

    vram = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
            "host_pin": "laptop",
            "vram_gb": 1.0,
        },
    ).json()
    assert "will queue behind" not in (vram["warning"] or ""), vram["warning"]
    assert "gpu: false" in (vram["warning"] or ""), vram["warning"]

    # Control: a CPU job pinned to the same busy host really does queue behind it.
    cpu = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a", "host_pin": "laptop"},
    ).json()
    assert "will queue behind" in (cpu["warning"] or ""), cpu["warning"]
    assert "gpu: false" not in (cpu["warning"] or ""), cpu["warning"]
