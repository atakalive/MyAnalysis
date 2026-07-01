"""Host tab context-menu の新機能 (#52): タブ名コピー / このタブにコメント。

_comment_on_tab は ToolWindow/ChatWidget を実体化せず duck-typed self で検証する
（Qt 不要）。focus_input のみ ChatWidget を offscreen で実体化してカーソル位置を検証。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from common.i18n import tr


class _FakeChat:
    def __init__(self, draft: str = "") -> None:
        self.draft = draft
        self.focus_calls = 0

    def input_draft(self) -> str:
        return self.draft

    def set_input_draft(self, text: str) -> None:
        self.draft = text

    def focus_input(self) -> None:
        self.focus_calls += 1


class _FakeWin:
    def __init__(self, chat) -> None:
        self._chat = chat

    def chat_widget(self):
        return self._chat


def _comment_on_tab(win, name: str) -> None:
    from gui.window import ToolWindow

    ToolWindow._comment_on_tab(win, name)


def test_comment_prepends_prefix_and_focuses():
    chat = _FakeChat("既存ドラフト")
    _comment_on_tab(_FakeWin(chat), "myanalysis")
    prefix = tr("menu.tab.comment_prefix", name="myanalysis")
    assert "myanalysis" in prefix          # キー存在＋{name}補間の確認
    assert chat.draft == prefix + "既存ドラフト"
    assert chat.focus_calls == 1


def test_comment_is_idempotent():
    chat = _FakeChat("")
    win = _FakeWin(chat)
    _comment_on_tab(win, "ds1")
    after_first = chat.draft
    _comment_on_tab(win, "ds1")
    assert chat.draft == after_first       # 二重付与しない
    assert chat.focus_calls == 2           # フォーカスは毎回


def test_comment_none_chat_is_noop():
    _comment_on_tab(_FakeWin(None), "x")   # 例外を投げないこと


def test_focus_input_moves_cursor_to_end():
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from gui.chat import ChatWidget

    class _FakeBackend:
        name = "mock"
        model = "fake-model"

    w = ChatWidget(_FakeBackend, dispatch=lambda *a, **k: None)
    w.set_input_draft("hello")
    w.focus_input()
    assert w._input.textCursor().position() == len("hello")
