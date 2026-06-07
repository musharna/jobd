"""Job-array template expansion.

A job array is N normal jobs that share an identity (the array) and a command
template. Submitting `--count N` fans out one template into N members; each
member substitutes a set of `{key}` placeholders into the command args and env
values. The count form supplies a single key, `{i}` → the 0-based member index.

The substitution is a literal `{key}` → value replacement, NOT `str.format`.
This is deliberate: job commands routinely contain bare braces (JSON literals,
shell brace-expansion, f-string-looking args), and `str.format` would choke on
or mangle them. Replacing only the keys we know about leaves everything else
untouched.

The `subs` mapping is the extension point: the count form passes `{"i": ...}`;
a future `--sweep lr=0.1,0.01` form passes `{"i": ..., "lr": ...}` over the same
machinery with no changes here.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence


def render_template(value: str, subs: Mapping[str, str]) -> str:
    """Replace each literal `{key}` in `value` with its mapped string.

    Keys absent from `value` are no-ops; braces that don't match a key are left
    as-is. Order across keys is irrelevant because replacements are literal and
    non-overlapping by construction (callers pass disjoint placeholder names).
    """
    for key, repl in subs.items():
        value = value.replace("{" + key + "}", repl)
    return value


def render_cmd(cmd: list[str], subs: Mapping[str, str]) -> list[str]:
    """Apply `render_template` to every argument in a command list."""
    return [render_template(arg, subs) for arg in cmd]


def render_env(env: Mapping[str, str], subs: Mapping[str, str]) -> dict[str, str]:
    """Apply `render_template` to every value in an env mapping (keys unchanged)."""
    return {name: render_template(val, subs) for name, val in env.items()}


def index_subs(i: int) -> dict[str, str]:
    """Substitution mapping for the `--count` form: member index as `{i}`."""
    return {"i": str(i)}


def sweep_member_subs(axes: Sequence[tuple[str, Sequence[str]]]) -> list[dict[str, str]]:
    """Cartesian product of named sweep axes → one substitution mapping per member.

    `axes` is an ordered list of `(key, values)` pairs — e.g.
    `[("lr", ["0.1", "0.01"]), ("seed", ["1", "2", "3"])]` yields 6 members.
    Member order is the standard `itertools.product` odometer (last axis varies
    fastest). Each mapping also carries `{i}` = the flat 0-based member index, so
    `{i}` keeps working alongside the named keys (e.g. for an output dir).

    Axis order is preserved so the product (and thus the index assignment) is
    deterministic for a given CLI invocation.
    """
    keys = [k for k, _ in axes]
    value_lists = [list(vs) for _, vs in axes]
    out: list[dict[str, str]] = []
    for i, combo in enumerate(itertools.product(*value_lists)):
        subs = dict(zip(keys, combo, strict=False))
        subs["i"] = str(i)
        out.append(subs)
    return out
