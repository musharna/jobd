"""server.json version identity.

jobd's ``__version__`` is derived from ``importlib.metadata.version("jobd")``, so it
cannot drift from the installed distribution — no test needed for that. ``CITATION.cff``
is covered by the ``citation`` workflow. ``server.json`` was the remaining gap: it records
the version in three places and nothing compared any of them to ``pyproject.toml``.

The existing release gates (``release.yml``, ``publish-image.yml``) check *tag* against
pyproject, which does not catch a ``server.json`` left behind — during the v0.5.36 release
it had to be bumped by hand, and nothing would have flagged it if that had been missed.
A stale ``server.json`` publishes an entry to the MCP registry that names a version the
artifact is not.
"""

from __future__ import annotations

import json
import tomllib  # stdlib on 3.11+, and jobd requires >=3.11
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = tomllib.loads((_ROOT / "pyproject.toml").read_text())
_SERVER_JSON = json.loads((_ROOT / "server.json").read_text())


def test_server_json_version_matches_pyproject() -> None:
    expected = _PYPROJECT["project"]["version"]
    assert _SERVER_JSON["version"] == expected, (
        f"server.json top-level version {_SERVER_JSON['version']!r} != pyproject {expected!r}"
    )
    for i, pkg in enumerate(_SERVER_JSON.get("packages", [])):
        assert pkg["version"] == expected, (
            f"server.json packages[{i}].version {pkg['version']!r} != pyproject {expected!r}"
        )


def test_server_json_identity() -> None:
    assert _SERVER_JSON["name"] == "io.github.musharna/jobd"
    identifiers = {p.get("identifier") for p in _SERVER_JSON.get("packages", [])}
    assert _PYPROJECT["project"]["name"] in identifiers, (
        f"pyproject name {_PYPROJECT['project']['name']!r} not among server.json "
        f"package identifiers {identifiers!r}"
    )
