"""Shared fixtures for tests/unit/."""

from __future__ import annotations

import pytest


@pytest.fixture
def logs_dir(tmp_path):
    return tmp_path / "logs"
