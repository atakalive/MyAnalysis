"""common.slots — pane slot path grammar (Issue #97). Qt-free."""
import ast
from pathlib import Path

import pytest

from common.slots import MAX_DEPTH, conflicts, format_slot, parse_slot


def test_aliases_share_index():
    assert parse_slot("left") == [("h", 0)]
    assert parse_slot("top") == [("v", 0)]
    assert parse_slot("right") == [("h", 1)]
    assert parse_slot("bottom") == [("v", 1)]
    assert parse_slot("left")[0][1] == parse_slot("top")[0][1]
    assert parse_slot("right")[0][1] == parse_slot("bottom")[0][1]


def test_case_and_whitespace():
    assert parse_slot("  Top / LEFT ") == [("v", 0), ("h", 0)]


@pytest.mark.parametrize("s", [None, "", "   "])
def test_omitted(s):
    assert parse_slot(s) == []


def test_depth_limit():
    assert len(parse_slot("/".join(["left"] * MAX_DEPTH))) == MAX_DEPTH
    with pytest.raises(ValueError, match="invalid slot"):
        parse_slot("/".join(["left"] * (MAX_DEPTH + 1)))


@pytest.mark.parametrize("s", ["left//top", "left/", "/left", "center", "top/middle", "l"])
def test_invalid(s):
    with pytest.raises(ValueError, match="invalid slot") as ei:
        parse_slot(s)
    assert "grammar" in str(ei.value)


@pytest.mark.parametrize("s", ["left", "top/right", "bottom/left/top", "right/bottom/right/top"])
def test_format_roundtrip(s):
    assert format_slot(parse_slot(s)) == s
    assert format_slot([]) == ""


def test_conflicts():
    p = parse_slot
    assert conflicts(p("top/left"), p("left/top"))      # same node, axis differs
    assert not conflicts(p("top/left"), p("top/right"))  # different child
    assert not conflicts(p("left"), p("right"))
    assert conflicts(p("top"), p("top/left"))            # prefix
    assert conflicts(p("top/left"), p("top"))
    assert conflicts(p("top/left"), p("top/left"))       # equal
    assert conflicts(p("left"), p("bottom"))             # root axis differs


def test_no_qt_import():
    src = Path(__file__).resolve().parents[1] / "common" / "slots.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            assert "PySide6" not in ast.unparse(n)
