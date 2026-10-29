"""チャットの数式（Issue #123）— gui/chat.py の組込み。偽のエンジンで、鍵ごとに pending → ready を手で切り替える。"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from common.i18n import tr
from gui import math_engine
from gui.math_engine import MathPrefs, MathResult

SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -750 1000 1000">'
       '<g stroke="currentColor" fill="currentColor" stroke-width="0" transform="scale(1,-1)">'
       '<rect x="100" y="-200" width="800" height="900"></rect></g></svg>')


class FakeEngine(QObject):
    rendered = Signal()

    def __init__(self, enabled=True):
        super().__init__()
        self.prefs = MathPrefs(enabled=enabled, font="mathjax-dejavu", scale=1.0, bold=False)
        self.ready: set[tuple[str, bool]] = set()

    def request(self, tex, display, *, force=False):
        if not self.prefs.enabled:
            return MathResult("error", None, "unavailable")
        if (tex, display) in self.ready:
            return MathResult("ready", SVG)
        return MathResult("pending")

    def is_pending(self, key):
        return self.request(key[1], key[2]).status == "pending"

    def make_ready(self, *texs, display=False):
        for t in texs:
            self.ready.add((t, display))


class _FakeBackend:
    name = "mock"
    model = "fake-model"


@pytest.fixture()
def fake(monkeypatch):
    QApplication.instance() or QApplication([])
    eng = FakeEngine()
    monkeypatch.setattr(math_engine, "engine", lambda: eng)
    return eng


@pytest.fixture()
def widget(fake):
    from gui.chat import ChatWidget
    w = ChatWidget(_FakeBackend, dispatch=lambda *a, **k: None)
    yield w
    w._turns.clear()
    w.deleteLater()


def _count_renders(widget, monkeypatch):
    calls = []
    real = widget._render_session

    def spy(sess):
        calls.append(sess.id)
        return real(sess)
    monkeypatch.setattr(widget, "_render_session", spy)
    return calls


def _math_objects(widget):
    """(tex, 画像か) の一覧（文書の順）。"""
    from gui.math_render import TEX_PROP
    doc = widget._log.document()
    out = []
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            fmt = it.fragment().charFormat()
            tex = fmt.property(TEX_PROP)
            if tex:
                out.append((tex, fmt.isImageFormat()))
            it += 1
        block = block.next()
    return out


def _add_assistant(widget, text):
    from llm_backend.base import Message
    widget._active.messages.append(Message(role="assistant", content=text))


def _start_turn(widget, buffer):
    from gui.chat import _Turn
    sess = widget._active
    turn = _Turn(sess, _FakeBackend(), MagicMock(), MagicMock())
    turn.buffer = buffer
    widget._turns[sess.id] = turn
    widget._render_session(sess)
    return turn


def test_history_redraws_once_when_all_ready(widget, fake, monkeypatch):
    _add_assistant(widget, "$a$ and $b$ and $c$")
    widget._render_session(widget._active)
    assert [img for _, img in _math_objects(widget)] == [False, False, False]
    calls = _count_renders(widget, monkeypatch)
    fake.make_ready("a", "b")
    fake.rendered.emit()
    assert widget._math_timer.isActive()
    widget._on_math_timer()
    assert calls == []
    fake.make_ready("c")
    fake.rendered.emit()
    widget._on_math_timer()
    assert len(calls) == 1
    assert [img for _, img in _math_objects(widget)] == [True, True, True]
    assert widget._math_pending == set()


def test_live_formula_replaced_without_chunks(widget, fake, monkeypatch):
    _add_assistant(widget, "history $A$")
    turn = _start_turn(widget, "live $B$")
    assert ("mathjax-dejavu", "B", False) in turn.math_pending
    calls = _count_renders(widget, monkeypatch)
    widget._live_timer.stop()
    fake.make_ready("B")
    fake.rendered.emit()
    widget._on_math_timer()
    assert calls == []
    assert widget._live_timer.isActive()
    widget._live_timer.stop()
    widget._flush_live_markdown()
    objs = dict(_math_objects(widget))
    assert objs == {"A": False, "B": True}
    # 完了したメッセージの式も、ストリーミング中に差し替わる
    fake.make_ready("A")
    fake.rendered.emit()
    widget._on_math_timer()
    assert len(calls) == 1
    assert dict(_math_objects(widget)) == {"A": True, "B": True}


def test_live_and_completed_render_same_math(widget, fake):
    text = "x $a$ y\n\n$$\nb\n$$\n\n- $c$"
    fake.make_ready("a", "c")
    fake.make_ready("b", display=True)
    _start_turn(widget, text)
    widget._flush_live_markdown()
    live = _math_objects(widget)
    widget._turns.clear()
    widget._log.clear()
    widget._append_block("assistant", text, markdown=True)
    assert _math_objects(widget) == live
    assert len(live) == 3


def test_refresh_keeps_scroll_position(widget, fake):
    for i in range(60):
        _add_assistant(widget, f"line {i} $x_{i}$\n\nmore text {i}")
    widget.resize(400, 300)
    widget.show()
    QApplication.processEvents()
    widget._render_session(widget._active)
    QApplication.processEvents()
    sb = widget._log.verticalScrollBar()
    assert sb.maximum() > 100
    sb.setValue(sb.maximum() // 3)
    pos = sb.value()
    widget.refresh_math()
    assert sb.value() == pos
    sb.setValue(sb.maximum())
    widget.refresh_math()
    assert sb.value() == sb.maximum()
    widget.hide()


def test_copy_gives_latex_and_keeps_formatting(widget, fake):
    fake.make_ready("x^2")
    widget._log.clear()
    widget._append_block("assistant", "**bold** and $x^2$ end", markdown=True)
    assert ("x^2", True) in _math_objects(widget)
    widget._log.selectAll()
    mime = widget._log.createMimeDataFromSelection()
    html = mime.html()
    assert "math:" not in html and "mathimg:" not in html
    assert "$x^2$" in html
    assert "font-weight" in html or "<b>" in html
    assert "$x^2$" in mime.text()


def test_copy_without_math_uses_default(widget, fake):
    widget._log.clear()
    widget._append_block("assistant", "plain **text**", markdown=True)
    widget._log.selectAll()
    mime = widget._log.createMimeDataFromSelection()
    assert "plain" in mime.text()


def test_math_tex_at_finds_image(widget, fake):
    from PySide6.QtGui import QTextCursor
    fake.make_ready("q")
    widget.resize(400, 300)
    widget.show()
    widget._log.clear()
    widget._append_block("assistant", "see $q$ here", markdown=True)
    QApplication.processEvents()
    doc = widget._log.document()
    pos = doc.toPlainText().index("￼")
    c = QTextCursor(doc)
    c.setPosition(pos)
    rect = widget._log.cursorRect(c)
    assert widget._log.math_tex_at(rect.center()) == "q"
    widget.hide()


def test_hint_once_when_disabled(widget, fake, monkeypatch):
    import gui.chat as chat_mod
    monkeypatch.setattr(chat_mod, "_math_hint_shown", False)
    fake.prefs = MathPrefs(enabled=False)
    win = MagicMock()
    widget.bind_window(win)
    win.statusBar.return_value.showMessage.reset_mock()
    _add_assistant(widget, "value $x$")
    widget._render_session(widget._active)
    widget._render_session(widget._active)
    msgs = [c.args[0] for c in win.statusBar.return_value.showMessage.call_args_list]
    assert msgs.count(tr("math.hint")) == 1
    assert ("x", False) in _math_objects(widget)


def test_source_display_when_disabled(widget, fake):
    fake.prefs = MathPrefs(enabled=False)
    widget._log.clear()
    widget._append_block("assistant", r"a $\frac{1}{2}$ b", markdown=True)
    assert r"$\frac{1}{2}$" in widget._log.toPlainText()     # Qt に壊されない
