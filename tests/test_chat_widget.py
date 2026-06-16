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


# ---- _simplify_tool_text (Issue #35, pure function — Qt not required) ----

CALL_A = "🔧 read_file  path=a.py"
RES_A = "   ↳ ok"
CALL_B = "🔧 write_file  path=b.py"
RES_B = "   ↳ done"


def test_simplify_t1_prose_only_unchanged():
    """T1: prose with no tool lines is byte-identical in every mode."""
    from gui.chat import _simplify_tool_text
    text = "Hello world.\n\nThis is a paragraph.\n- a\n- b"
    for mode in ("full", "compact", "hidden"):
        assert _simplify_tool_text(text, mode) == text


def test_simplify_t2_empty():
    from gui.chat import _simplify_tool_text
    for mode in ("full", "compact", "hidden"):
        assert _simplify_tool_text("", mode) == ""


def test_simplify_t1c_codeblock_preserved():
    """T1c: tool-line-free code block with blank lines is byte-identical."""
    from gui.chat import _simplify_tool_text
    text = (
        "```python\n"
        "def a():\n"
        "    pass\n"
        "\n"
        "\n"
        "def b():\n"
        "    pass\n"
        "```"
    )
    for mode in ("full", "compact", "hidden"):
        assert _simplify_tool_text(text, mode) == text


def test_simplify_t1b_full_tool_lines_separated():
    """T1b: full keeps all tool lines, each as a blank-separated paragraph."""
    from gui.chat import _simplify_tool_text
    text = f"intro\n{CALL_A}\n{RES_A}\n\n{CALL_B}\n{RES_B}\noutro"
    out = _simplify_tool_text(text, "full")
    lines = out.split("\n")
    assert lines.count(CALL_A) == 1 and lines.count(CALL_B) == 1
    assert lines.count(RES_A) == 1 and lines.count(RES_B) == 1
    # every tool line is flanked by blank lines (independent paragraph)
    for i, ln in enumerate(lines):
        if ln in (CALL_A, RES_A, CALL_B, RES_B):
            assert i > 0 and lines[i - 1] == ""
            assert i + 1 < len(lines) and lines[i + 1] == ""


def test_simplify_t3_compact_drops_results():
    """T3: compact keeps 🔧 lines, drops ↳/✗, each blank-separated."""
    from gui.chat import _simplify_tool_text
    text = f"intro\n{CALL_A}\n{RES_A}\n\n{CALL_B}\n{RES_B}\noutro"
    out = _simplify_tool_text(text, "compact")
    lines = out.split("\n")
    assert lines.count(CALL_A) == 1 and lines.count(CALL_B) == 1
    assert RES_A not in lines and RES_B not in lines
    for i, ln in enumerate(lines):
        if ln in (CALL_A, CALL_B):
            assert lines[i - 1] == "" and lines[i + 1] == ""


def test_simplify_t4_hidden_folds_run():
    """T4: hidden folds a run (incl. absorbed blank) into one summary line,
    flanked by blank lines, prose preserved."""
    from gui.chat import _simplify_tool_text
    text = "prose\n🔧 A\n   ↳ rA\n\n🔧 B\n   ↳ rB\nprose2"
    out = _simplify_tool_text(text, "hidden")
    assert out == "prose\n\n🔧 2 tool calls\n\nprose2"


def test_simplify_t5_hidden_three_calls():
    from gui.chat import _simplify_tool_text
    text = "🔧 A\n   ↳ rA\n🔧 B\n   ↳ rB\n🔧 C\n   ↳ rC"
    out = _simplify_tool_text(text, "hidden")
    assert "🔧 3 tool calls" in out.split("\n")


def test_simplify_t6_false_positive_in_code_block():
    """T6: marker-like lines INSIDE a fenced code block are (intentionally,
    documented limitation) misclassified as tool lines and folded/dropped.
    Pin the lossy behavior so it stays deliberate, not accidental."""
    from gui.chat import _simplify_tool_text
    text = "Here is code:\n```\n🔧 not_really_a_tool\n   ↳ also_not\n```"
    # hidden: the in-fence marker lines are folded into a summary (false positive)
    hidden_lines = _simplify_tool_text(text, "hidden").split("\n")
    assert "🔧 not_really_a_tool" not in hidden_lines
    assert "   ↳ also_not" not in hidden_lines
    assert "🔧 1 tool calls" in hidden_lines
    assert "```" in hidden_lines  # surrounding fence survives; content corrupted
    # compact: the result line is dropped, the call-shaped line kept
    compact_lines = _simplify_tool_text(text, "compact").split("\n")
    assert "   ↳ also_not" not in compact_lines
    assert "🔧 not_really_a_tool" in compact_lines


def test_simplify_t7_degenerate_run_results_only():
    """T7: a run with only result lines (no 🔧) keeps them verbatim in all modes."""
    from gui.chat import _simplify_tool_text
    text = f"intro\n{RES_A}\n{RES_B}\noutro"
    for mode in ("full", "compact", "hidden"):
        out = _simplify_tool_text(text, mode)
        lines = out.split("\n")
        assert RES_A in lines and RES_B in lines
        assert not any("tool calls" in ln for ln in lines)


def test_load_tool_display_invalid_types_never_raise(monkeypatch):
    """reviewer P1: a hand-edited / corrupt ui_prefs `tool_display` of any type —
    including unhashable JSON array/object — must normalize to 'full' without
    raising (the helper's contract is 'Must never raise')."""
    import llm_bridge.paths as paths
    from gui.chat import _load_tool_display
    for bad in ([], {}, ["full"], {"x": 1}, 0, 1.5, True, None, "bogus", ""):
        monkeypatch.setattr(paths, "read_ui_pref", lambda *a, **k: bad)
        assert _load_tool_display() == "full"
    for good in ("full", "compact", "hidden"):
        monkeypatch.setattr(paths, "read_ui_pref", lambda *a, _g=good, **k: _g)
        assert _load_tool_display() == good
