"""Guard the guards: prove the H-1 / TOCTOU monkeypatch targets actually reach the code.

`submit` moved out of the `jobd.app` route closure into `jobd.broker.submit`. The two
tests that protect the worst bugs this project has shipped work by monkeypatching
module-level names:

    tests/test_h1_real_race_regression.py   patches _serialization_warning
    tests/test_submit_depends_on_toctou.py  patches _FAILED_SIDE_TERMINAL

Those patches only do anything if they target the module whose globals `submit_job`
resolves the names from. If the code moves again and the patch sites don't follow, the
patches become **silent no-ops and both tests keep passing while testing nothing** —
exactly the failure the 2026-07-12 audit found (an H-1 fix that was a no-op in
production while its test stayed green, v0.5.13).

A green suite cannot detect that. These tests can: each one proves the patch is *live*
by observing it change behaviour, and the last one removes the trap that made the
silent version possible in the first place.
"""

from __future__ import annotations

import jobd.app as app_mod
import jobd.broker.submit as submit_mod


def _submit(client, **overrides):
    body = {"cmd": ["true"], "cwd": "/tmp", "project": "project-a"}
    body.update(overrides)
    return client.post("/submit", json=body)


def test_serialization_warning_patch_target_is_live(client, monkeypatch):
    """Patching jobd.broker.submit._serialization_warning must actually intercept.

    If this fails, test_h1_real_race is no longer injecting its race and is passing
    vacuously — the H-1 regression would be undetectable.
    """
    fired: list[int] = []
    orig = submit_mod._serialization_warning

    def hook(*a, **kw):
        fired.append(1)
        return orig(*a, **kw)

    monkeypatch.setattr(submit_mod, "_serialization_warning", hook)
    assert _submit(client).status_code == 200
    assert fired, (
        "patching jobd.broker.submit._serialization_warning did NOT intercept the "
        "submit path. test_h1_real_race's race injection is therefore a no-op and that "
        "test is passing while proving nothing. The submit logic has moved — repoint "
        "the patch target to wherever submit_job now resolves this name."
    )


def test_failed_side_terminal_patch_target_is_live(client, monkeypatch):
    """Patching jobd.broker.submit._FAILED_SIDE_TERMINAL must reach the reject.

    The TOCTOU test empties this set so the submit-time point-read reject does NOT
    fire, letting it prove the H-1 cascade catches the child instead. If the patch
    stops reaching the code, the reject fires and that test is no longer exercising
    the cascade at all.
    """
    parent_id = _submit(client).json()["id"]
    client.post(f"/jobs/{parent_id}/cancel")  # -> a failed-side terminal state

    # Unpatched: submitting a default-policy child of a failed-side parent is rejected.
    rejected = _submit(client, depends_on=[parent_id])
    assert rejected.status_code == 400, (
        "precondition: a child of an already-cancelled parent should be rejected at "
        f"submit; got {rejected.status_code}"
    )

    # Patched to empty: the reject must no longer fire — which is only true if the
    # patch actually reaches the code path.
    monkeypatch.setattr(submit_mod, "_FAILED_SIDE_TERMINAL", frozenset())
    accepted = _submit(client, depends_on=[parent_id])
    assert accepted.status_code == 200, (
        "patching jobd.broker.submit._FAILED_SIDE_TERMINAL did NOT reach the "
        "submit-time reject (still got "
        f"{accepted.status_code}). test_submit_depends_on_toctou is therefore not "
        "exercising the H-1 cascade it claims to test."
    )


def test_jobd_app_does_not_expose_the_stale_patch_targets():
    """jobd.app must NOT carry these names — or a stale patch fails silently.

    This is the trap that made the original no-op possible. `monkeypatch.setattr` with
    a string target raises AttributeError when the attribute is absent, so a stale
    `"jobd.app._FAILED_SIDE_TERMINAL"` now fails LOUDLY. But a direct assignment —
    `app_mod._serialization_warning = hook`, which is exactly how the H-1 test injects
    its race — would happily create a brand-new attribute on the module that nothing
    reads, and the test would sail through proving nothing.

    Keeping these names off jobd.app means there is nothing to patch there by mistake.
    """
    for name in ("_serialization_warning", "_FAILED_SIDE_TERMINAL"):
        assert not hasattr(app_mod, name), (
            f"jobd.app re-exposes {name!r}. Submit no longer reads it from there, so "
            "patching it is a silent no-op — and a test that patches it would pass "
            "while testing nothing. Import it in jobd.broker.submit only."
        )
