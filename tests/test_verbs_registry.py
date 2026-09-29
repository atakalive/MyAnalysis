"""llm_bridge/verbs.py の表が実際に登録される verb と一致する（Issue #100 D-1）。

Qt は起動しない（window / tab は MagicMock）。gui.imageviewer の import は PySide6 を
読むが必須依存なので skip しない。
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import llm_bridge
from llm_bridge import commands
from llm_bridge.__main__ import main
from llm_bridge.verbs import (
    ANALYSIS_TAB_VERBS,
    COMMON_TAB_VERBS,
    IMAGE_VIEWER_VERBS,
    INTERNAL_WINDOW_VERBS,
    WINDOW_VERBS,
)

_REPO = Path(__file__).resolve().parent.parent


def _registered(mock) -> set[str]:
    return {c.args[0] for c in mock.register_command.call_args_list}


def _ast_registered(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register_command"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.add(node.args[0].value)
    return found


def test_window_verbs_match_registered():
    win = MagicMock()
    llm_bridge._rewire_window(win)
    hot = _ast_registered(_REPO / "devtools" / "qt_integration.py")
    assert hot == {"reload"}
    assert _registered(win) | hot == set(WINDOW_VERBS) | set(INTERNAL_WINDOW_VERBS)
    assert set(WINDOW_VERBS) & set(INTERNAL_WINDOW_VERBS) == set()


def test_analysis_tab_verbs_match_registered(monkeypatch):
    expected = set(COMMON_TAB_VERBS) | set(ANALYSIS_TAB_VERBS)

    # dataset 無しの分岐（_building を立てない。watcher を張らない）
    tab = MagicMock()
    llm_bridge.attach_tab(tab, lambda: {})
    assert _registered(tab) == expected

    # dataset 有りの分岐
    noop_writer = lambda *a, **k: (lambda *_a, **_k: None)  # noqa: E731
    monkeypatch.setattr("llm_bridge.state.writer", noop_writer)
    monkeypatch.setattr("llm_bridge.snapshots.writer", noop_writer)
    monkeypatch.setattr("llm_bridge.annotations.start_watcher", lambda tab, ds: None)
    tab = MagicMock()
    with llm_bridge._building("ds"):
        llm_bridge.attach_tab(tab, lambda: {})
    assert _registered(tab) == expected


def test_viewer_tab_verbs_match_registered():
    tab = MagicMock()
    llm_bridge._finish_new(MagicMock(), tab, "v", None)
    assert _registered(tab) == set(COMMON_TAB_VERBS)
    assert set(COMMON_TAB_VERBS) & set(ANALYSIS_TAB_VERBS) == set()


def test_viewer_verbs_match_registered():
    from gui.imageviewer import _register_viewer_verbs

    tab = MagicMock()
    _register_viewer_verbs(tab, MagicMock())
    assert _registered(tab) == set(IMAGE_VIEWER_VERBS)


def _listed(out: str) -> set[str]:
    return {
        line.strip().split()[0]
        for line in out.splitlines()
        if line.startswith("  ") and line.strip()
    }


def test_list_commands_prints_every_verb(monkeypatch, capsys):
    submitted = []
    monkeypatch.setattr(commands, "submit", lambda *a, **k: submitted.append(a))

    assert main(["list-commands"]) == 0
    assert _listed(capsys.readouterr().out) == set(WINDOW_VERBS) | set(INTERNAL_WINDOW_VERBS)

    assert main(["list-commands", "viewer"]) == 0
    assert _listed(capsys.readouterr().out) == (
        set(COMMON_TAB_VERBS) | set(ANALYSIS_TAB_VERBS) | set(IMAGE_VIEWER_VERBS)
    )
    assert submitted == []
