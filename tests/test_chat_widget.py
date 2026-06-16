"""Tests for ChatWidget._on_rename_session (Issue #28).

ChatWidget requires PySide6. For headless runs QT_QPA_PLATFORM=offscreen must be
set before PySide6 is imported, and a QApplication must exist (qapp fixture
pattern, mirroring the other GUI tests).

The `updated` timestamp bump is verified directly; the dirty flag is verified by
binding a mock window and asserting mark_chat_dirty's call count (with
_window=None, _mark_chat_dirty() is a NOP and cannot be observed).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest


class _FakeBackend:
    name = "mock"
    model = "fake-model"


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def widget(qapp, monkeypatch):
    from gui.chat import ChatWidget

    w = ChatWidget(_FakeBackend, dispatch=lambda *a, **k: None)
    w.bind_window(MagicMock())
    return w


def _make_session(widget, *, dataset=None, title="元のタイトル"):
    """Add a fresh ChatSession to the widget and reflect it in the tab bar."""
    from llm_bridge import chat_store

    sess = chat_store.new_session("mock", "sys", dataset=dataset, title=title)
    widget._sessions.append(sess)
    widget._current_dataset = dataset
    widget._rebuild_tab_bar()
    return sess


def _tab_index_for(widget, sess):
    for i in range(widget._tab_bar.count()):
        if widget._tab_bar.tabData(i) == sess.id:
            return i
    return -1


def test_rename_success(widget, monkeypatch):
    from gui.chat import QInputDialog

    sess = _make_session(widget, dataset="ds")
    before = sess.updated
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("新しい名前", True))

    widget._on_rename_session(sess)

    assert sess.title == "新しい名前"
    assert sess.updated > before
    idx = _tab_index_for(widget, sess)
    assert widget._tab_bar.tabText(idx) == "新しい名前"
    assert widget._window.mark_chat_dirty.call_count == 1


def test_rename_cancel(widget, monkeypatch):
    from gui.chat import QInputDialog

    sess = _make_session(widget, dataset="ds")
    before_title, before_updated = sess.title, sess.updated
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("", False))

    widget._on_rename_session(sess)

    assert sess.title == before_title
    assert sess.updated == before_updated
    assert widget._window.mark_chat_dirty.call_count == 0


def test_rename_whitespace_only(widget, monkeypatch):
    from gui.chat import QInputDialog

    sess = _make_session(widget, dataset="ds")
    before_title, before_updated = sess.title, sess.updated
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("   ", True))

    widget._on_rename_session(sess)

    assert sess.title == before_title
    assert sess.updated == before_updated
    assert widget._window.mark_chat_dirty.call_count == 0


def test_rename_same_title(widget, monkeypatch):
    from gui.chat import QInputDialog

    sess = _make_session(widget, dataset="ds")
    before_title, before_updated = sess.title, sess.updated
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: (sess.title, True))

    widget._on_rename_session(sess)

    assert sess.title == before_title
    assert sess.updated == before_updated
    assert widget._window.mark_chat_dirty.call_count == 0


def test_rename_dataset_session_marks_dirty(widget, monkeypatch):
    from gui.chat import QInputDialog

    sess = _make_session(widget, dataset="some_dataset")
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("変更後", True))

    widget._on_rename_session(sess)

    assert widget._window.mark_chat_dirty.call_count == 1


def test_rename_scratch_session_no_dirty(widget, monkeypatch):
    from gui.chat import QInputDialog

    sess = _make_session(widget, dataset=None)
    before = sess.updated
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("変更後", True))

    widget._on_rename_session(sess)

    assert sess.title == "変更後"
    assert sess.updated > before
    assert widget._window.mark_chat_dirty.call_count == 0


def test_rename_monotonic_with_future_updated(widget, monkeypatch):
    from gui.chat import QInputDialog
    from llm_bridge.chat_store import merge_sessions
    import copy

    sess = _make_session(widget, dataset="ds", title="旧タイトル")
    sess.updated = time.time() + 3600  # NTP rollback: future value
    before = sess.updated
    old_sess = copy.deepcopy(sess)
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("最新タイトル", True))

    widget._on_rename_session(sess)

    assert sess.updated > before
    merged = merge_sessions([old_sess], [sess])
    assert merged[0].title == "最新タイトル"


# ---- new-session placement + drag-and-drop reorder ----


def test_new_session_appends_at_end(widget):
    before = widget._tab_bar.count()
    widget._on_new_session()
    assert widget._tab_bar.count() == before + 1
    # The new session is active and occupies the LAST tab, not the first.
    last = widget._tab_bar.count() - 1
    assert widget._tab_bar.tabData(last) == widget._active.id
    assert widget._active is widget._sessions[-1]


def test_tab_moved_reorders_backing_list(widget):
    from llm_bridge import chat_store

    hidden = chat_store.new_session("mock", "sys", dataset="other", title="隠し")
    a = chat_store.new_session("mock", "sys", dataset="ds", title="A")
    b = chat_store.new_session("mock", "sys", dataset="ds", title="B")
    # Pool: [scratch(None), hidden(other), A(ds), B(ds)]
    widget._sessions = [widget._sessions[0], hidden, a, b]
    widget._current_dataset = "ds"
    widget._active = a
    widget._rebuild_tab_bar()
    # Visible tabs (ds + scratch): [scratch, A, B] at indices 0,1,2.
    # Drag B (index 2) to the front (index 0) → fires tabMoved → _on_tab_moved.
    widget._tab_bar.moveTab(2, 0)

    vis = [s for s in widget._sessions if s.dataset in ("ds", None)]
    assert [s.title for s in vis] == ["B", "新しいチャット", "A"]
    # The hidden other-dataset session keeps its absolute pool position.
    assert widget._sessions[1] is hidden


def test_tab_moved_unresolvable_id_falls_back(widget):
    from llm_bridge import chat_store

    a = chat_store.new_session("mock", "sys", dataset=None, title="A")
    widget._sessions = [widget._sessions[0], a]
    widget._current_dataset = None
    widget._rebuild_tab_bar()
    # Corrupt a tab's session ref so the visible counts won't line up.
    widget._tab_bar.setTabData(0, "bogus-id")
    widget._on_tab_moved()  # must not raise — falls back to a clean rebuild

    assert widget._tab_bar.count() == 2
    ids = {widget._tab_bar.tabData(i) for i in range(2)}
    assert ids == {widget._sessions[0].id, a.id}
