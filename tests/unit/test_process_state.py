"""The suite puts the child-subreaper bit back after every test.

``tests/process_state.py`` says why. The first test drives the real ``prctl``
in both directions, so a restore that always clears the bit, or one that never
restores, fails it. The last two are a pair in file order: one leaves the bit
set, the next finds it cleared, which only the autouse fixture can have done.
"""

from __future__ import annotations

import sys

import pytest

from jobd import subreaper
from tests.process_state import (
    child_subreaper_bit,
    child_subreaper_bit_restored,
    set_child_subreaper_bit,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="prctl is Linux-only")


def test_the_bit_is_put_back_whichever_way_the_body_moved_it() -> None:
    with child_subreaper_bit_restored():  # leave this process as it was found
        set_child_subreaper_bit(0)
        with child_subreaper_bit_restored():
            assert subreaper.set_child_subreaper() is True
            assert child_subreaper_bit() == 1  # the real call set the real bit
        assert child_subreaper_bit() == 0

        # The other direction: a restore that only ever clears would pass above.
        set_child_subreaper_bit(1)
        with child_subreaper_bit_restored():
            set_child_subreaper_bit(0)
            assert child_subreaper_bit() == 0
        assert child_subreaper_bit() == 1

        # A body that raises is restored too.
        set_child_subreaper_bit(0)
        with pytest.raises(RuntimeError, match="body failed"), child_subreaper_bit_restored():
            set_child_subreaper_bit(1)
            raise RuntimeError("body failed")
        assert child_subreaper_bit() == 0


def test_a_test_that_sets_the_bit_and_walks_away() -> None:
    """Leaves the bit set on purpose; the next test, in file order, reads it."""
    assert subreaper.set_child_subreaper() is True
    assert child_subreaper_bit() == 1


def test_every_test_runs_inside_the_restore(request: pytest.FixtureRequest) -> None:
    assert "_restore_child_subreaper_bit" in request.fixturenames
    # The test above left the bit set; the fixture's teardown cleared it.
    assert child_subreaper_bit() == 0
