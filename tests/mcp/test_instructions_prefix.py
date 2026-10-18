"""The server's instructions name tools bare (jobd_list). A client such as
Claude Code lists them prefixed (mcp__jobd__jobd_list) and refuses a bare
call; in a sibling server's benchmark the model copied a bare name from the
instructions and 23 of 138 calls were refused. The instructions must say so
before the first tool name appears."""

import re

from jobd.mcp.server import _INSTRUCTIONS, _TOOLS


def test_prefix_note_comes_before_the_first_bare_tool_name():
    names = [t[0] for t in _TOOLS]
    first_bare = min(
        m.start()
        for n in names
        for m in re.finditer(rf"(?<![\w_]){re.escape(n)}\b", _INSTRUCTIONS)
        if not _INSTRUCTIONS[max(0, m.start() - 10) : m.start()].endswith("mcp__jobd__")
    )
    note = _INSTRUCTIONS.find("mcp__jobd__jobd_list")
    assert note != -1, "the instructions never show the prefixed name"
    assert "prefix" in _INSTRUCTIONS[: first_bare + 200].lower()
    # The note's own example ("written bare (jobd_list)") is the first bare
    # name; the prefixed form must follow within the same note.
    assert note - first_bare < 200, "the prefixed example is not next to the first bare name"


def test_every_tool_named_in_the_instructions_exists():
    names = {t[0] for t in _TOOLS}
    for n in set(re.findall(r"\bjobd_[a-z_]+\b", _INSTRUCTIONS)):
        assert n in names, n
