"""Score one benchmark answer against its question's truth.

Every run ends its reply with ``ANSWER: <value>`` (or ``ANSWER: UNKNOWN``);
the verdict is decided from that line alone, so the same reply always scores
the same way. Verdicts:

- ``right``: the value matches the truth.
- ``wrong``: a value that does not match.
- ``declined``: ``ANSWER: UNKNOWN``.
- ``no_answer``: no ``ANSWER:`` line at all (a format failure, kept apart so it
  is never silently counted as either of the others).

Matching rules, by category:

- ``exit_code``: the integer must be equal (sign included: -15 is how jobd
  records a SIGTERM, 143 is a different number).
- ``host``: any name the host goes by (its own aliases from the key), case
  ignored.
- ``wall_time``: within 1 s of finished_at - started_at. Both timestamps are
  shown to microseconds by every surface (``job status``, ``jobd_status``),
  and the question asks for whole seconds, so rounding moves an honest answer
  by at most 0.5 s and truncation by under 1 s. ``H:MM:SS`` is read too.
- ``project_failures``: the set of job ids must be equal, order ignored. One
  missing or extra id is wrong: the question asks for every one.
- ``log_line``: equal after trimming, collapsing runs of whitespace, and
  dropping one pair of quotes or backticks wrapped around the whole line.
  Case counts: it is a quotation.
- ``fleet``: the set of hosts must be equal, each name read through the
  key's alias table.

Version 2 (the harder set, ``build_hard.py``):

- ``window_count``, ``event_count``: the integer must be equal.
- ``window_ids``: the set of job ids must be equal.
- ``busiest_host``: as ``host``.
- ``busiest_project``: the project name, case ignored, quotes dropped.
- ``total_runtime``: within the key's ``tolerance_s`` (1 s per job summed, the
  most rounding each job's whole seconds can move the total).
- ``queue_wait``: within 1 s, as ``wall_time``.
- ``log_join``: exactly one job id, the key's.

A wrong answer in ``host`` or ``fleet`` that names no host the broker has ever
known (the key's ``known_names``, checked, not guessed from its shape) is also
flagged ``unknown_host``.
"""

from __future__ import annotations

import re
from typing import Any

_ANSWER_RE = re.compile(r"^[\s>*_`#-]*answer[*_`]*\s*:\s*(.*?)\s*$", re.IGNORECASE)
# Not "NONE": here it is a claim ("no job failed that day", "no worker can
# run it"), so it is scored like any other value.
_UNKNOWN = {"UNKNOWN", "UNKNOWN.", "N/A"}
_QUOTES = ("`", '"', "'")


def extract(reply: str) -> str | None:
    """The value on the reply's last ``ANSWER:`` line, unwrapped; None if none."""
    found = None
    for line in reply.splitlines():
        m = _ANSWER_RE.match(line)
        if m:
            found = m.group(1)
    if found is None:
        return None
    return re.sub(r"</?answer>", "", found, flags=re.IGNORECASE).strip(" \t*_")


def _int(value: str) -> int | None:
    m = re.search(r"(?<![\w.])[-+]?\d+(?![\w.])", value.replace("`", ""))
    return int(m.group(0)) if m else None


def _seconds(value: str) -> float | None:
    v = value.replace(",", "").replace("`", "").strip()
    m = re.match(r"^(\d+):([0-5]\d):([0-5]\d(?:\.\d+)?)$", v)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.match(r"^~?\s*(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)?\.?$", v, re.IGNORECASE)
    return float(m.group(1)) if m else None


def _ids(value: str) -> set[int]:
    return {int(t) for t in re.findall(r"(?<![\w.])#?(\d+)(?![\w.])", value)}


def _line(value: str) -> str:
    v = value.strip()
    for q in _QUOTES:
        if len(v) >= 2 and v.startswith(q) and v.endswith(q):
            v = v[1:-1].strip()
            break
    return re.sub(r"\s+", " ", v)


def _hosts(value: str, known: dict[str, str]) -> list[str]:
    value = re.sub(r"\([^)]*\)", " ", value)  # "laptop (3 slots)" names laptop
    tokens = [t.strip(" .`'\"") for t in re.split(r",|\band\b|\s+", value)]
    return [known.get(t.lower(), t) for t in tokens if t]


def score(question: dict[str, Any], reply: str) -> dict[str, Any]:
    value = extract(reply)
    if value is None:
        return {"verdict": "no_answer", "value": None, "unknown_host": False}
    if value.strip("`").upper() in _UNKNOWN:
        return {"verdict": "declined", "value": value, "unknown_host": False}
    truth = question["truth"]
    category = question["category"]
    names: list[str] = []
    if category == "exit_code":
        right = _int(value) == truth["exit_code"]
    elif category == "host":
        names = _hosts(value, truth["known_names"])
        right = len(names) == 1 and names[0].lower() in {n.lower() for n in truth["names"]}
    elif category == "wall_time":
        got = _seconds(value)
        right = got is not None and abs(got - truth["seconds"]) <= 1.0
    elif category == "project_failures":
        right = _ids(value) == set(truth["job_ids"])
    elif category == "log_line":
        right = _line(value) == _line(truth["line"])
    elif category == "fleet":
        names = _hosts(value, truth["known_names"])
        right = set(names) == set(truth["hosts"])
    elif category in ("window_count", "event_count"):
        right = _int(value) == truth["count"]
    elif category == "window_ids":
        right = _ids(value) == set(truth["job_ids"])
    elif category == "busiest_host":
        names = _hosts(value, truth["known_names"])
        right = len(names) == 1 and names[0].lower() in {n.lower() for n in truth["names"]}
    elif category == "busiest_project":
        right = _line(value).strip(" .").casefold() == truth["project"].casefold()
    elif category in ("total_runtime", "queue_wait"):
        got = _seconds(value)
        tol = truth.get("tolerance_s", 1.0)
        right = got is not None and abs(got - truth["seconds"]) <= tol
    elif category == "log_join":
        right = _ids(value) == {truth["job_id"]}
    else:
        raise ValueError(f"unknown category {category!r}")
    known = set(truth.get("known_names", {}).values())
    unknown_host = not right and any(n not in known for n in names)
    return {"verdict": "right" if right else "wrong", "value": value, "unknown_host": unknown_host}


def oracle_reply(question: dict[str, Any]) -> str:
    """The key's own answer as a reply: the positive control. Every one must
    score right, or the scorer cannot recognise a correct answer."""
    t, c = question["truth"], question["category"]
    value = {
        "exit_code": lambda: str(t["exit_code"]),
        "host": lambda: t["names"][0],
        "wall_time": lambda: str(round(t["seconds"])),
        "project_failures": lambda: ", ".join(str(i) for i in t["job_ids"]),
        "log_line": lambda: t["line"],
        "fleet": lambda: ", ".join(t["hosts"]),
        "window_count": lambda: str(t["count"]),
        "event_count": lambda: str(t["count"]),
        "window_ids": lambda: ", ".join(str(i) for i in t["job_ids"]),
        "busiest_host": lambda: t["names"][0],
        "busiest_project": lambda: t["project"],
        "total_runtime": lambda: str(round(t["seconds"])),
        "queue_wait": lambda: str(round(t["seconds"])),
        "log_join": lambda: str(t["job_id"]),
    }[c]()
    return f"Looked it up.\nANSWER: {value}"


def wrong_reply(question: dict[str, Any]) -> str:
    """A near miss for every kind: the negative control. Every one must score
    wrong, or the scorer is too lenient to tell a miss from a hit."""
    t, c = question["truth"], question["category"]
    if c == "exit_code":
        value = str(t["exit_code"] + 1)
    elif c == "host":
        others = sorted(set(t["known_names"].values()) - {t["names"][0]})
        value = others[0] if others else "no-such-host"
    elif c == "wall_time":
        value = str(round(t["seconds"]) + 3)
    elif c == "project_failures":
        value = ", ".join(str(i) for i in [*t["job_ids"], max(t["job_ids"]) + 1])
    elif c == "log_line":
        value = t["line"] + " (cont.)"
    elif c == "fleet":
        extra = sorted(set(t["known_names"].values()) - set(t["hosts"]))
        value = ", ".join([*t["hosts"], extra[0]] if extra else t["hosts"][:-1] or ["none"])
    elif c in ("window_count", "event_count"):
        value = str(t["count"] + 1)
    elif c == "window_ids":
        value = ", ".join(str(i) for i in [*t["job_ids"], max(t["job_ids"]) + 1])
    elif c == "busiest_host":
        # The runner-up: the near miss a tally slip would give.
        others = sorted((h for h in t["tally"] if h != t["names"][0]), key=lambda h: -t["tally"][h])
        value = others[0]
    elif c == "busiest_project":
        others = sorted((p for p in t["tally"] if p != t["project"]), key=lambda p: -t["tally"][p])
        value = others[0]
    elif c in ("total_runtime", "queue_wait"):
        value = str(round(t["seconds"] + t.get("tolerance_s", 1.0) + 2))
    elif c == "log_join":
        value = next(i for i in sorted(t["lines"]) if int(i) != t["job_id"])
    else:
        raise ValueError(f"unknown category {c!r}")
    return f"Looked it up.\nANSWER: {value}"
