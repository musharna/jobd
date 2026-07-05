"""Job-array broker fan-out: /submit --count N + {i} substitution."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from jobd.app import build_app
from jobd.db import Job


def _stored_env(client, jid):
    """Real (un-redacted) env as stored and delivered to the worker via
    /next-job. The observability read surfaces (GET /jobs/{id}) mask the values,
    so array-substitution assertions must check the stored row, not the GET."""
    with Session(client.app.state.engine) as s:
        return json.loads(s.get(Job, jid).env_json)


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


def _submit(client, **overrides):
    body = {"cmd": ["echo", "hi"], "cwd": "/tmp", "project": "project-a"}
    body.update(overrides)
    return client.post("/submit", json=body)


def test_count_default_is_single_job_with_null_array_fields(client):
    r = _submit(client)
    assert r.status_code == 200, r.text
    body = r.json()
    # count==1 path is unchanged: a single JobInfo, not an array summary.
    assert body["id"] > 0
    assert body["array_id"] is None
    assert body["array_index"] is None
    assert body["array_size"] is None


def test_count_one_explicit_is_still_single_job(client):
    r = _submit(client, count=1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "job_ids" not in body  # not an array response
    assert body["array_id"] is None


def test_count_n_returns_array_summary(client):
    r = _submit(client, count=3)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 3
    assert len(body["job_ids"]) == 3
    assert body["array_id"] == body["job_ids"][0]  # array_id = first member id
    assert "warnings" in body


def test_array_members_have_grouping_columns(client):
    r = _submit(client, count=4)
    body = r.json()
    array_id = body["array_id"]
    for idx, jid in enumerate(body["job_ids"]):
        m = client.get(f"/jobs/{jid}").json()
        assert m["array_id"] == array_id
        assert m["array_index"] == idx
        assert m["array_size"] == 4


def test_index_substituted_into_cmd(client):
    r = _submit(client, cmd=["python", "train.py", "--fold", "{i}"], count=3)
    body = r.json()
    folds = []
    for jid in body["job_ids"]:
        cmd = client.get(f"/jobs/{jid}").json()["cmd"]
        assert cmd[:3] == ["python", "train.py", "--fold"]
        folds.append(cmd[3])
    assert folds == ["0", "1", "2"]


def test_index_substituted_into_env(client):
    r = _submit(client, cmd=["echo", "x"], env={"FOLD": "{i}", "STATIC": "k"}, count=2)
    body = r.json()
    seen = []
    for jid in body["job_ids"]:
        # GET masks values (redaction applies to array members too) — keys only.
        assert client.get(f"/jobs/{jid}").json()["env"] == {"FOLD": "***", "STATIC": "***"}
        stored = _stored_env(client, jid)
        assert stored["STATIC"] == "k"
        seen.append(stored["FOLD"])
    assert seen == ["0", "1"]


def test_literal_braces_in_arg_survive_array_submit(client):
    # JSON literal arg must not be mangled by substitution (the .replace vs
    # str.format guarantee), while {i} inside it is still replaced.
    r = _submit(client, cmd=["run", '--cfg={"lr":0.1,"fold":{i}}'], count=2)
    body = r.json()
    cfgs = [client.get(f"/jobs/{jid}").json()["cmd"][1] for jid in body["job_ids"]]
    assert cfgs == ['--cfg={"lr":0.1,"fold":0}', '--cfg={"lr":0.1,"fold":1}']


def test_count_zero_rejected(client):
    assert _submit(client, count=0).status_code == 422


def test_count_over_cap_rejected(client):
    assert _submit(client, count=1001).status_code == 422


def test_dry_run_reports_array_count_and_inserts_nothing(client):
    before = len(client.get("/jobs").json())
    r = _submit(client, count=5, dry_run=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "dry-run"
    assert body["array_count"] == 5
    after = len(client.get("/jobs").json())
    assert after == before  # dry-run inserts no rows


def test_array_members_are_independent_queued_jobs(client):
    r = _submit(client, count=3)
    for jid in r.json()["job_ids"]:
        assert client.get(f"/jobs/{jid}").json()["state"] == "queued"


# --- parameter sweeps (--sweep) ------------------------------------------------


def test_single_axis_sweep_fans_out_one_member_per_value(client):
    r = _submit(
        client,
        cmd=["t.py", "--lr", "{lr}"],
        sweep=[{"key": "lr", "values": ["0.1", "0.01", "0.001"]}],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 3
    lrs = [client.get(f"/jobs/{jid}").json()["cmd"][2] for jid in body["job_ids"]]
    assert lrs == ["0.1", "0.01", "0.001"]


def test_multi_axis_sweep_is_cartesian_product(client):
    r = _submit(
        client,
        cmd=["t.py", "--lr", "{lr}", "--seed", "{seed}"],
        sweep=[
            {"key": "lr", "values": ["0.1", "0.01"]},
            {"key": "seed", "values": ["1", "2", "3"]},
        ],
    )
    body = r.json()
    assert body["count"] == 6
    pairs = []
    for jid in body["job_ids"]:
        cmd = client.get(f"/jobs/{jid}").json()["cmd"]
        pairs.append((cmd[2], cmd[4]))
    # odometer: lr outer, seed inner (last axis varies fastest)
    assert pairs == [
        ("0.1", "1"),
        ("0.1", "2"),
        ("0.1", "3"),
        ("0.01", "1"),
        ("0.01", "2"),
        ("0.01", "3"),
    ]


def test_sweep_index_available_alongside_named_keys(client):
    r = _submit(
        client,
        cmd=["t.py", "--lr", "{lr}", "--out", "run-{i}"],
        sweep=[{"key": "lr", "values": ["0.1", "0.01"]}],
    )
    body = r.json()
    outs = [client.get(f"/jobs/{jid}").json()["cmd"][4] for jid in body["job_ids"]]
    assert outs == ["run-0", "run-1"]


def test_sweep_members_carry_array_grouping(client):
    r = _submit(client, cmd=["echo", "{k}"], sweep=[{"key": "k", "values": ["a", "b", "c"]}])
    body = r.json()
    array_id = body["array_id"]
    assert array_id == body["job_ids"][0]
    for idx, jid in enumerate(body["job_ids"]):
        m = client.get(f"/jobs/{jid}").json()
        assert m["array_id"] == array_id
        assert m["array_index"] == idx
        assert m["array_size"] == 3


def test_sweep_substitutes_into_env(client):
    r = _submit(
        client,
        cmd=["echo", "x"],
        env={"LR": "{lr}", "STATIC": "k"},
        sweep=[{"key": "lr", "values": ["0.1", "0.01"]}],
    )
    body = r.json()
    seen = []
    for jid in body["job_ids"]:
        # GET masks values; the swept value is verified against the stored row.
        assert client.get(f"/jobs/{jid}").json()["env"] == {"LR": "***", "STATIC": "***"}
        stored = _stored_env(client, jid)
        assert stored["STATIC"] == "k"
        seen.append(stored["LR"])
    assert seen == ["0.1", "0.01"]


def test_sweep_and_count_are_mutually_exclusive(client):
    r = _submit(client, count=2, sweep=[{"key": "lr", "values": ["0.1"]}])
    assert r.status_code == 422


def test_sweep_reserved_index_key_rejected(client):
    r = _submit(client, sweep=[{"key": "i", "values": ["0", "1"]}])
    assert r.status_code == 422


def test_sweep_over_cap_rejected(client):
    r = _submit(
        client,
        sweep=[
            {"key": "a", "values": [str(x) for x in range(40)]},
            {"key": "b", "values": [str(x) for x in range(40)]},
        ],
    )
    assert r.status_code == 422


def test_sweep_dry_run_reports_product_count_and_inserts_nothing(client):
    before = len(client.get("/jobs").json())
    r = _submit(
        client,
        sweep=[
            {"key": "lr", "values": ["0.1", "0.01"]},
            {"key": "seed", "values": ["1", "2", "3"]},
        ],
        dry_run=True,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "dry-run"
    assert body["array_count"] == 6
    assert len(client.get("/jobs").json()) == before
