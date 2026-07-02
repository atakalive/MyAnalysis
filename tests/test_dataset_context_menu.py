"""DS 切替タブ右クリックメニュー (#57): 「データセットを閉じる」+ ◆ 識別記号。

ハンドラは ToolWindow を実体化せず duck-typed self（_DsMenuHost）で検証する。
QMenu を作るテストは QApplication を先に用意して SIGABRT を避ける（雛形と同じ）。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from common.i18n import tr
from gui.window import ToolWindow


class _DsMenuHost:
    def __init__(self, groups):
        self._groups = groups          # ガード `ds in self._groups` 用
        self.close_calls = []          # close_dataset 呼び出し記録

    def close_dataset(self, name):
        self.close_calls.append(name)
        return 0


def test_format_switcher_label():
    assert ToolWindow._format_switcher_label("ds1", False) == "◆ ds1"
    assert ToolWindow._format_switcher_label("ds1", True) == "● ◆ ds1"


def test_populate_dataset_menu_builds_close_action():
    from PySide6.QtWidgets import QApplication, QMenu
    QApplication.instance() or QApplication([])
    host = _DsMenuHost({"ds1": object()})
    menu = QMenu()
    ToolWindow._populate_dataset_menu(host, menu, "ds1")
    actions = menu.actions()
    assert len(actions) == 1
    assert actions[0].text() == tr("menu.dataset.close")


def test_action_triggers_close_dataset_when_open():
    from PySide6.QtWidgets import QApplication, QMenu
    QApplication.instance() or QApplication([])
    host = _DsMenuHost({"ds1": object()})
    menu = QMenu()
    ToolWindow._populate_dataset_menu(host, menu, "ds1")
    menu.actions()[0].trigger()
    assert host.close_calls == ["ds1"]


def test_action_guarded_when_already_closed():
    from PySide6.QtWidgets import QApplication, QMenu
    QApplication.instance() or QApplication([])
    host = _DsMenuHost({})  # "ds1" が _groups に無い＝表示中に閉じられた状況
    menu = QMenu()
    ToolWindow._populate_dataset_menu(host, menu, "ds1")
    menu.actions()[0].trigger()  # 例外が送出されないこと
    assert host.close_calls == []


def test_i18n_key_exists():
    import tomllib

    from common.paths import i18n_dir

    d = i18n_dir()
    for lang in ("ja", "en"):
        with open(d / f"{lang}.toml", "rb") as f:
            cat = tomllib.load(f)
        val = cat.get("menu.dataset.close", "")
        assert isinstance(val, str) and val.strip(), f"{lang}: menu.dataset.close missing/empty"
