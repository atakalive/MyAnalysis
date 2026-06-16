"""Offscreen Qt test: language switch live-reflects in the window."""
import pytest


@pytest.fixture
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _restore_i18n():
    import common.i18n as i18n
    saved = (i18n._active, dict(i18n._catalogs))
    yield
    i18n._active, i18n._catalogs = saved[0], saved[1]


def test_language_switch_live_reflects(qapp, tmp_path, monkeypatch):
    import llm_bridge.paths as lp
    monkeypatch.setattr(lp, "ui_prefs_path", lambda: tmp_path / "ui_prefs.json")

    import common.i18n as i18n
    i18n._load_catalogs()  # use the real i18n/ catalogs (commit artefact smoke)

    from gui.window import ToolWindow

    i18n.set_language("en")
    win = ToolWindow()
    win.retranslate()
    en_title = win._file_menu.title()

    i18n.set_language("ja")
    win.retranslate()
    ja_title = win._file_menu.title()

    assert en_title != ja_title
    assert ja_title == "ファイル(&F)"
    assert en_title == i18n._catalogs["en"]["menu.file"]
