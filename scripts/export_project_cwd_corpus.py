#!/usr/bin/env python3
"""Export distinct (project, cwd, count) from the live job history.

Run on the broker host; commit the output. This is production data copied
verbatim, not a fixture: the whole value of the replay test in
tests/test_corpus_replay.py is that the author did not choose its contents.

    ssh <broker> 'timeout 90 python3 -' < scripts/export_project_cwd_corpus.py \
        > tests/data/project_cwd_corpus.csv

Refresh it the same way. If the export fails, the correct response is to fix
the export -- never to hand-write rows. A synthesized corpus would still make
the test pass, which is exactly the failure it exists to rule out.
"""

import csv
import os
import signal
import sqlite3
import sys

signal.signal(
    signal.SIGALRM,
    lambda *_: (sys.stderr.write("aborting: walltime guard\n"), sys.exit(2)),
)
signal.alarm(60)

DB = os.environ.get("JOBD_DB", "/home/mjarnold/jobd/data/jobd.db")
rows = (
    sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    .execute("SELECT project, cwd, COUNT(*) FROM jobs GROUP BY project, cwd ORDER BY project, cwd")
    .fetchall()
)

w = csv.writer(sys.stdout)
w.writerow(["project", "cwd", "n"])
w.writerows(rows)
