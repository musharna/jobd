"""Tests for projects.yaml defaults: load + persist round-trip.

Covers docs/projects-yaml.md §7 test list 1-6. The
``test_persist_projects_round_trip`` test is the critical regression for the
silent-defaults-erase bug: any change to ``_persist_projects`` MUST keep this
test green.
"""

from __future__ import annotations

import pytest
import yaml

from jobd.broker.projects import _persist_projects
from jobd.config import (
    ProjectDefaults,
    ProjectEntry,
    load_effective_projects,
    load_projects,
)


def _full_projects_yaml(tmp_path):
    path = tmp_path / "projects.yaml"
    path.write_text(
        """projects:
  project-c:
    priority: 55
    defaults:
      max_wall_s: 14400
      idle_timeout_s: 1800
      host_pin: desktop
      requires:
        gpu: true
        needs: [cuda]
      preemptible: true
      escalate_to_arc: false
  bare-project:
    priority: 60
  _default:
    priority: 40
"""
    )
    return path


def test_load_projects_full_schema(tmp_path):
    """All keys under ``defaults`` parse into the ProjectDefaults dataclass."""
    path = _full_projects_yaml(tmp_path)
    projects = load_projects(path)
    agri = projects["project-c"]
    assert isinstance(agri, ProjectEntry)
    assert agri.priority == 55
    d = agri.defaults
    assert d.max_wall_s == 14400
    assert d.idle_timeout_s == 1800
    assert d.host_pin == "desktop"
    assert d.preemptible is True
    assert d.escalate_to_arc is False
    assert d.requires is not None
    assert d.requires.gpu is True
    assert d.requires.needs == ["cuda"]


def test_load_projects_missing_defaults_block(tmp_path):
    """Entry with only ``priority`` gets a zero-valued ProjectDefaults."""
    path = _full_projects_yaml(tmp_path)
    projects = load_projects(path)
    bare = projects["bare-project"]
    assert bare.priority == 60
    assert bare.defaults == ProjectDefaults()


def test_load_projects_missing_file(tmp_path):
    """Missing projects.yaml returns ``{_default: ProjectEntry(priority=40)}``."""
    path = tmp_path / "does-not-exist.yaml"
    projects = load_projects(path)
    assert "_default" in projects
    assert projects["_default"].priority == 40
    assert projects["_default"].defaults == ProjectDefaults()


def test_load_projects_malformed_yaml(tmp_path):
    """Corrupt YAML raises yaml.YAMLError, not silent empty dict."""
    path = tmp_path / "broken.yaml"
    path.write_text("projects:\n  project-c:\n    priority: [unclosed\n")
    with pytest.raises(yaml.YAMLError):
        load_projects(path)


def test_load_projects_unknown_keys_in_defaults(tmp_path):
    """Unrecognized keys under ``defaults`` are silently dropped."""
    path = tmp_path / "projects.yaml"
    path.write_text(
        """projects:
  project-c:
    priority: 55
    defaults:
      max_wall_s: 100
      future_field_we_dont_know_yet: 42
      another_unknown: hello
  _default: { priority: 40 }
"""
    )
    projects = load_projects(path)
    assert projects["project-c"].defaults.max_wall_s == 100
    # No exception, no attribute on the dataclass; just dropped.


def _state(tmp_path, base_path):
    """Build a BrokerState the way build_app does: git baseline + writable overlay."""
    overrides = tmp_path / "state" / "project-priorities.yaml"
    projects, base_priorities = load_effective_projects(base_path, overrides)
    return {
        "projects": projects,
        "base_priorities": base_priorities,
        "paths": {"projects": base_path, "project_overrides": overrides},
    }


def test_persist_writes_overlay_and_never_touches_git_config(tmp_path):
    """THE bug (audit 2026-07-12): _persist_projects used to rewrite
    projects.yaml, which is bind-mounted :ro in the container — so every
    `job projects set/nudge` raised OSError -> HTTP 500. The git baseline must
    now be left byte-identical, with the delta landing in the overlay."""
    base_path = _full_projects_yaml(tmp_path)
    before = base_path.read_text()
    state = _state(tmp_path, base_path)

    state["projects"]["project-c"].priority = 65
    _persist_projects(state)

    # The git-owned baseline is untouched.
    assert base_path.read_text() == before
    # The delta is in the overlay.
    overlay = yaml.safe_load(state["paths"]["project_overrides"].read_text())
    assert overlay == {"priorities": {"project-c": 65}}


def test_persist_survives_a_read_only_config_dir(tmp_path):
    """Real-execution check of the production condition that caused the 500:
    the config dir is genuinely read-only. Persist must still succeed."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    base_path = cfg_dir / "projects.yaml"
    base_path.write_text("projects:\n  project-c: { priority: 55 }\n  _default: { priority: 40 }\n")
    state = _state(tmp_path, base_path)

    cfg_dir.chmod(0o555)  # read-only, exactly like the :ro bind mount
    try:
        state["projects"]["project-c"].priority = 70
        _persist_projects(state)  # pre-fix: OSError: Read-only file system -> 500
        reloaded, _ = load_effective_projects(base_path, state["paths"]["project_overrides"])
        assert reloaded["project-c"].priority == 70
    finally:
        cfg_dir.chmod(0o755)


def test_persist_round_trip_preserves_defaults(tmp_path):
    """The original regression still holds — a nudge must not erase `defaults:`.
    It is now structurally impossible: defaults live only in the git baseline,
    which persist never rewrites."""
    base_path = _full_projects_yaml(tmp_path)
    state = _state(tmp_path, base_path)

    state["projects"]["project-c"].priority = 65
    _persist_projects(state)

    reloaded, _ = load_effective_projects(base_path, state["paths"]["project_overrides"])
    agri = reloaded["project-c"]
    assert agri.priority == 65  # override applied
    assert agri.defaults.max_wall_s == 14400  # whole defaults block survives
    assert agri.defaults.idle_timeout_s == 1800
    assert agri.defaults.host_pin == "desktop"
    assert agri.defaults.preemptible is True
    assert agri.defaults.requires is not None
    assert agri.defaults.requires.gpu is True
    assert agri.defaults.requires.needs == ["cuda"]


def test_overlay_holds_only_deltas_so_git_changes_still_flow(tmp_path):
    """Why the overlay stores diffs, not a full snapshot: a project nobody
    nudged must still pick up a later config-as-code priority change, while a
    nudged project keeps its runtime override."""
    base_path = _full_projects_yaml(tmp_path)
    state = _state(tmp_path, base_path)

    state["projects"]["project-c"].priority = 65  # nudged at runtime
    _persist_projects(state)
    overlay = yaml.safe_load(state["paths"]["project_overrides"].read_text())
    assert "bare-project" not in overlay["priorities"]  # untouched -> not pinned

    # Now `git pull` raises both priorities in the baseline.
    base_path.write_text(
        "projects:\n"
        "  project-c: { priority: 99 }\n"
        "  bare-project: { priority: 99 }\n"
        "  _default: { priority: 40 }\n"
    )
    reloaded, _ = load_effective_projects(base_path, state["paths"]["project_overrides"])
    assert reloaded["bare-project"].priority == 99  # git change lands
    assert reloaded["project-c"].priority == 65  # runtime override still wins


def test_overlay_can_materialize_a_project_absent_from_git(tmp_path):
    """`job projects set brand-new 70` on a project not in projects.yaml."""
    base_path = _full_projects_yaml(tmp_path)
    state = _state(tmp_path, base_path)

    state["projects"]["brand-new"] = ProjectEntry(priority=70)
    _persist_projects(state)

    reloaded, _ = load_effective_projects(base_path, state["paths"]["project_overrides"])
    assert reloaded["brand-new"].priority == 70


def test_priority_endpoints_work_against_a_read_only_config_dir(tmp_path):
    """End-to-end regression for the live 500 (audit 2026-07-12).

    Reproduces production exactly: config/ is read-only (the `:ro` bind mount),
    the DB lives in a writable data dir. Pre-fix, `POST /projects/{name}/nudge`
    returned HTTP 500 (`OSError: Read-only file system`) — verified against the
    running gt76 broker. Both mutation endpoints must now return 200 and survive
    a reload.
    """
    from fastapi.testclient import TestClient

    from jobd.app import build_app

    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "projects.yaml").write_text(
        "projects:\n  project-a: { priority: 80 }\n  _default: { priority: 40 }\n"
    )
    (cfg / "profiles.yaml").write_text("profiles: {}\n")
    (cfg / "classifier.yaml").write_text("rules: []\n")
    data = tmp_path / "data"
    data.mkdir()

    app = build_app(
        db_url=f"sqlite:///{data}/jobd.db",
        projects_path=cfg / "projects.yaml",
        profiles_path=cfg / "profiles.yaml",
        classifier_path=cfg / "classifier.yaml",
        logs_path=tmp_path / "logs",
    )
    client = TestClient(app)

    cfg.chmod(0o555)  # the :ro mount
    try:
        r = client.post("/projects/project-a/nudge", json={"delta": -10})
        assert r.status_code == 200, r.text  # was 500
        assert r.json()["project-a"]["priority"] == 70

        r = client.post("/projects/project-d", json={"priority": 55})
        assert r.status_code == 200, r.text
        assert r.json()["project-d"]["priority"] == 55

        # Overrides survive a config reload (and the baseline is still readable).
        assert client.post("/reload").status_code == 200
        got = client.get("/projects").json()
        assert got["project-a"]["priority"] == 70
        assert got["project-d"]["priority"] == 55
    finally:
        cfg.chmod(0o755)


# --- defaults validation (audit 2026-07-15 F3) ------------------------------
#
# YAML values bypass Pydantic. An unvalidated `max_wall_s: 0` splits
# broker/worker semantics: the worker reads it as unset (`or`-falsy) and never
# enforces, the sweeper reads it as set (`is not None`) and orphans the healthy
# job as wall_clock_exceeded — which is not resurrectable. A STRING value
# TypeErrors the whole sweep pass every 30s. Both must die at parse time.


def _defaults_yaml(tmp_path, defaults_body: str):
    path = tmp_path / "projects.yaml"
    path.write_text(
        f"""
projects:
  p:
    priority: 50
    defaults:
{defaults_body}
  _default:
    priority: 40
"""
    )
    return load_projects(path)["p"].defaults


def test_zero_wall_clock_is_dropped_not_half_enforced(tmp_path, caplog):
    d = _defaults_yaml(tmp_path, "      max_wall_s: 0")
    assert d.max_wall_s is None, (
        "max_wall_s=0 must not reach Job rows: the worker treats 0 as unset but the "
        "sweeper treats it as set, and orphans a healthy running job as wall_clock_exceeded"
    )
    assert any("max_wall_s" in r.message for r in caplog.records), "the drop must be loud"


def test_a_string_duration_is_dropped_before_it_can_kill_the_sweep(tmp_path):
    d = _defaults_yaml(tmp_path, '      max_wall_s: "8h"')
    assert d.max_wall_s is None, (
        "a string max_wall_s flows into Job.max_wall_s and the sweeper's arithmetic "
        "raises TypeError — aborting the ENTIRE sweep pass (timeouts, reclaims, "
        "retention) every 30 seconds"
    )


def test_out_of_bounds_values_are_dropped(tmp_path):
    d = _defaults_yaml(
        tmp_path,
        "      max_wall_s: 999999999\n      idle_timeout_s: -5\n      checkpoint_grace_s: 301\n      priority: 150",
    )
    assert d.max_wall_s is None
    assert d.idle_timeout_s is None
    assert d.checkpoint_grace_s is None
    assert d.priority is None


def test_a_boolean_is_not_an_int_here(tmp_path):
    # YAML `max_wall_s: true` is a Python bool, which IS an int subclass —
    # without the explicit bool check it would land as max_wall_s=1.
    d = _defaults_yaml(tmp_path, "      max_wall_s: true")
    assert d.max_wall_s is None


def test_mistyped_flag_fields_are_dropped(tmp_path):
    d = _defaults_yaml(tmp_path, '      preemptible: "yes please"\n      host_pin: 7')
    assert d.preemptible is None
    assert d.host_pin is None


def test_valid_defaults_still_parse_bounds_inclusive(tmp_path):
    d = _defaults_yaml(
        tmp_path,
        "      max_wall_s: 604800\n      idle_timeout_s: 1\n      checkpoint_grace_s: 300\n      priority: 0",
    )
    assert d.max_wall_s == 604800
    assert d.idle_timeout_s == 1
    assert d.checkpoint_grace_s == 300
    assert d.priority == 0
