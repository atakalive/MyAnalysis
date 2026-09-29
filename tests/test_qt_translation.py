"""Offscreen Qt test: qtbase translator install/swap (Issue #103 G-1)."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QLibraryInfo, QTranslator  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QDialogButtonBox, QMessageBox,
)

import gui.qt_translation as qt_translation  # noqa: E402
from gui.qt_translation import install_qt_translator  # noqa: E402

_ATTR = "_myanalysis_qtbase_translator"


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    # 他のテストに translator を残さない（英語のボタン文字列を前提にしたテストがある）。
    install_qt_translator(app, "en")


@pytest.fixture
def qapp_ja(qapp):
    path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if not QTranslator().load("qtbase_ja", path):
        pytest.skip("qtbase_ja.qm not available")
    return qapp


def _cancel_text() -> str:
    box = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
    try:
        return box.button(QDialogButtonBox.StandardButton.Cancel).text()
    finally:
        box.deleteLater()


def _yes_text() -> str:
    box = QMessageBox()
    box.setStandardButtons(QMessageBox.StandardButton.Yes)
    try:
        return box.button(QMessageBox.StandardButton.Yes).text()
    finally:
        box.deleteLater()


def test_ja_then_en(qapp_ja):
    app = qapp_ja
    assert install_qt_translator(app, "ja") is True
    assert _cancel_text() == "キャンセル"
    assert _yes_text() != "&Yes"
    assert install_qt_translator(app, "en") is False
    assert getattr(app, _ATTR) is None
    assert _cancel_text() == "Cancel"


def test_reinstall_replaces_previous(qapp_ja):
    app = qapp_ja
    assert install_qt_translator(app, "ja") is True
    first = getattr(app, _ATTR)
    assert install_qt_translator(app, "ja") is True
    assert app.removeTranslator(first) is False      # 既に外れている
    assert getattr(app, _ATTR) is not first


def test_translators_do_not_accumulate(qapp_ja):
    app = qapp_ja
    for lang in ("ja", "ja", "en", "zz", "ja", "en"):
        install_qt_translator(app, lang)
    assert app.findChildren(QTranslator) == []
    assert getattr(app, _ATTR) is None


def test_unknown_language_installs_nothing(qapp):
    assert install_qt_translator(qapp, "zz") is False
    assert getattr(qapp, _ATTR, None) is None
    assert _cancel_text() == "Cancel"


def test_install_failure_is_not_reported_as_success(qapp, monkeypatch):
    class _FakeTranslator:
        def load(self, *args, **kwargs):
            return True

    monkeypatch.setattr(qt_translation, "QTranslator", _FakeTranslator)
    monkeypatch.setattr(qapp, "installTranslator", lambda t: False)
    assert install_qt_translator(qapp, "ja") is False
    assert getattr(qapp, _ATTR, None) is None
