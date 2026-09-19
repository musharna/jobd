"""The suite must never name scopes in a real broker's namespace.

Several tests start real `systemd-run --user` scopes through `run_job`. A
worker's startup sweep kills every scope in its own namespace, and the
namespace comes from the broker URL. A developer with JOBD_URL exported would
otherwise run these tests inside their production worker's namespace, where a
restarting worker would sweep the test's scope, and where a test scope is
indistinguishable from a job.
"""

import os

import jobd.worker.job_worker as job_worker


def test_the_suite_runs_in_a_namespace_no_real_broker_has():
    real = {job_worker.scope_namespace(job_worker._DEFAULT_JOBD_URL)}
    if os.environ.get("JOBD_URL"):
        real.add(job_worker.scope_namespace(os.environ["JOBD_URL"]))
    assert job_worker._scope_ns not in real
    # Positive control: the binding is the mechanism, so it must still work.
    assert job_worker.scope_unit_name(7) == f"jobd-{job_worker._scope_ns}-7.scope"
