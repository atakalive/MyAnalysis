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


# ---- chat search verbs（Issue #108） ----


def _chat_ctx():
    from llm_backend.base import Message
    from llm_bridge.chat_search import SearchContext
    from llm_bridge.chat_store import new_session

    def mk(ds, text, *, archived=False, updated=1.0):
        s = new_session("b", "sys", dataset=ds, title=f"t-{text}")
        s.messages += [Message(role="user", content=text), Message(role="assistant", content="ok " + text)]
        s.archived = archived
        s.updated = updated
        return s

    odd = "<chat_index> a'b"
    sessions = [mk("dsA", "peak one", updated=3.0), mk("dsB", "peak two", updated=2.0),
                mk(None, "peak free", updated=1.0), mk(odd, "peak odd", updated=4.0),
                mk(odd, "peak old", archived=True, updated=5.0)]
    search = new_session("b", "sys", dataset="dsA", kind="search",
                         search_spec={"scope": "dataset", "dataset": odd,
                                      "include_archived": True, "ai": True})
    sessions.append(search)
    ctx = SearchContext(sessions=sessions, current_dataset="dsA",
                        open_datasets=["dsA", "dsB", odd])
    return ctx, search, odd, sessions


def _handlers(ctx):
    win = MagicMock()
    win.chat_widget.return_value.search_context.return_value = ctx
    llm_bridge._rewire_window(win)
    return win, {c.args[0]: c.args[1] for c in win.register_command.call_args_list}


def test_chat_verbs_match_core():
    import pytest
    from llm_bridge import chat_search as cs
    ctx, _search, _odd, sessions = _chat_ctx()
    win, h = _handlers(ctx)
    assert h["chat-list"](scope="all", archived="true", offset=1) == cs.list_page(
        ctx, scope="all", include_archived=True, offset=1)
    assert h["chat-search"](query="peak", offset=1) == cs.search_hits(ctx, "peak", offset=1)
    sid = sessions[0].id[:8]
    assert h["chat-show"](sid=sid, start=1, end=2, char_offset=2) == cs.show_session(
        ctx, sid, start=1, end=2, char_offset=2)
    assert h["chat-list"](scope="unbound")["total"] == 1
    with pytest.raises(ValueError):
        h["chat-list"](scope="bogus")
    with pytest.raises(ValueError):
        h["chat-search"](query="peak", limit=True)
    win.chat_widget.return_value = None
    with pytest.raises(RuntimeError):
        h["chat-list"]()


def test_chat_verbs_search_tab_end_to_end():
    import re

    import pytest
    from llm_bridge import chat_search as cs
    ctx, search, odd, sessions = _chat_ctx()
    _win, h = _handlers(ctx)
    req = cs.SearchRequest(query="q", scope="dataset", dataset=odd, include_archived=True, ai=True)
    prompt = cs.build_ai_prompt(req, ctx, "en", search_sid=search.id)
    tab = re.search(r"search_tab=([0-9a-f]+)", prompt).group(1)
    expect_list = cs.list_page(ctx, scope="dataset", dataset=odd, include_archived=True)
    assert h["chat-list"](search_tab=tab) == expect_list
    assert expect_list["total"] == 3   # odd の 2 件（archived 含む）＋ DS 無しの 1 件
    assert h["chat-list"](search_tab=tab, scope="all") == expect_list
    assert h["chat-search"](query="peak", search_tab=tab) == cs.search_hits(
        ctx, "peak", scope="dataset", dataset=odd, include_archived=True)
    with pytest.raises(ValueError):
        h["chat-list"](search_tab=sessions[0].id[:8])
    with pytest.raises(LookupError):
        h["chat-list"](search_tab="ffffffff")
