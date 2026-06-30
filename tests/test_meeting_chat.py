"""Tests for ChatWidget meeting hooks (Issue #42): messageAdded emit,
inject_remote_message (active / non-active / busy-FIFO), session_summaries, and
_pending_remote cleanup on delete.

GUI tests run offscreen with a QApplication, mirroring test_chat_widget.py.
"""

from __future__ import annotations

import time

import pytest


class EmptyBackend:
    """Backend whose stream yields nothing → the turn completes immediately."""
    name = "mock"
    model = "fake-model"
    last_usage = None

    def stream(self, messages, tools=None):
        return iter(())

    def cancel(self):
        pass


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def widget(qapp):
    from gui.chat import ChatWidget
    return ChatWidget(EmptyBackend, dispatch=lambda *a, **k: None)


def _drain(qapp, widget, timeout=5.0):
    deadline = time.time() + timeout
    while widget._turns and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)


def test_session_summaries(widget):
    from llm_bridge import chat_store
    s2 = chat_store.new_session("mock", "sys", dataset="ds", title="T")
    widget._sessions.append(s2)
    summ = widget.session_summaries()
    row = next(s for s in summ if s["id"] == s2.id)
    assert row["dataset"] == "ds"
    assert row["title"] == "T"
    assert row["busy"] is False


def test_user_message_emit(qapp, widget):
    got = []
    widget.messageAdded.connect(lambda *a: got.append(a))
    widget._input.setPlainText("hello")
    widget._on_send()
    # user emit is synchronous, before the worker thread starts
    assert (widget._active.id, "user", "hello", "local") in got
    _drain(qapp, widget)


def test_assistant_message_emit(widget):
    from gui.chat import _Turn
    from PySide6.QtCore import QTimer
    sess = widget._active

    class B:
        name = "mock"
        last_usage = None

    turn = _Turn(sess, B(), object(), QTimer(widget))
    turn.buffer = "the answer"
    widget._turns[sess.id] = turn
    got = []
    widget.messageAdded.connect(lambda *a: got.append(a))
    widget._on_done(sess.id)
    assert (sess.id, "assistant", "the answer", "local") in got
    # exactly one assistant emit
    assert sum(1 for g in got if g[1] == "assistant") == 1


def test_start_turn_assigns_stream_id(qapp, widget):
    """Every turn gets a stable, non-empty stream_id at start (relay correlation)."""
    widget._input.setPlainText("hi")
    widget._on_send()
    sid = widget._active.id
    assert widget._turns[sid].stream_id   # assigned synchronously at _start_turn
    _drain(qapp, widget)


def test_on_chunk_emits_cumulative_partials_with_stream_id(qapp, widget):
    """_on_chunk emits messageStreaming with the CUMULATIVE buffer + the turn's
    stream_id, BEFORE the active-session check (so background sessions stream to
    guests too). Driven directly on a non-active session to isolate the signal
    from the live-document render path."""
    from gui.chat import _Turn
    from PySide6.QtCore import QTimer
    from llm_bridge import chat_store

    bg = chat_store.new_session("mock", "sys", dataset=None, title="BG")
    widget._sessions.append(bg)
    assert bg is not widget._active   # non-active → _on_chunk returns after emitting

    class B:
        name = "mock"
        last_usage = None

    turn = _Turn(bg, B(), object(), QTimer(widget))
    turn.stream_id = "STREAM123"
    widget._turns[bg.id] = turn

    streamed = []
    widget.messageStreaming.connect(lambda *a: streamed.append(a))
    for piece in ("Hel", "lo wor", "ld"):
        widget._on_chunk(bg.id, piece)

    assert streamed == [
        (bg.id, "Hel", "local", "STREAM123"),
        (bg.id, "Hello wor", "local", "STREAM123"),
        (bg.id, "Hello world", "local", "STREAM123"),
    ]
    assert turn.buffer == "Hello world"
    widget._turns.pop(bg.id, None)   # don't leave a dangling busy session for teardown


def test_inject_unknown_session_is_noop(widget):
    before = len(widget._active.messages)
    widget.inject_remote_message("hi", "X", session_id="does-not-exist")
    assert len(widget._active.messages) == before
    assert not widget._turns


def test_inject_non_active_leaves_active_log(qapp, widget):
    from llm_bridge import chat_store
    A = widget._active
    B = chat_store.new_session("mock", "sys", dataset=None, title="B")
    widget._sessions.append(B)
    widget._rebuild_tab_bar()
    log_before = widget._log.toPlainText()
    widget.inject_remote_message("hi", "X", session_id=B.id)
    assert B.id in widget._turns
    assert widget._active is A
    assert widget._log.toPlainText() == log_before   # active transcript untouched
    _drain(qapp, widget)


def test_inject_busy_fifo(qapp, widget):
    from gui.chat import _Turn
    from PySide6.QtCore import QTimer
    sess = widget._active

    class B:
        name = "mock"
        last_usage = None

    turn = _Turn(sess, B(), object(), QTimer(widget))
    turn.buffer = ""
    widget._turns[sess.id] = turn   # mark busy

    widget.inject_remote_message("q1", "X", session_id=sess.id)
    assert widget._pending_remote.get(sess.id) == [("X", "q1")]

    widget._on_done(sess.id)   # completes the busy turn → drains the queue
    assert not widget._pending_remote.get(sess.id)
    assert sess.id in widget._turns   # a fresh (remote) turn started
    _drain(qapp, widget)


def test_delete_clears_pending(qapp, widget, monkeypatch):
    from llm_bridge import chat_store
    from gui.chat import QMessageBox
    B = chat_store.new_session("mock", "sys", dataset="ds", title="B")
    widget._sessions.append(B)
    widget._current_dataset = "ds"
    widget._rebuild_tab_bar()
    widget._pending_remote[B.id] = [("X", "q")]
    idx = next(i for i in range(widget._tab_bar.count())
               if widget._tab_bar.tabData(i) == B.id)
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    widget._on_delete_session(idx)
    assert B.id not in widget._pending_remote
