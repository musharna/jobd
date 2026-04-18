"""Shared pytest fixtures for jobd tests."""
import pytest


@pytest.fixture
def tmp_db_url(tmp_path):
    """Give each test an isolated SQLite file."""
    return f"sqlite:///{tmp_path}/jobd.db"


@pytest.fixture
def sample_profiles_yaml(tmp_path):
    path = tmp_path / "profiles.yaml"
    path.write_text("""profiles:
  small:
    vram_gb: 0
    ram_gb: 2
    cpus: 2
    expected_runtime: 10m
    preemptible: true
    host_hint: any
    fast_path: true
  gpu-heavy:
    vram_gb: 28
    ram_gb: 22
    cpus: 8
    expected_runtime: 8h
    preemptible: true
    host_hint: desktop
""")
    return path


@pytest.fixture
def sample_projects_yaml(tmp_path):
    path = tmp_path / "projects.yaml"
    path.write_text("""projects:
  phelipanche: { priority: 80 }
  orchid-sdxl: { priority: 55 }
  _default: { priority: 40 }
""")
    return path


@pytest.fixture
def sample_classifier_yaml(tmp_path):
    path = tmp_path / "classifier.yaml"
    path.write_text("""rules:
  - id: sdxl-lora-train
    match:
      - command_regex: "^(bash\\\\s+)?train_lora_v\\\\d+\\\\.sh\\\\b"
    suggest_profile: gpu-heavy
    confidence: high
""")
    return path
