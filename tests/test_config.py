"""Tests for YAML config loaders."""

from jobd.config import (
    load_classifier_rules,
    load_profiles,
    load_projects,
    resolve_priority,
    resolve_profile,
)


def test_load_projects(sample_projects_yaml):
    projects = load_projects(sample_projects_yaml)
    assert projects["project-b"].priority == 80
    assert projects["project-a"].priority == 55
    assert projects["_default"].priority == 40


def test_load_profiles(sample_profiles_yaml):
    profiles = load_profiles(sample_profiles_yaml)
    assert profiles["small"].fast_path is True
    assert profiles["gpu-heavy"].vram_gb == 28
    assert profiles["gpu-heavy"].preemptible is True


def test_load_classifier_rules(sample_classifier_yaml):
    rules = load_classifier_rules(sample_classifier_yaml)
    assert len(rules) == 1
    assert rules[0].id == "sdxl-lora-train"
    assert rules[0].confidence == "high"


def test_resolve_priority_known_project(sample_projects_yaml):
    projects = load_projects(sample_projects_yaml)
    assert resolve_priority(projects, "project-b", delta=0) == 80
    assert resolve_priority(projects, "project-b", delta=5) == 85
    assert resolve_priority(projects, "project-b", delta=-15) == 65


def test_resolve_priority_unknown_falls_to_default(sample_projects_yaml):
    projects = load_projects(sample_projects_yaml)
    assert resolve_priority(projects, "unknown-project", delta=0) == 40


def test_resolve_priority_clamps(sample_projects_yaml):
    projects = load_projects(sample_projects_yaml)
    assert resolve_priority(projects, "project-b", delta=50) == 100
    assert resolve_priority(projects, "project-b", delta=-500) == 0


def test_resolve_profile(sample_profiles_yaml):
    profiles = load_profiles(sample_profiles_yaml)
    p = resolve_profile(profiles, "gpu-heavy")
    assert p.vram_gb == 28
    assert p.preemptible is True


def test_resolve_profile_unknown_returns_none(sample_profiles_yaml):
    profiles = load_profiles(sample_profiles_yaml)
    assert resolve_profile(profiles, "does-not-exist") is None
