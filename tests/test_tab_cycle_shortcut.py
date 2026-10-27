"""Issue #121: Ctrl+←/→ で解析タブ、Alt+←/→ でデータセットを切り替える（offscreen Qt）。"""
from __future__ import annotations

import pytest


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def win(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication
    from gui.window import ToolWindow
    w = ToolWindow()
    yield w
    # close() は closeEvent が保存の確認を出すので呼ばない。
    w._close_all_floats()
    w.hide()
    w.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _add(win, ds, name):
    from gui.tab import AnalysisTab
    tab = AnalysisTab(name)
    tab.session_spec = {"kind": "analysis", "name": name, "dataset": ds}
    win.add_tab(tab)
    return tab


def _current_tab_name(win):
    grp = win._current_group()
    return grp.tabs.currentWidget().name


def _hide_other_tool_windows(win):
    # 他のテストが show() したまま残した ToolWindow は参照が切れても破棄されず表示された
    # まま残る。同じ ApplicationShortcut が 2 つ見えると曖昧になって発火しない。
    from PySide6.QtWidgets import QApplication
    from gui.window import ToolWindow
    for w in QApplication.topLevelWidgets():
        if isinstance(w, ToolWindow) and w is not win:
            w.hide()


# --------------------------------------------------------------------------- #
# 解析タブの巡回                                                                #
# --------------------------------------------------------------------------- #

def test_cycle_tab_wraps_around(win):
    for name in ("a", "b", "c"):
        _add(win, "ds", name)
    win.set_active_tab("a")
    assert _current_tab_name(win) == "a"

    win._cycle_tab(1)
    assert _current_tab_name(win) == "b"
    win._cycle_tab(1)
    assert _current_tab_name(win) == "c"
    win._cycle_tab(1)
    assert _current_tab_name(win) == "a"  # 端で反対側へ戻る
    win._cycle_tab(-1)
    assert _current_tab_name(win) == "c"


def test_cycle_tab_skips_float_stub(win):
    for name in ("a", "b", "c"):
        _add(win, "ds", name)
    assert win.float_tab("b", dataset="ds") is True
    win.set_active_tab("a")
    assert _current_tab_name(win) == "a"

    win._cycle_tab(1)
    assert _current_tab_name(win) == "c"  # b の位置スタブは飛ばす
    win._cycle_tab(1)
    assert _current_tab_name(win) == "a"
    win._cycle_tab(-1)
    assert _current_tab_name(win) == "c"


def test_cycle_tab_single_tab_is_noop(win):
    _add(win, "ds", "only")
    win._cycle_tab(1)
    win._cycle_tab(-1)
    assert _current_tab_name(win) == "only"


# --------------------------------------------------------------------------- #
# データセットの巡回                                                            #
# --------------------------------------------------------------------------- #

def test_cycle_dataset_wraps_around(win):
    for ds in ("ds1", "ds2", "ds3"):
        _add(win, ds, f"t_{ds}")
    win.set_active_tab("t_ds1", dataset="ds1")
    assert win.current_dataset == "ds1"

    win._cycle_dataset(1)
    assert win.current_dataset == "ds2"
    win._cycle_dataset(1)
    assert win.current_dataset == "ds3"
    win._cycle_dataset(1)
    assert win.current_dataset == "ds1"  # 端で反対側へ戻る
    win._cycle_dataset(-1)
    assert win.current_dataset == "ds3"
    assert win._current_group().name == "ds3"


def test_cycle_dataset_single_dataset_is_noop(win):
    _add(win, "ds1", "x")
    _add(win, "ds1", "y")
    win.set_active_tab("y", dataset="ds1")
    win._cycle_dataset(1)
    win._cycle_dataset(-1)
    assert win.current_dataset == "ds1"
    assert _current_tab_name(win) == "y"


# --------------------------------------------------------------------------- #
# キー操作（実際のショートカット経由）                                          #
# --------------------------------------------------------------------------- #

def test_key_shortcuts_from_chat_dock(win, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QTextBrowser, QTextEdit, QVBoxLayout, QWidget

    _hide_other_tool_windows(win)
    for ds in ("ds1", "ds2"):
        for name in ("a", "b"):
            _add(win, ds, f"{name}_{ds}")
    win.set_active_tab("a_ds1", dataset="ds1")

    log = QTextBrowser()
    log.setPlainText("log")
    edit = QTextEdit()
    edit.setPlainText("hello world")
    holder = QWidget()
    lay = QVBoxLayout(holder)
    lay.addWidget(log)
    lay.addWidget(edit)
    win._chat_dock.setWidget(holder)
    win.show()
    qapp.processEvents()

    ctrl = Qt.KeyboardModifier.ControlModifier
    alt = Qt.KeyboardModifier.AltModifier

    # チャットのログ（読み取り専用）で Ctrl+←/→ → 解析タブが切り替わる
    log.setFocus()
    qapp.processEvents()
    QTest.keyClick(log, Qt.Key.Key_Right, ctrl)
    assert _current_tab_name(win) == "b_ds1"
    QTest.keyClick(log, Qt.Key.Key_Left, ctrl)
    assert _current_tab_name(win) == "a_ds1"

    # 編集できるテキスト欄で Alt+←/→ → データセットが切り替わり、文字は変わらない
    edit.setFocus()
    qapp.processEvents()
    QTest.keyClick(edit, Qt.Key.Key_Right, alt)
    assert win.current_dataset == "ds2"
    QTest.keyClick(edit, Qt.Key.Key_Left, alt)
    assert win.current_dataset == "ds1"
    assert edit.toPlainText() == "hello world"

    # 編集できるテキスト欄の Ctrl+→ は単語移動に残す（解析タブは変わらない）
    QTest.keyClick(edit, Qt.Key.Key_Right, ctrl)
    assert _current_tab_name(win) == "a_ds1"
    assert win.current_dataset == "ds1"
