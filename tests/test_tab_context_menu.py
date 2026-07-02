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


def test_analysis_tab_default_not_placeholder():
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from gui.tab import AnalysisTab

    assert AnalysisTab(name="x").is_placeholder is False


def test_build_placeholder_tab_is_flagged():
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from tool import build_placeholder_tab

    tab, _sp, _ah = build_placeholder_tab()
    assert tab.is_placeholder is True


class _MenuHost:
    """`_populate_tab_menu` の self 代役。将来 lambda を直接メソッド参照
    (`triggered.connect(self.close_tab)` 等) に置き換えても AttributeError に
    ならないよう、参照されうるメソッドを実体として持つダミー。"""

    def _comment_on_tab(self, name: str) -> None:
        pass

    def close_tab(self, name: str, dataset: str | None = None) -> bool:
        return False


def test_placeholder_menu_is_empty():
    from PySide6.QtWidgets import QApplication, QMenu

    QApplication.instance() or QApplication([])
    from gui.window import ToolWindow

    class _Widget:
        name = "(empty)"
        is_placeholder = True

    menu = QMenu()
    ToolWindow._populate_tab_menu(_MenuHost(), menu, _Widget(), None)
    assert menu.actions() == []  # プレースホルダは項目ゼロ（close も出さない）


def test_close_tab_refuses_placeholder():
    from gui.window import ToolWindow

    class _Ph:
        name = "(empty)"
        is_placeholder = True

        def deleteLater(self):  # 呼ばれたらガード漏れ
            raise AssertionError("placeholder must not be closed")

    class _Grp:
        def find(self, name):
            return (0, _Ph()) if name == "(empty)" else (None, None)

    class _Self:
        def _groups_to_search(self, dataset):
            return [_Grp()]

        def mark_session_dirty(self):
            pass

    assert ToolWindow.close_tab(_Self(), "(empty)") is False


def test_real_tab_menu_shows_all_items():
    from PySide6.QtWidgets import QApplication, QMenu

    QApplication.instance() or QApplication([])
    from gui.window import ToolWindow

    class _Widget:
        name = "myanalysis"
        is_placeholder = False

    menu = QMenu()
    ToolWindow._populate_tab_menu(_MenuHost(), menu, _Widget(), "ds1")
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert texts == [
        tr("menu.tab.copy_name"),
        tr("menu.tab.comment"),
        tr("menu.tab.close"),
    ]
