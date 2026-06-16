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
    c = chat_store.new_session("mock", "sys", dataset="ds", title="C")
    # Pool: [hidden(other), A(ds), B(ds), C(ds)]
    widget._sessions = [hidden, a, b, c]
    widget._current_dataset = "ds"
    widget._active = a
    widget._rebuild_tab_bar()
    # Visible tabs (ds): [A, B, C] at indices 0,1,2 (hidden belongs to "other").
    # Drag C (index 2) to the front (index 0) → fires tabMoved → _on_tab_moved.
    widget._tab_bar.moveTab(2, 0)

    assert [s.title for s in widget._visible_sessions()] == ["C", "A", "B"]
    # The hidden other-dataset session keeps its absolute pool position.
    assert widget._sessions[0] is hidden


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


# ---- opening datasets must not accumulate empty chat tabs ----


def test_open_chatless_datasets_does_not_accumulate_tabs(widget):
    from llm_bridge import chat_store
    from llm_backend.base import Message

    # A history-bearing chat bound to dataset A is active (mimics: user opened A
    # and chatted, so the original scratch was adopted).
    a = chat_store.new_session("mock", "sys", dataset="A", title="chatA")
    a.messages.append(Message(role="user", content="hi"))
    widget._sessions = [a]
    widget._active = a
    widget._current_dataset = "A"
    widget._rebuild_tab_bar()

    # Open B (no chats): one dataset-agnostic blank is created, NOT bound to B.
    widget.set_current_dataset("B")
    after_b = list(widget._sessions)
    assert len(after_b) == 2
    blank = widget._active
    assert blank is not a
    assert blank.dataset is None

    # Open C, then D (no chats): the same blank is reused — no new tabs.
    widget.set_current_dataset("C")
    widget.set_current_dataset("D")
    assert widget._sessions == after_b
    assert widget._active is blank


def test_open_dataset_reuses_empty_foreign_active(widget):
    """An empty session bound to another dataset (e.g. left over from a delete)
    is reused on open and rebound to None, not duplicated."""
    from llm_bridge import chat_store

    blank_a = chat_store.new_session("mock", "sys", dataset="A", title="新しいチャット")
    widget._sessions = [blank_a]
    widget._active = blank_a
    widget._current_dataset = "A"
    widget._rebuild_tab_bar()

    widget.set_current_dataset("B")

    assert widget._sessions == [blank_a]  # no new session minted
    assert widget._active is blank_a
    assert blank_a.dataset is None  # rebound to a universal blank


def test_open_dataset_with_chats_hides_empty_blank(widget):
    """Opening a dataset that has its own chats must not show a stray empty
    'new chat' tab — only the dataset's chats."""
    from llm_bridge import chat_store

    c1 = chat_store.new_session("mock", "sys", dataset="A", title="chat1")
    # Pool: [scratch(None, empty), C1(A)]
    widget._sessions = [widget._sessions[0], c1]

    widget.set_current_dataset("A")

    assert [s.title for s in widget._visible_sessions()] == ["chat1"]
    assert widget._active.id == c1.id


def test_nonactive_history_scratch_visible_with_bound_chats(widget):
    """An unsaved (history-bearing) scratch is never hidden, even when the
    dataset has bound chats — only EMPTY blanks are suppressed."""
    from llm_bridge import chat_store
    from llm_backend.base import Message

    scratch = chat_store.new_session("mock", "sys", dataset=None, title="未保存")
    scratch.messages.append(Message(role="user", content="x"))  # has history, unbound
    c1 = chat_store.new_session("mock", "sys", dataset="A", title="chat1")
    widget._sessions = [c1, scratch]
    widget._active = c1
    widget._current_dataset = "A"

    vis_ids = {s.id for s in widget._visible_sessions()}
    assert scratch.id in vis_ids
    assert c1.id in vis_ids
