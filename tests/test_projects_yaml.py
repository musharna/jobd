"""Tests for projects.yaml defaults: load + persist round-trip.

Covers docs/projects-yaml.md §7 test list 1-6. The
``test_persist_projects_round_trip`` test is the critical regression for the
silent-defaults-erase bug: any change to ``_persist_projects`` MUST keep this
test green.
"""

from __future__ import annotations

import pytest
import yaml

from jobd.app import _persist_projects
from jobd.config import ProjectDefaults, ProjectEntry, load_projects


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


def test_persist_projects_round_trip(tmp_path):
    """Critical regression: writing a ProjectEntry with defaults, reloading,
    must preserve the defaults block intact. Pre-fix, the persist path emitted
    only ``{priority: ...}`` and any nudge silently erased every defaults
    block."""
    path = _full_projects_yaml(tmp_path)
    projects = load_projects(path)
    state = {"projects": projects, "paths": {"projects": path}}

    # Mutate priority via the same path that ``set_project_priority`` /
    # ``nudge_project_priority`` use.
    state["projects"]["project-c"].priority = 65
    _persist_projects(state)

    reloaded = load_projects(path)
    agri = reloaded["project-c"]
    assert agri.priority == 65
    # The whole defaults block must survive.
    assert agri.defaults.max_wall_s == 14400
    assert agri.defaults.idle_timeout_s == 1800
    assert agri.defaults.host_pin == "desktop"
    assert agri.defaults.preemptible is True
    assert agri.defaults.requires is not None
    assert agri.defaults.requires.gpu is True
    assert agri.defaults.requires.needs == ["cuda"]
