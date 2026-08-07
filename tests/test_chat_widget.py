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

from common.i18n import tr


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


# ---- per-dataset last-active chat session (Issue #74) ----


def test_dataset_switch_restores_last_active_session(widget):
    """Switching datasets returns to the session last open in each, not the first.

    Bound sessions are always visible (no history needed), so A shows [A1, A2].
    """
    from llm_bridge import chat_store

    a1 = chat_store.new_session("mock", "sys", dataset="A", title="A1")
    a2 = chat_store.new_session("mock", "sys", dataset="A", title="A2")
    b1 = chat_store.new_session("mock", "sys", dataset="B", title="B1")
    widget._sessions = [a1, a2, b1]
    widget._active = a1
    widget._current_dataset = "A"
    widget._rebuild_tab_bar()

    # User selects A2 (drives the real currentChanged -> _on_switch_session path).
    widget._tab_bar.setCurrentIndex(_tab_index_for(widget, a2))
    assert widget._active is a2

    # B has no memory yet -> its only/first session.
    widget.set_current_dataset("B")
    assert widget._active is b1

    # Back to A: restores A2, NOT the first session A1.
    widget.set_current_dataset("A")
    assert widget._active is a2

    # B remembers b1 as its last-active too.
    widget.set_current_dataset("B")
    assert widget._active is b1


def test_dataset_switch_falls_back_when_remembered_session_gone(widget):
    """A remembered session that is no longer visible must not be resurrected —
    the switch falls back to the first visible session."""
    from llm_bridge import chat_store

    a1 = chat_store.new_session("mock", "sys", dataset="A", title="A1")
    a2 = chat_store.new_session("mock", "sys", dataset="A", title="A2")
    b1 = chat_store.new_session("mock", "sys", dataset="B", title="B1")
    widget._sessions = [a1, a2, b1]
    widget._active = a1
    widget._current_dataset = "A"
    widget._rebuild_tab_bar()

    widget._tab_bar.setCurrentIndex(_tab_index_for(widget, a2))
    widget.set_current_dataset("B")          # remembers A -> a2

    widget._sessions = [a1, b1]              # a2 disappears (e.g. deleted/synced away)
    widget.set_current_dataset("A")
    assert widget._active is a1

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
    """T3: compact aggregates a run into a single 🔧 line of names, drops ↳/✗."""
    from gui.chat import _simplify_tool_text
    text = f"intro\n{CALL_A}\n{RES_A}\n\n{CALL_B}\n{RES_B}\noutro"
    out = _simplify_tool_text(text, "compact")
    lines = out.split("\n")
    tool_lines = [ln for ln in lines if ln.startswith("🔧")]
    assert len(tool_lines) == 1
    assert tool_lines[0] == "🔧 read_file · write_file"
    assert RES_A not in lines and RES_B not in lines
    assert CALL_A not in lines and CALL_B not in lines


def test_simplify_compact_aggregates_run_names_in_order():
    """SSOT 行 A: 呼び出し3つ (Read, Grep, Read) が 1 本の 🔧 行に順序・重複保持で集約される。"""
    from gui.chat import _simplify_tool_text
    text = "🔧 Read  a.py\n   ↳ x\n🔧 Grep  q\n   ↳ y\n🔧 Read  b.py"
    out = _simplify_tool_text(text, "compact")
    tool_lines = [ln for ln in out.split("\n") if ln.startswith("🔧")]
    assert len(tool_lines) == 1
    assert tool_lines[0] == "🔧 Read · Grep · Read"


def test_simplify_compact_drops_arg_summary():
    """SSOT 行 D: 引数summary付き呼び出しは名前のみに集約され summary は消える。"""
    from gui.chat import _simplify_tool_text
    out = _simplify_tool_text(CALL_B, "compact")
    tool_lines = [ln for ln in out.split("\n") if ln.startswith("🔧")]
    assert len(tool_lines) == 1
    assert tool_lines[0] == "🔧 write_file"
    assert "path=b.py" not in out


def test_simplify_compact_separate_runs_across_prose():
    """SSOT 行 C: prose を挟んだ 2 つの run は別々の集約行になる (run 境界維持)。"""
    from gui.chat import _simplify_tool_text
    text = f"pre\n🔧 A  x\n{RES_A}\nmid\n🔧 B  y\n{RES_B}\npost"
    out = _simplify_tool_text(text, "compact")
    tool_lines = [ln for ln in out.split("\n") if ln.startswith("🔧")]
    assert tool_lines == ["🔧 A", "🔧 B"]


def test_simplify_compact_single_call_run():
    """SSOT 行 D 同型: 単一呼び出しの run は区切り無しで 🔧 <name> になる。"""
    from gui.chat import _simplify_tool_text
    text = f"{CALL_A}\n{RES_A}"
    out = _simplify_tool_text(text, "compact")
    tool_lines = [ln for ln in out.split("\n") if ln.startswith("🔧")]
    assert tool_lines == ["🔧 read_file"]


def test_simplify_t4_hidden_removes_run():
    """T4: hidden removes a run (incl. absorbed blank) entirely — no summary
    line — leaving the surrounding prose separated by a single blank line."""
    from gui.chat import _simplify_tool_text
    text = "prose\n🔧 A\n   ↳ rA\n\n🔧 B\n   ↳ rB\nprose2"
    out = _simplify_tool_text(text, "hidden")
    assert out == "prose\n\nprose2"


def test_simplify_t5_hidden_tool_only_is_empty():
    """T5: a tool-only run under hidden collapses to the empty string."""
    from gui.chat import _simplify_tool_text
    text = "🔧 A\n   ↳ rA\n🔧 B\n   ↳ rB\n🔧 C\n   ↳ rC"
    out = _simplify_tool_text(text, "hidden")
    assert out == ""


def test_simplify_hidden_run_at_start_no_leading_blank():
    from gui.chat import _simplify_tool_text
    text = f"{CALL_A}\n{RES_A}\nprose"
    assert _simplify_tool_text(text, "hidden") == "prose"


def test_simplify_hidden_run_at_end_no_trailing_blank():
    from gui.chat import _simplify_tool_text
    text = f"prose\n{CALL_A}\n{RES_A}"
    assert _simplify_tool_text(text, "hidden") == "prose"


def test_simplify_hidden_runs_between_prose_single_blanks():
    """Each removed run leaves a single blank line — never a doubled gap."""
    from gui.chat import _simplify_tool_text
    text = f"a\n{CALL_A}\n{RES_A}\nb\n{CALL_B}\n{RES_B}\nc"
    out = _simplify_tool_text(text, "hidden")
    assert out == "a\n\nb\n\nc"
    assert "\n\n\n" not in out


def test_simplify_t6_false_positive_in_code_block():
    """T6: marker-like lines INSIDE a fenced code block are (intentionally,
    documented limitation) misclassified as tool lines and removed/dropped.
    Pin the lossy behavior so it stays deliberate, not accidental."""
    from gui.chat import _simplify_tool_text
    text = "Here is code:\n```\n🔧 not_really_a_tool\n   ↳ also_not\n```"
    # hidden: the in-fence marker lines are removed entirely (false positive)
    hidden_lines = _simplify_tool_text(text, "hidden").split("\n")
    assert "🔧 not_really_a_tool" not in hidden_lines
    assert "   ↳ also_not" not in hidden_lines
    assert not any("tool calls" in ln for ln in hidden_lines)
    assert hidden_lines.count("```") == 2  # surrounding fence survives; content removed
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


# ---- N 段（多段）セッションタブ：MultiRowTabBar 採用（Issue #41） ----


def test_session_tab_bar_is_multirow(widget):
    """セッションタブは MultiRowTabBar。×ボタン廃止（閉じるはメニュー）・横スクロール
    廃止（多段で折り返す）。"""
    from gui.tabbar import MultiRowTabBar
    assert isinstance(widget._tab_bar, MultiRowTabBar)
    assert widget._tab_bar.usesScrollButtons() is False
    assert widget._tab_bar.tabsClosable() is False
    assert widget._tab_bar.isMovable() is True


def test_session_tabs_wrap_when_dock_narrow(widget, qapp):
    """実 ChatWidget を狭めるとセッションタブが多段に折り返す（QTabWidget を介さない
    素のレイアウト直置きでも sizeHint 高さが伝播することの担保）。"""
    from llm_bridge import chat_store
    widget._sessions = [
        chat_store.new_session("mock", "sys", dataset="ds", title=f"セッション{i:02d}")
        for i in range(14)
    ]
    widget._current_dataset = "ds"
    widget._active = widget._sessions[0]
    widget._rebuild_tab_bar()
    widget.resize(220, 600)
    widget.show()
    qapp.processEvents()
    try:
        assert widget._tab_bar.count() == 14
        assert widget._tab_bar._row_count > 1
    finally:
        widget.hide()


def test_session_tab_bar_does_not_inflate_dock(qapp):
    """多数セッションを復元してもチャットタブバーの sizeHint 幅がドックを膨張させない
    （compact_width_hint=True）。バーは実ドック幅いっぱいに広がり多段折り返しするが、
    自然幅ヒントで中央の解析パネルを圧殺しないこと。"""
    from gui.window import ToolWindow
    from gui.chat import ChatWidget
    from llm_bridge import chat_store

    win = ToolWindow()
    chat = ChatWidget(_FakeBackend, dispatch=lambda *a, **k: None)
    win.set_chat_widget(chat)
    chat._current_dataset = None
    chat._sessions = [
        chat_store.new_session(
            "mock", "sys", dataset=None,
            title=f"これは長めのセッションタイトルです {i:02d}",
        )
        for i in range(20)
    ]
    chat._active = chat._sessions[0]
    chat._rebuild_tab_bar()

    win.show()
    qapp.processEvents()
    win.resize(1400, 800)
    qapp.processEvents()
    try:
        assert chat._tab_bar.sizeHint().width() <= 200
        assert win._chat_dock.width() <= 600
        assert win.centralWidget().width() >= 600
        assert chat._tab_bar._row_count > 1
    finally:
        win.hide()


def test_close_from_menu_confirms_then_deletes(widget, monkeypatch):
    """「閉じる」が呼ぶ _on_delete_session は確認ダイアログ Yes でセッションを削除する。"""
    from gui.chat import QMessageBox

    sess = _make_session(widget, dataset="ds", title="閉じる対象")
    idx = _tab_index_for(widget, sess)
    before = len(widget._sessions)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *a, **k: QMessageBox.StandardButton.Yes,
    )
    widget._on_delete_session(idx)
    assert sess not in widget._sessions
    assert len(widget._sessions) == before - 1


def test_close_from_menu_cancel_keeps_session(widget, monkeypatch):
    """確認ダイアログ No なら削除しない。"""
    from gui.chat import QMessageBox

    sess = _make_session(widget, dataset="ds", title="残す対象")
    idx = _tab_index_for(widget, sess)
    before = len(widget._sessions)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *a, **k: QMessageBox.StandardButton.No,
    )
    widget._on_delete_session(idx)
    assert sess in widget._sessions
    assert len(widget._sessions) == before


# ---- archive / unarchive (Issue #93) ----


def _with_history(sess):
    """Give a session a real (non-system) message so _can_archive accepts it."""
    from llm_backend.base import Message
    sess.messages.append(Message(role="user", content="hi"))
    return sess


def test_archive_hides_tab_but_keeps_session(widget, monkeypatch):
    from gui.chat import QMessageBox

    # archive は無確認 — question が呼ばれたら fail。
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *a, **k: pytest.fail("archive must not confirm"),
    )
    sess = _with_history(_make_session(widget, dataset="ds", title="対象"))
    widget._active = sess
    widget._rebuild_tab_bar()
    before = sess.updated
    idx = _tab_index_for(widget, sess)

    widget._on_archive_session(idx)

    assert sess.archived is True
    assert sess in widget._sessions            # 消さない
    assert sess not in widget._visible_sessions()   # タブから隠れる
    assert _tab_index_for(widget, sess) == -1
    assert not widget._deleted                  # tombstone なし
    assert sess.updated > before
    assert widget._window.mark_chat_dirty.call_count >= 1


def test_archive_active_falls_back_to_blank(widget):
    # dataset に他チャットが無い状態で唯一のアクティブをアーカイブ → blank 補充。
    sess = _with_history(_make_session(widget, dataset="ds", title="唯一"))
    # 開いていた自動 blank を除去して sess のみ可視にする。
    widget._sessions = [sess]
    widget._active = sess
    widget._rebuild_tab_bar()
    idx = _tab_index_for(widget, sess)

    widget._on_archive_session(idx)

    assert widget._active is not sess
    assert widget._active.dataset is None       # dataset 非依存 blank
    assert not widget._has_history(widget._active)


def test_archive_active_falls_back_to_sibling(widget):
    from llm_bridge import chat_store

    a = _with_history(
        chat_store.new_session("mock", "sys", dataset="ds", title="A"))
    b = chat_store.new_session("mock", "sys", dataset="ds", title="B")
    widget._sessions = [a, b]
    widget._active = a
    widget._current_dataset = "ds"
    widget._rebuild_tab_bar()
    idx = _tab_index_for(widget, a)

    widget._on_archive_session(idx)

    assert a.archived is True
    assert widget._active is b


def test_archive_nonactive_keeps_active(widget):
    from llm_bridge import chat_store

    a = chat_store.new_session("mock", "sys", dataset="ds", title="A")
    b = _with_history(
        chat_store.new_session("mock", "sys", dataset="ds", title="B"))
    widget._sessions = [a, b]
    widget._active = a
    widget._current_dataset = "ds"
    widget._rebuild_tab_bar()
    idx = _tab_index_for(widget, b)

    widget._on_archive_session(idx)

    assert b.archived is True
    assert widget._active is a                  # 非アクティブアーカイブは active 不変


def test_can_archive_guards(widget):
    from llm_bridge import chat_store

    # unbound (dataset None) — 履歴あっても不可。
    unbound = _with_history(_make_session(widget, dataset=None))
    assert not widget._can_archive(unbound)

    # 履歴なし — 不可。
    nohist = _make_session(widget, dataset="ds")
    assert not widget._can_archive(nohist)

    # busy (_turns 在中) — 不可。
    busy = _with_history(
        chat_store.new_session("mock", "sys", dataset="ds", title="busy"))
    widget._sessions.append(busy)
    widget._turns[busy.id] = object()
    assert not widget._can_archive(busy)

    # 既にアーカイブ済み — 不可。
    arch = _with_history(
        chat_store.new_session("mock", "sys", dataset="ds", title="a"))
    arch.archived = True
    assert not widget._can_archive(arch)


def test_archive_handler_noop_when_guard_fails(widget):
    # 履歴なし → ハンドラは no-op（archived を立てない）。
    sess = _make_session(widget, dataset="ds")
    widget._active = sess
    widget._rebuild_tab_bar()
    idx = _tab_index_for(widget, sess)
    widget._on_archive_session(idx)
    assert sess.archived is False


def test_unarchive_reshows_and_activates(widget):
    from llm_bridge import chat_store

    keep = _with_history(
        chat_store.new_session("mock", "sys", dataset="ds", title="keep"))
    arch = _with_history(
        chat_store.new_session("mock", "sys", dataset="ds", title="arch"))
    arch.archived = True
    widget._sessions = [keep, arch]
    widget._active = keep
    widget._current_dataset = "ds"
    widget._rebuild_tab_bar()
    before = arch.updated

    widget._on_unarchive_session(arch.id)

    assert arch.archived is False
    assert arch in widget._visible_sessions()
    assert widget._active is arch
    assert widget._last_active_by_ds["ds"] == arch.id
    assert arch.updated > before
    assert widget._window.mark_chat_dirty.call_count >= 1


def test_tab_moved_reorders_with_archived_present(widget):
    """slots 方式の _on_tab_moved が archived 混在でも可視のみ並べ替える回帰ロック。"""
    from llm_bridge import chat_store

    a = _with_history(chat_store.new_session("mock", "sys", dataset="ds", title="A"))
    arch = _with_history(chat_store.new_session("mock", "sys", dataset="ds", title="X"))
    arch.archived = True
    b = chat_store.new_session("mock", "sys", dataset="ds", title="B")
    c = chat_store.new_session("mock", "sys", dataset="ds", title="C")
    widget._sessions = [a, arch, b, c]
    widget._current_dataset = "ds"
    widget._active = a
    widget._rebuild_tab_bar()
    # Visible tabs: [A, B, C]; archived X hidden. Drag C (2) → front (0).
    widget._tab_bar.moveTab(2, 0)

    assert [s.title for s in widget._visible_sessions()] == ["C", "A", "B"]
    assert arch in widget._sessions             # archived は pool に残る


def test_session_summaries_includes_archived(widget):
    from llm_bridge import chat_store

    arch = _with_history(
        chat_store.new_session("mock", "sys", dataset="ds", title="a"))
    arch.archived = True
    widget._sessions.append(arch)
    summ = {s["id"]: s for s in widget.session_summaries()}
    assert summ[arch.id]["archived"] is True


def test_set_active_session_by_id_ignores_archived(widget):
    from llm_bridge import chat_store

    keep = _make_session(widget, dataset="ds", title="keep")
    widget._active = keep
    arch = _with_history(
        chat_store.new_session("mock", "sys", dataset="ds", title="a"))
    arch.archived = True
    widget._sessions.append(arch)

    widget.set_active_session_by_id(arch.id)
    assert widget._active is keep               # archived への切替は no-op


def test_merge_incoming_archived_hides_and_redraws(widget):
    """同期で archived=True(updated 新)の同 id が来たら非表示化＋新 active に再描画。"""
    from llm_bridge import chat_store
    from llm_backend.base import Message

    a = _with_history(
        chat_store.new_session("mock", "sys", dataset="ds", title="A"))
    b = _with_history(
        chat_store.new_session("mock", "sys", dataset="ds", title="B"))
    # b に固有の本文を持たせる。_render_session は messages を描画し title は描かない
    # ので、この文字列が transcript に出るかどうかが「新 active(B) を再描画したか」を
    # 弁別する。a の "hi" とも区別できる。
    b.messages.append(Message(role="user", content="uniqB-transcript"))
    widget._sessions = [a, b]
    widget._active = a
    widget._current_dataset = "ds"
    widget._rebuild_tab_bar()

    # incoming: 同 id a のアーカイブ済みコピー（updated 新）。
    incoming = chat_store.new_session("mock", "sys", dataset="ds", title="A")
    incoming.id = a.id
    incoming.messages.append(Message(role="user", content="hi"))
    incoming.archived = True
    incoming.updated = a.updated + 10.0

    widget.merge_dataset_sessions("ds", [incoming])

    merged = widget._session_by_id(a.id)
    assert merged.archived is True
    assert widget._active is not merged         # active 付け替え
    assert widget._active is widget._session_by_id(b.id)
    # transcript が新 active(B) を映しているか（3.10 の再描画をロック）。b 固有本文で
    # 検査する: merge の `if self._active.id != prev_id: self._render_session(...)` を
    # 外すと _log は旧 active(a) のままで "uniqB-transcript" は出ず fail する。
    assert "uniqB-transcript" in widget._log.toPlainText()


def test_archived_chats_dialog_rows_and_selection(qapp):
    from gui.chat import _ArchivedChatsDialog

    rows = [
        ("新しい方", "2026-07-28 10:00", "id-new"),
        ("古い方", "2026-07-01 09:00", "id-old"),
    ]
    dlg = _ArchivedChatsDialog(rows)
    assert dlg._table.rowCount() == 2
    assert dlg._table.item(0, 0).text() == "新しい方"
    assert dlg._table.item(0, 1).text() == "2026-07-28 10:00"
    assert dlg._table.item(1, 0).text() == "古い方"
    # 選択なし → None。
    assert dlg.selected_sid() is None
    dlg._table.selectRow(1)
    assert dlg.selected_sid() == "id-old"


def test_archived_sessions_sorted_updated_desc(widget):
    from llm_bridge import chat_store

    older = _with_history(
        chat_store.new_session("mock", "sys", dataset="ds", title="old"))
    older.archived = True
    older.updated = 100.0
    newer = _with_history(
        chat_store.new_session("mock", "sys", dataset="ds", title="new"))
    newer.archived = True
    newer.updated = 200.0
    widget._sessions = [older, newer]
    widget._current_dataset = "ds"

    got = widget._archived_sessions()
    assert [s.title for s in got] == ["new", "old"]


# ---- live streaming respects the display mode (Issue: hide tool calls live) ----

def _start_inflight(widget, *, mode_default):
    """Mimic _on_send's document setup for the active session and register a
    _Turn so _on_chunk can drive it, without a real worker thread. Returns
    (session, turn). Caller should pop the turn from widget._turns at the end."""
    from gui.chat import _Turn
    widget._tool_display_default = mode_default
    sess = widget._active
    widget._append_block("assistant", "")
    turn = _Turn(sess, _FakeBackend(), MagicMock(), MagicMock())
    turn.anchor = widget._log.document().characterCount() - 1
    widget._turns[sess.id] = turn
    return sess, turn


def test_live_hidden_suppresses_tool_lines(widget):
    """Streaming chunks containing 🔧/↳ lines never reach the document in
    hidden mode; surrounding prose does."""
    sess, _ = _start_inflight(widget, mode_default="hidden")
    sid = sess.id
    widget._on_chunk(sid, "thinking\n")
    widget._on_chunk(sid, "🔧 read_file  path=a.py\n")
    widget._on_chunk(sid, "   ↳ ok\n")
    widget._on_chunk(sid, "done")
    widget._flush_live_markdown()           # タイマーを待たず決定的に flush
    text = widget._log.toPlainText()
    assert "🔧" not in text and "↳" not in text
    assert "thinking" in text and "done" in text
    widget._turns.pop(sid)


def test_live_full_keeps_tool_lines(widget):
    """full mode still streams tool-call lines verbatim."""
    sess, _ = _start_inflight(widget, mode_default="full")
    sid = sess.id
    widget._on_chunk(sid, "🔧 read_file  path=a.py\n")
    widget._flush_live_markdown()           # タイマーを待たず決定的に flush
    assert "🔧 read_file" in widget._log.toPlainText()
    widget._turns.pop(sid)


def test_live_hidden_prose_appended_once(widget):
    """全置換再描画は末尾プロサを重複させない（チャンク追加で二重描画しない）。"""
    sess, _ = _start_inflight(widget, mode_default="hidden")
    sid = sess.id
    widget._on_chunk(sid, "🔧 read_file  path=a.py\n")
    widget._on_chunk(sid, "   ↳ ok\n")
    widget._on_chunk(sid, "Hello ")
    widget._on_chunk(sid, "world")
    widget._flush_live_markdown()           # タイマーを待たず決定的に flush
    text = widget._log.toPlainText()
    assert text.count("Hello world") == 1
    assert "🔧" not in text
    widget._turns.pop(sid)


def test_render_session_inflight_hidden_filters(widget):
    """A tab-switch-back redraw of an in-flight turn applies hidden mode and
    re-anchors so subsequent chunks keep filtering."""
    from gui.chat import _Turn
    widget._tool_display_default = "hidden"
    sess = widget._active
    turn = _Turn(sess, _FakeBackend(), MagicMock(), MagicMock())
    turn.buffer = "intro\n🔧 read_file  path=a.py\n   ↳ ok\ntail"
    widget._turns[sess.id] = turn
    widget._render_session(sess)
    text = widget._log.toPlainText()
    assert "🔧" not in text
    assert "intro" in text and "tail" in text
    assert turn.anchor is not None
    widget._on_chunk(sess.id, "\n🔧 write_file  path=b.py\n")
    widget._flush_live_markdown()           # 新方式では _on_chunk が描画しないため明示 flush
    assert "🔧" not in widget._log.toPlainText()
    widget._turns.pop(sess.id)


def test_live_markdown_applied_during_streaming(widget):
    """ストリーミング中（完了前）でも in-flight 本文が Markdown 整形される。"""
    sess, _ = _start_inflight(widget, mode_default="full")
    sid = sess.id
    widget._on_chunk(sid, "**bold**")
    widget._flush_live_markdown()           # タイマーを待たず決定的に flush
    text = widget._log.toPlainText()
    assert "bold" in text and "**" not in text   # 完了前に既に Markdown 整形済み
    widget._turns.pop(sid)


def test_live_markdown_matches_completion_across_flushes(widget):
    """見出し/リスト/コードブロック/段落を跨いで段階的に伸びる本文を複数回 flush した
    最終 HTML が、同一最終本文を完了時パス（1 発 Markdown 描画）で描いた HTML と一致する。"""
    pieces = ("Title\n", "=====\n\n- a\n", "- b\n\n```\nx\n```\n", "\ntail")
    final = "".join(pieces)

    def render_staged():
        widget._log.clear()
        sess, turn = _start_inflight(widget, mode_default="full")  # clear 後の fresh ブロックへ
        for piece in pieces:
            turn.buffer += piece
            widget._flush_live_markdown()
        html = widget._log.toHtml()
        widget._turns.pop(sess.id)
        return html

    def render_completion():
        widget._log.clear()
        widget._append_block("assistant", final, markdown=True)  # 完了時本文描画と同一パス（素ヘッダ）
        return widget._log.toHtml()

    assert render_staged() == render_completion()


def test_live_flush_defers_while_user_selecting(widget):
    """ログ内テキスト選択中は _flush_live_markdown が描画を保留し、選択を保持する。"""
    from PySide6.QtGui import QTextCursor
    sess, turn = _start_inflight(widget, mode_default="full")
    sid = sess.id
    turn.buffer = "hello world"
    widget._flush_live_markdown()                 # 一旦描画
    cur = widget._log.textCursor()
    cur.select(QTextCursor.SelectionType.Document) # ユーザー選択を模す
    widget._log.setTextCursor(cur)
    assert widget._log.textCursor().hasSelection()
    turn.buffer += " more"
    widget._flush_live_markdown()                 # 選択中 → 保留（no-op 描画）
    assert widget._log.textCursor().hasSelection()  # 選択は保持されている
    widget._turns.pop(sid)


def test_completed_tool_only_message_no_bare_header(widget):
    """A completed tool-only assistant message draws no bare 'assistant' header
    in hidden mode (its body simplifies to empty)."""
    from llm_backend.base import Message
    widget._tool_display_default = "hidden"
    sess = widget._active
    sess.messages.append(
        Message(role="assistant", content="🔧 read_file  path=a.py\n   ↳ ok")
    )
    widget._render_session(sess)
    text = widget._log.toPlainText()
    assert "assistant" not in text
    assert "🔧" not in text


# ---- fork / edit (Issue #63) ----


def _make_active_with_messages(widget, *, dataset=None, title="元のタイトル"):
    """アクティブなセッションに [system, user, assistant, user, assistant] を仕込む。"""
    from llm_backend.base import Message
    from llm_bridge import chat_store

    sess = chat_store.new_session("mock", "sys", dataset=dataset, title=title)
    sess.messages = [
        Message(role="system", content="sys"),
        Message(role="user", content="u1"),
        Message(role="assistant", content="a1"),
        Message(role="user", content="u2"),
        Message(role="assistant", content="a2"),
    ]
    widget._sessions.append(sess)
    widget._current_dataset = dataset
    widget._active = sess
    widget._rebuild_tab_bar()
    return sess


def test_edit_forks_and_prefills_single_message(widget):
    src = _make_active_with_messages(widget, dataset="ds")
    src_msgs = list(src.messages)
    before = len(widget._sessions)
    widget._handle_chat_action("edit", 3)  # user u2
    assert len(widget._sessions) == before + 1
    assert widget._active is not src
    assert widget._active.messages == src_msgs[:3]
    assert widget._active.backend_session_id is None
    assert widget.input_draft() == "u2"
    assert src.messages == src_msgs  # 元は無改変


def test_fork_includes_assistant_and_empty_draft(widget):
    src = _make_active_with_messages(widget, dataset="ds")
    src_msgs = list(src.messages)
    widget._handle_chat_action("fork", 2)  # assistant a1
    assert widget._active.messages == src_msgs[:3]
    assert widget.input_draft() == ""
    assert src.messages == src_msgs


def test_fork_title_suffix(widget):
    from common.i18n import tr

    _make_active_with_messages(widget, dataset="ds", title="my chat")
    widget._handle_chat_action("fork", 2)
    assert widget._active.title == "my chat" + tr("chat.fork.title_suffix")


def test_fork_title_suffix_not_doubled(widget):
    from common.i18n import tr

    suffix = tr("chat.fork.title_suffix")
    _make_active_with_messages(widget, dataset="ds", title="my chat" + suffix)
    widget._handle_chat_action("fork", 2)
    assert widget._active.title == "my chat" + suffix


def test_edit_preserves_source_draft(widget, monkeypatch):
    """下書きが per-tab になったので、分岐は分岐元の未送信テキストを破棄しない
    → かつての破棄確認ダイアログは廃止。確認なしで分岐し、src.draft は保持される。"""
    from gui.chat import QMessageBox

    # 確認ダイアログが復活したら（No で分岐が阻止され）このテストが落ちるように、
    # question は常に No を返すようにしておく。
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No
    )
    src = _make_active_with_messages(widget, dataset="ds")
    widget.set_input_draft("unsent")
    before = len(widget._sessions)

    widget._handle_chat_action("edit", 3)  # user u2

    assert len(widget._sessions) == before + 1
    assert widget._active is not src
    assert widget.input_draft() == "u2"     # 新タブは prefill
    assert src.draft == "unsent"            # 分岐元の下書きは保持される


def test_edit_scratch_first_message_visible(widget):
    # dataset に bound チャットがある状態で、scratch(None) の最初の user 発言を編集。
    _make_active_with_messages(widget, dataset="ds", title="bound")
    from llm_backend.base import Message
    from llm_bridge import chat_store

    scratch = chat_store.new_session("mock", "sys", dataset=None)
    scratch.messages = [
        Message(role="system", content="sys"),
        Message(role="user", content="uX"),
    ]
    widget._sessions.append(scratch)
    widget._current_dataset = "ds"
    widget._active = scratch
    widget._rebuild_tab_bar()

    widget._handle_chat_action("edit", 1)
    assert widget._active in widget._visible_sessions()
    assert widget._active.dataset == "ds"


def test_action_role_and_range_guards(widget):
    _make_active_with_messages(widget, dataset="ds")
    before = len(widget._sessions)
    widget._handle_chat_action("edit", 2)     # assistant index → no-op
    widget._handle_chat_action("fork", 1)     # user index → no-op
    widget._handle_chat_action("edit", 99)    # out of range
    widget._handle_chat_action("fork", -1)    # negative
    assert len(widget._sessions) == before


def test_action_content_none_user_guard(widget):
    from llm_backend.base import Message

    src = _make_active_with_messages(widget, dataset="ds")
    src.messages.append(Message(role="user", content=None))
    idx = len(src.messages) - 1
    before = len(widget._sessions)
    widget._handle_chat_action("edit", idx)
    assert len(widget._sessions) == before


def test_action_in_flight_allows_fork(widget):
    """生成中でも編集/分岐は許可され、元セッションの生成は背景で継続する（#76）。"""
    src = _make_active_with_messages(widget, dataset="ds")
    widget._turns[src.id] = MagicMock()          # src を生成中に見せる
    before = len(widget._sessions)
    widget._handle_chat_action("edit", 3)        # user u2 を編集
    assert len(widget._sessions) == before + 1   # 分岐が実行される
    assert widget._active is not src
    assert src.id in widget._turns               # 元の生成は背景に残る（Stop しない）
    del widget._turns[src.id]


# ---- per-tab draft: composer はタブに紐づく ----


def _add_scratch(widget, title):
    from llm_bridge import chat_store

    sess = chat_store.new_session("mock", "sys", dataset=None, title=title)
    widget._sessions.append(sess)
    widget._rebuild_tab_bar()
    return sess


def test_draft_is_per_tab(widget):
    """A に書きかけのままタブ切替 → B は空。戻ると A の下書きが復元し、B も自分の
    下書きを保持する（タブ間で別々の内容を自在なタイミングで送れる）。"""
    a = widget._active
    b = _add_scratch(widget, "B")

    widget.set_input_draft("draft A")
    widget._tab_bar.setCurrentIndex(_tab_index_for(widget, b))  # → _on_switch_session
    assert widget._active is b
    assert widget.input_draft() == ""

    widget.set_input_draft("draft B")
    widget._tab_bar.setCurrentIndex(_tab_index_for(widget, a))
    assert widget._active is a
    assert widget.input_draft() == "draft A"

    widget._tab_bar.setCurrentIndex(_tab_index_for(widget, b))
    assert widget.input_draft() == "draft B"


def test_switch_to_same_tab_keeps_live_composer(widget):
    """同一タブの再選択で、まだ退避していないライブ composer を潰さない。"""
    a = widget._active
    widget.set_input_draft("typing...")
    widget._on_switch_session(_tab_index_for(widget, a))
    assert widget.input_draft() == "typing..."


def test_new_session_clears_composer_and_keeps_old_draft(widget):
    a = widget._active
    widget.set_input_draft("draft A")
    widget._on_new_session()
    assert widget._active is not a
    assert widget.input_draft() == ""     # 新規タブは空
    assert a.draft == "draft A"           # 旧タブの下書きは退避済み


def test_send_clears_active_draft(widget, monkeypatch):
    """送信したテキストが後の退避/読込で復活しないこと。"""
    monkeypatch.setattr(widget, "_start_turn", lambda *a, **k: None)
    widget.set_input_draft("hello")
    widget._on_send()
    assert widget.input_draft() == ""
    assert widget._active.draft == ""


def test_delete_nonactive_tab_keeps_live_composer(widget, monkeypatch):
    from gui.chat import QMessageBox

    a = widget._active
    b = _add_scratch(widget, "B")
    widget.set_input_draft("typing in A")
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    widget._on_delete_session(_tab_index_for(widget, b))
    assert widget._active is a
    assert widget.input_draft() == "typing in A"


def test_delete_active_tab_loads_remaining_draft(widget, monkeypatch):
    from gui.chat import QMessageBox

    a = widget._active
    b = _add_scratch(widget, "B")
    # A に下書きを残してから B へ移り、B をアクティブのまま削除する。
    widget.set_input_draft("draft A")
    widget._tab_bar.setCurrentIndex(_tab_index_for(widget, b))
    widget.set_input_draft("draft B (doomed)")
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    widget._on_delete_session(_tab_index_for(widget, b))
    assert widget._active is a
    assert widget.input_draft() == "draft A"   # 残ったタブの下書きが読込まれる


def test_merge_preserves_drafts_across_instance_replacement(widget):
    """merge は同一 id セッションを disk の新しいコピー（draft は非永続なので空）へ
    差し替え得る。アクティブ/非アクティブどちらの未送信下書きも消えないこと。"""
    import copy

    from llm_backend.base import Message
    from llm_bridge import chat_store

    a = chat_store.new_session("mock", "sys", dataset="ds", title="A")
    a.messages.append(Message(role="user", content="hi"))
    b = chat_store.new_session("mock", "sys", dataset="ds", title="B")
    b.messages.append(Message(role="user", content="yo"))
    widget._sessions = [a, b]
    widget._active = a
    widget._current_dataset = "ds"
    widget._rebuild_tab_bar()

    b.draft = "important unsent text in B"      # 非アクティブタブの下書き
    widget.set_input_draft("live text in A")    # アクティブはまだ未退避

    # 別 PC が同期した strictly-newer な同一 id コピー（draft="" で来る）
    a2 = copy.deepcopy(a)
    a2.draft = ""
    a2.updated = a.updated + 10
    b2 = copy.deepcopy(b)
    b2.draft = ""
    b2.updated = b.updated + 10

    widget.merge_dataset_sessions("ds", [a2, b2])

    assert widget._active.id == a.id
    assert widget.input_draft() == "live text in A"          # アクティブの composer は無傷
    assert widget._session_by_id(a.id).draft == "live text in A"
    assert widget._session_by_id(b.id).draft == "important unsent text in B"


def test_dataset_switch_preserves_per_tab_drafts(widget):
    """DS 切替でも下書きはタブに残る（#51 の DS 別セッション群と両立）。"""
    from llm_bridge import chat_store
    from llm_backend.base import Message

    a = chat_store.new_session("mock", "sys", dataset="A", title="chatA")
    a.messages.append(Message(role="user", content="hi"))
    b = chat_store.new_session("mock", "sys", dataset="B", title="chatB")
    b.messages.append(Message(role="user", content="yo"))
    widget._sessions = [a, b]
    widget._active = a
    widget._current_dataset = "A"
    widget._rebuild_tab_bar()

    widget.set_input_draft("draft for A")
    widget.set_current_dataset("B")
    assert widget._active is b
    assert widget.input_draft() == ""
    widget.set_input_draft("draft for B")

    widget.set_current_dataset("A")
    assert widget._active is a
    assert widget.input_draft() == "draft for A"
    assert b.draft == "draft for B"


# ---- 履歴/入力のリサイズ（縦 QSplitter） ----


def test_log_and_input_share_a_vertical_splitter(widget):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QSplitter

    assert isinstance(widget._io_split, QSplitter)
    assert widget._io_split.orientation() == Qt.Orientation.Vertical
    assert widget._io_split.count() == 2
    assert widget._log.parent() is widget._io_split
    assert widget._input.parent() is widget._io_split
    # 潰れ防止: どちらのペインもドラッグで 0 高にできない
    assert widget._io_split.childrenCollapsible() is False


def test_input_height_is_resizable_not_fixed(widget):
    """setFixedHeight(80) 廃止の担保: 入力欄の高さは splitter で可変。"""
    assert widget._input.minimumHeight() < widget._input.maximumHeight()


def test_splitter_move_debounces_then_persists_state(widget, monkeypatch):
    """ドラッグ中の連続発火では書かず（タイマーへ集約）、発火後に 1 回だけ保存する。"""
    import llm_bridge.paths as paths

    saved = {}
    monkeypatch.setattr(paths, "update_ui_pref",
                        lambda k, v: saved.__setitem__(k, v))

    for _ in range(20):                      # ドラッグ中の連続 splitterMoved を模す
        widget._on_io_split_moved()
    assert saved == {}                       # まだ 1 回も書いていない
    assert widget._split_save_timer.isActive()

    widget._flush_split_state()              # タイマー発火を模す
    assert isinstance(saved.get("chat_io_split"), str) and saved["chat_io_split"]


def test_splitter_state_hex_roundtrip(widget):
    """保存(_on_io_split_moved)↔復元(__init__)で使う hex エンコードの往復が成立する
    ＝保存した比率を restoreState が受理する。"""
    from PySide6.QtCore import QByteArray

    widget._io_split.setSizes([500, 100])
    hexstate = bytes(widget._io_split.saveState().toHex()).decode("ascii")
    ok = widget._io_split.restoreState(QByteArray.fromHex(bytes(hexstate, "ascii")))
    assert ok is True


def test_load_chat_split_invalid_types_never_raise(monkeypatch):
    """手編集/破損した ui_prefs の chat_io_split がどんな型でも "" に正規化される。"""
    import llm_bridge.paths as paths
    from gui.chat import _load_chat_split

    # 型違い / 非 hex / 非 ASCII はすべて "" へ正規化。とくに非 ASCII は
    # bytes(v,"ascii") が UnicodeEncodeError を投げるので loader で止める。
    for bad in ([], {}, ["x"], {"x": 1}, 0, 1.5, True, None, "zzzz", "あいうえお", "de ad"):
        monkeypatch.setattr(paths, "read_ui_pref", lambda *a, _b=bad, **k: _b)
        assert _load_chat_split() == ""
    monkeypatch.setattr(paths, "read_ui_pref", lambda *a, **k: "abc")
    assert _load_chat_split() == "abc"   # 有効 hex はそのまま通す


def test_non_ascii_saved_split_state_does_not_crash_construction(qapp, monkeypatch):
    """手編集で非 ASCII が入っても ChatWidget.__init__ が落ちない（dock 全滅を防ぐ）。"""
    import llm_bridge.paths as paths
    from gui.chat import ChatWidget

    monkeypatch.setattr(paths, "read_ui_pref",
                        lambda k, d=None: "あいうえお" if k == "chat_io_split" else d)
    w = ChatWidget(_FakeBackend, dispatch=lambda *a, **k: None)   # must not raise
    assert w._io_split.count() == 2
    assert all(s > 0 for s in w._io_split.sizes())


def test_bad_saved_split_state_falls_back_to_defaults(qapp, monkeypatch):
    """非 hex / 途中で切れた state でも例外を投げず、2 ペイン構成で立ち上がる。"""
    import llm_bridge.paths as paths
    from gui.chat import ChatWidget

    for bad in ("zzzz", "6e6f74616", "deadbeef"):
        monkeypatch.setattr(paths, "read_ui_pref",
                            lambda k, d=None, _b=bad: _b if k == "chat_io_split" else d)
        w = ChatWidget(_FakeBackend, dispatch=lambda *a, **k: None)   # must not raise
        assert w._io_split.count() == 2
        assert all(s > 0 for s in w._io_split.sizes())


def test_anchor_clicked_routes_chataction(widget, monkeypatch):
    from PySide6.QtCore import QUrl

    calls = []
    monkeypatch.setattr(widget, "_handle_chat_action",
                        lambda a, i: calls.append((a, i)))
    widget._log.anchorClicked.emit(QUrl("chataction:edit:2"))
    assert calls == [("edit", 2)]


def test_anchor_clicked_external_url(widget, monkeypatch):
    from PySide6.QtCore import QUrl
    from gui.chat import QDesktopServices

    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda u: opened.append(u))
    calls = []
    monkeypatch.setattr(widget, "_handle_chat_action",
                        lambda a, i: calls.append((a, i)))
    widget._log.anchorClicked.emit(QUrl("http://example.com"))
    assert calls == []
    assert len(opened) == 1


def test_render_session_emits_chataction_anchors(widget):
    """生成側の突合: _render_session が user 行に chataction:edit:<i>、本文あり
    assistant 行に chataction:fork:<j> を正しい index で描くこと。クリック側テスト
    (test_anchor_clicked_routes_chataction) は QUrl を直接 emit して _append_block/
    _render_session を迂回するため、生成 href 書式がパーサとずれても緑になる盲点を塞ぐ
    (reviewer P2)。"""
    src = _make_active_with_messages(widget, dataset="ds")
    widget._render_session(src)
    html = widget._log.toHtml()
    # messages = [system(0), user(1)=u1, assistant(2)=a1, user(3)=u2, assistant(4)=a2]
    assert "chataction:edit:1" in html
    assert "chataction:edit:3" in html
    assert "chataction:fork:2" in html
    assert "chataction:fork:4" in html
    # role↔action の取り違え・system 行へのリンク付与が無いこと
    assert "chataction:fork:1" not in html
    assert "chataction:edit:2" not in html
    assert "chataction:edit:0" not in html and "chataction:fork:0" not in html


# ---- Issue #81: guest-requested new chat session (meeting relay) ----

def test_create_remote_session_binds_dataset(widget):
    sess = _make_session(widget, dataset="ds")
    widget._active = sess
    before = len(widget._sessions)

    sid = widget.create_remote_session("ds")

    assert len(widget._sessions) == before + 1
    new = next(s for s in widget._sessions if s.id == sid)
    assert new.dataset == "ds"
    assert len(new.messages) == 1
    assert new.messages[0].role == "system"


def test_create_remote_session_keeps_active_and_composer(widget):
    sess = _make_session(widget, dataset="ds")
    widget._active = sess
    widget._input.setPlainText("typing...")

    widget.create_remote_session("ds")

    assert widget._active.id == sess.id
    assert widget.input_draft() == "typing..."


def test_create_remote_session_adopts_hidden_blank_active(widget):
    from llm_bridge import chat_store

    blank = chat_store.new_session("mock", "sys", dataset=None)
    widget._sessions = [blank]
    widget._active = blank
    widget._current_dataset = "ds"
    widget._rebuild_tab_bar()
    widget._input.setPlainText("typing...")

    sid = widget.create_remote_session("ds")

    assert widget._active.id == sid
    assert widget._last_active_by_ds["ds"] == sid
    assert widget.input_draft() == "typing..."


def test_create_remote_session_does_not_mark_dirty(widget):
    sess = _make_session(widget, dataset="ds")
    widget._active = sess
    widget._window.mark_chat_dirty.reset_mock()

    widget.create_remote_session("ds")

    assert widget._window.mark_chat_dirty.call_count == 0


# ----- backend apply + capture (Issue #94) -----

def test_apply_backend_change_busy_still_applies(widget):
    """busy でも適用は行われる（進行中ターンは turn.backend の自参照で完走する）。
    戻り値 False は呼び出し側の「次の送信から反映」表示用。"""
    widget._turns["t"] = object()             # simulate an in-flight turn
    widget._session_backends["s"] = _FakeBackend()
    old = widget._backend
    assert widget.apply_backend_change() is False
    assert widget._session_backends == {}     # busy でもキャッシュは落ちる
    assert widget._backend is not old         # プロトタイプも新設定で再構築


def test_apply_backend_change_clears_and_rebuilds(widget):
    widget._session_backends["s"] = _FakeBackend()
    old = widget._backend
    assert widget.apply_backend_change() is True
    assert widget._session_backends == {}
    assert widget._backend is not old         # fresh prototype from the factory


def test_capture_backend_session_adopts_name_and_token(widget):
    from llm_bridge import chat_store

    sess = chat_store.new_session("mock", "sys")

    class _B:
        name = "claude-code"
        _session_id = "tok123"

    widget._capture_backend_session(_B(), sess)
    assert sess.backend_name == "claude-code"
    assert sess.backend_session_id == "tok123"


def test_capture_backend_session_nulls_token_for_tokenless_backend(widget):
    from llm_bridge import chat_store

    sess = chat_store.new_session("claude-code", "sys")
    sess.backend_session_id = "old-token"

    class _B:
        name = "mock"       # no _session_id attribute

    widget._capture_backend_session(_B(), sess)
    assert sess.backend_name == "mock"
    assert sess.backend_session_id is None


# ---- resume token: PC ローカル化 + 失敗時の自己修復 ----
#
# 元のバグ: _on_failed が成功時と同じ _capture_backend_session を呼ぶため、
# resume に失敗した死んだ token が書き戻されていた。バックエンドの _session_id は
# 成功イベントでしか代入されないので、次ターンも同じ死んだ token で --resume →
# また失敗、を永久に繰り返す。しかも _session_id が非 None だと replay=False に
# なるので履歴すら送られず、そのチャットは二度と使えなくなっていた。


class _TokenBackend:
    """claude 相当（resume token を持つ）。_engine_id は _build_session_backend が刻む。"""
    name = "claude-code"

    def __init__(self, token="tok-live", engine="claude-cli"):
        self._session_id = token
        self._engine_id = engine


def _load_and_peek(widget, sess, backend):
    """次ターン開始時に backend へ渡る token を見る（None = 履歴再送になる）。"""
    widget._load_backend_session(backend, sess)
    return backend._session_id


def _failed_turn(widget, sess, backend, *, stopped):
    from gui.chat import _Turn
    turn = _Turn(sess, backend, MagicMock(), MagicMock())
    turn.stopped = stopped
    widget._turns[sess.id] = turn
    return turn


def test_failed_turn_forgets_token_so_next_turn_replays(widget):
    """恒久破損の回帰テスト: 失敗ターンは token を捨て、次ターンで履歴再送に戻る。"""
    sess = _make_session(widget)
    backend = _TokenBackend(token="dead-token")
    widget._capture_backend_session(backend, sess)          # 事前に token を持たせる
    assert _load_and_peek(widget, sess,backend) == "dead-token"

    _failed_turn(widget, sess, backend, stopped=False)
    widget._on_failed(sess.id, "claude exited with code 1")

    assert sess.backend_session_id is None
    # 次ターン: token が無い → backend._session_id=None → replay=True で全履歴再送
    assert _load_and_peek(widget, sess,backend) is None


def test_user_stop_keeps_token(widget):
    """Stop も cancel()→非ゼロ終了→_on_failed に落ちる。ここで token を捨てると
    中断のたびに全履歴再送になる（replay に上限が無いので実害がある）。"""
    sess = _make_session(widget)
    backend = _TokenBackend(token="tok-live")
    _failed_turn(widget, sess, backend, stopped=True)
    widget._on_failed(sess.id, "stopped")

    assert sess.backend_session_id == "tok-live"
    assert _load_and_peek(widget, sess,backend) == "tok-live"


def test_token_not_used_when_no_local_record(widget):
    """別 PC 相当: ローカルレコードが無ければ token は使わない（=履歴再送）。"""
    sess = _make_session(widget)
    backend = _TokenBackend(token="stale-from-other-pc")
    assert _load_and_peek(widget, sess,backend) is None


def test_token_not_reused_across_claude_engines(widget):
    """claude-vscode と claude-cli は backend.name が同じ "claude-code" なので、
    name だけで判定していた旧実装では別エンジンの token を素通ししていた。"""
    sess = _make_session(widget)
    widget._capture_backend_session(_TokenBackend("tok-cli", "claude-cli"), sess)

    vscode = _TokenBackend(token=None, engine="claude-vscode")
    assert _load_and_peek(widget, sess,vscode) is None      # エンジン違い → 使わない

    cli = _TokenBackend(token=None, engine="claude-cli")
    assert _load_and_peek(widget, sess,cli) == "tok-cli"    # 同じエンジン → 使う


def test_token_not_reused_across_backends(widget):
    sess = _make_session(widget)
    widget._capture_backend_session(_TokenBackend("tok-claude", "claude-cli"), sess)

    class _Pi:
        name = "pi-coding-agent"
        _session_id = None
        _engine_id = "pi"

    assert _load_and_peek(widget, sess,_Pi()) is None


def test_deleting_session_drops_its_token(widget, monkeypatch):
    """ストアは side record なので、draft と違ってセッション削除で自動的には消えない。"""
    from llm_bridge.paths import read_backend_session
    sess = _make_session(widget)
    widget._capture_backend_session(_TokenBackend("tok-1", "claude-cli"), sess)
    assert read_backend_session(sess.id) is not None

    from gui.chat import QMessageBox
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    widget._on_delete_session(_tab_index_for(widget, sess))
    assert read_backend_session(sess.id) is None


# ---- セッションごとのエンジン切替 ----


def _override(widget, sess, engine_id, model="", provider=""):
    widget._set_session_engine(sess, engine_id, model, provider)


def test_no_override_follows_global(widget):
    sess = _make_session(widget)
    assert widget._effective_engine(sess) is None


def test_override_resolves_to_catalog_engine(widget):
    sess = _make_session(widget)
    _override(widget, sess, "pi", "qwen3-coder", "llama.cpp")
    assert widget._effective_engine(sess).id == "pi"
    assert (sess.engine_model, sess.engine_provider) == ("qwen3-coder", "llama.cpp")


def test_unknown_engine_id_degrades_to_global_but_is_kept(widget):
    """古いビルドや当該エンジンの無い PC を経由しても、ユーザーの指定は消さない。"""
    sess = _make_session(widget)
    sess.engine = "engine-from-a-newer-build"
    assert widget._effective_engine(sess) is None      # 解決できない → 既定で動く
    assert sess.engine == "engine-from-a-newer-build"  # が、値は残る


def test_override_only_rebuilds_its_own_session(widget):
    """個別変更が他セッションのバックエンドを巻き込まないこと。"""
    a, b = _make_session(widget), _make_session(widget)
    widget._session_backends[a.id] = object()
    widget._session_backends[b.id] = sentinel = object()
    _override(widget, b, "pi")
    assert b.id not in widget._session_backends
    assert widget._session_backends[a.id] is not None      # A は温存
    assert widget._session_backends.get(a.id) is not sentinel


def test_changing_engine_drops_the_resume_token(widget):
    """別エンジンのネイティブセッションを指す token は無意味になる。"""
    from llm_bridge.paths import read_backend_session
    sess = _make_session(widget)
    widget._capture_backend_session(_TokenBackend("tok-1", "claude-cli"), sess)
    _override(widget, sess, "pi")
    assert sess.backend_session_id is None
    assert read_backend_session(sess.id) is None


def test_set_engine_bumps_updated_for_merge(widget):
    """merge_sessions は厳密 `updated >` なので、メッセージを触らない変更でも
    bump しないと別 PC の古いコピーに負ける。"""
    sess = _make_session(widget)
    before = sess.updated
    _override(widget, sess, "pi")
    assert sess.updated > before


def test_clearing_override_returns_to_default(widget):
    sess = _make_session(widget)
    _override(widget, sess, "pi", "qwen3-coder")
    widget._set_session_engine(sess, None)
    assert sess.engine is None
    assert (sess.engine_model, sess.engine_provider) == (None, None)
    assert widget._effective_engine(sess) is None


def test_apply_backend_change_reanchors_active_streaming_turn(widget):
    """安全性主張の核心を実経路で通す: ACTIVE セッションがストリーミング中に適用
    しても、_render_session が turn を再 anchor し部分応答が描き直されること。
    （fake turn を active に紐づけない他テストではこの経路が走らない。）"""
    from types import SimpleNamespace

    sess = _make_session(widget)
    widget._active = sess
    turn = SimpleNamespace(buffer="部分応答", anchor=None, rendered="", session=sess)
    widget._turns[sess.id] = turn
    assert widget.apply_backend_change() is False
    assert turn.anchor is not None                 # 再 anchor された
    assert turn.rendered == "部分応答"             # 部分バッファが描画基準に再設定
    assert "部分応答" in widget._log.toPlainText() # 実際に描き直されている


def test_mid_turn_engine_change_notifies_via_status_bar(widget):
    """応答中の適用は transcript ではなくステータスバーで通知する
    （_flush_live_markdown が anchor→文書末尾を全置換するため、transcript への
    追記は次の描画で消える）。非応答時は何も出さない。"""
    sess = _make_session(widget)
    _override(widget, sess, "pi")
    widget._window.statusBar.assert_not_called()

    widget._turns[sess.id] = object()          # simulate an in-flight turn
    _override(widget, sess, None)
    widget._window.statusBar().showMessage.assert_called_once()


def test_build_session_backend_falls_back_on_broken_override(widget, monkeypatch):
    """_start_turn はリレー経由の Qt スロットからも走るので、壊れた上書きで
    例外を投げてはならない（未捕捉スロット例外になる）。"""
    import llm_backend
    sess = _make_session(widget)
    _override(widget, sess, "pi")
    monkeypatch.setattr(
        llm_backend, "build_backend",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unknown backend key")),
    )
    backend = widget._build_session_backend(sess)      # must not raise
    assert backend is not None
    assert isinstance(widget._log.toPlainText(), str)


def test_build_session_backend_tags_engine_id(widget):
    sess = _make_session(widget)
    backend = widget._build_session_backend(sess)
    assert hasattr(backend, "_engine_id")


def test_engine_header_marks_default_vs_override(widget):
    sess = _make_session(widget)
    assert tr("chat.engine.mark_default") in widget._engine_header(sess)
    _override(widget, sess, "pi", "qwen3-coder")
    header = widget._engine_header(sess)
    assert tr("chat.engine.mark_override") in header
    assert "qwen3-coder" in header


def test_engine_header_does_not_build_a_backend(widget, monkeypatch):
    """ヘッダはタブ切替のたびに描かれる。依存の無いエンジンで落ちては困る。"""
    import llm_backend
    sess = _make_session(widget)
    _override(widget, sess, "pi", "qwen3-coder")
    monkeypatch.setattr(
        llm_backend, "build_backend",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not build")),
    )
    assert "qwen3-coder" in widget._engine_header(sess)
