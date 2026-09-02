"""audit 2026-09-02 L-1: `/reload` swapped `state["projects"]` while a
concurrent `set`/`nudge` could be between mutating the old dict and persisting
`state["projects"]` -- the overlay was then written from the NEW dict and the
mutation lost both in memory and on disk. The fix is that reload takes the same
lock the writers hold.
"""

import threading

from jobd.broker.projects import projects_mutation_lock


def test_reload_waits_for_the_projects_mutation_lock(client):
    done = threading.Event()
    status: list[int] = []

    def _reload():
        status.append(client.post("/reload").status_code)
        done.set()

    projects_mutation_lock.acquire()
    try:
        t = threading.Thread(target=_reload, daemon=True)
        t.start()
        # While a writer holds the lock, reload must not complete.
        assert not done.wait(0.5), "reload ran without taking projects_mutation_lock"
    finally:
        projects_mutation_lock.release()
    # Positive control: once the writer is done, reload proceeds normally.
    assert done.wait(5.0)
    assert status == [200]
