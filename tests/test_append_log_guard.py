"""POST /jobs/{job_id}/log must validate the job exists and cap body size.

Before the fix, append_log appended any-sized body to logs_dir/<id>.log with
no DB existence check (unlike get_output, which 404s) and no size limit.
"""

import pytest
from fastapi.testclient import TestClient

from jobd.app import MAX_LOG_CHUNK_BYTES, build_app


@pytest.fixture
def client(tmp_path, sample_projects_yaml, sample_profiles_yaml, sample_classifier_yaml):
    app = build_app(
        db_url=f"sqlite:///{tmp_path}/jobd.db",
        projects_path=sample_projects_yaml,
        profiles_path=sample_profiles_yaml,
        classifier_path=sample_classifier_yaml,
        logs_path=tmp_path / "logs",
    )
    return TestClient(app)


def _submit(client):
    return client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"},
    )


def test_append_log_404_on_unknown_job(client, tmp_path):
    r = client.post("/jobs/99999/log", content=b"oops\n")
    assert r.status_code == 404
    assert not (tmp_path / "logs" / "99999.log").exists()


def test_append_log_rejects_oversized_body(client):
    job = _submit(client).json()
    body = b"x" * (MAX_LOG_CHUNK_BYTES + 1)
    r = client.post(f"/jobs/{job['id']}/log", content=body)
    assert r.status_code == 413


def test_append_log_normal_append_works(client):
    job = _submit(client).json()
    r = client.post(f"/jobs/{job['id']}/log", content=b"hello\n")
    assert r.status_code == 200
    assert r.json()["bytes"] == 6
    out = client.get(f"/jobs/{job['id']}/output").json()
    assert out["tail"] == "hello\n"
