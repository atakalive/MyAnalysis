"""gui/math_settings_dialog.py — 設定 → 数式の描画…（Issue #123）。偽の導入で動かす（ネットは使わない）。"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from common import mathjax_install as mi
from common.i18n import tr
from common.mathmd import split_math
from gui import math_engine
from gui import math_settings_dialog as msd
from llm_bridge.paths import read_ui_pref, update_ui_pref
from test_mathmd import SAMPLE_MD


@pytest.fixture(autouse=True)
def _app(monkeypatch):
    app = QApplication.instance() or QApplication([])
    # QtWebEngine をこのプロセスに読み込まない（読めることにする）
    monkeypatch.setattr(math_engine, "_load_webengine", lambda: object())
    # プレビューで page を作らない（作ることを確かめるテストは Starts でインスタンスに差し替える）
    monkeypatch.setattr(math_engine.MathEngine, "_start", lambda self: None)
    return app


class Starts:
    def __init__(self, eng, monkeypatch):
        self.n = 0
        monkeypatch.setattr(eng, "_start", self._start)

    def _start(self):
        self.n += 1


def _fake_install(fonts=("mathjax-dejavu",)):
    """install_root の下に、install() が置くのと同じ形の完全な版を作る。"""
    import json
    v = mi.install_root() / mi.VERSION
    (v / "fonts").mkdir(parents=True, exist_ok=True)
    (v / "tex-svg.js").write_text("//")
    for f in fonts:
        (v / "fonts" / f).mkdir(exist_ok=True)
        (v / "fonts" / f / "svg.js").write_text("//")
    (v / "installed.json").write_text(json.dumps({"version": mi.VERSION, "fonts": list(fonts)}))


def _dialog(eng=None, changed=None):
    eng = eng or math_engine.MathEngine()
    calls = changed if changed is not None else []
    dlg = msd.MathSettingsDialog(engine=eng, on_changed=lambda: calls.append(1))
    dlg._spawn = lambda fn: fn()                 # 同期で動かす
    return dlg, eng, calls


def test_preview_tex_matches_sample():
    first = next(s for s in split_math(SAMPLE_MD)[1] if s.display)
    assert msd.PREVIEW_TEX == first.tex


def test_status_and_buttons_by_state(monkeypatch):
    dlg, eng, _ = _dialog()
    assert dlg.status_text() == tr("math.status.missing")
    assert dlg._install_btn.text() == tr("math.btn.install") and not dlg._install_btn.isHidden()
    assert dlg._uninstall_btn.isHidden()
    assert not dlg._enable.isEnabled()
    _fake_install()
    dlg._refresh()
    assert dlg.status_text() == tr("math.status.installed", version=mi.VERSION)
    assert dlg._install_btn.isHidden() and not dlg._uninstall_btn.isHidden()
    assert dlg._enable.isEnabled()
    (mi.install_root() / mi.VERSION / "installed.json").unlink()
    dlg._refresh()
    assert dlg.status_text() == tr("math.status.broken")
    assert dlg._install_btn.text() == tr("math.btn.reinstall")
    eng.last_error = "boom"
    eng._state = "failed"
    _fake_install()
    dlg._refresh()
    assert dlg.status_text() == tr("math.status.last_failure", error="boom")
    dlg.deleteLater()


def test_outdated_offers_update():
    _fake_install()
    os.replace(mi.install_root() / mi.VERSION, mi.install_root() / "4.0.0")
    dlg, _, _ = _dialog()
    assert dlg._install_btn.text() == tr("math.btn.update")
    dlg.deleteLater()


def test_origin_links_follow_constants_and_font():
    dlg, _, _ = _dialog()
    assert dlg._origin.openExternalLinks()
    text = dlg._origin.text()
    mj = mi.mathjax_package()
    for url in (mi.MATHJAX_HOMEPAGE, mi.MATHJAX_SOURCE, mj.npm_page, mj.tarball, mi.LICENSE_URL,
                mi.FONTS_SOURCE, mj.integrity):
        assert url in text
    dejavu = mi.font_package("mathjax-dejavu")
    assert dejavu.npm_page in text and dejavu.tarball in text and dejavu.integrity in text
    assert "DejaVu" in text and str(mi.install_root()) in text
    dlg._font.setCurrentIndex(mi.FONTS.index("mathjax-fira"))
    text = dlg._origin.text()
    fira = mi.font_package("mathjax-fira")
    assert fira.npm_page in text and fira.tarball in text and "Fira Math" in text
    assert dejavu.tarball not in text
    dlg.deleteLater()


def test_install_success_enables(monkeypatch):
    seen = {}

    def fake(font, progress, cancelled):
        seen["font"] = font
        progress(10, 100)
        _fake_install((font,))
        return mi.InstallResult(True)
    monkeypatch.setattr(mi, "install", fake)
    dlg, eng, calls = _dialog()
    dlg._on_install()
    assert seen["font"] == mi.DEFAULT_FONT
    assert read_ui_pref("math_render") is True
    assert eng.prefs.enabled and dlg._enable.isChecked()
    assert calls and dlg.message_text() == tr("math.done.installed")
    assert not dlg.is_busy()
    dlg.deleteLater()


def test_install_error_message(monkeypatch):
    monkeypatch.setattr(mi, "install", lambda f, p, c: mi.InstallResult(False, "superseded"))
    dlg, eng, _ = _dialog()
    dlg._on_install()
    assert dlg.message_text() == tr("math.error.superseded")
    assert read_ui_pref("math_render", False) is False
    dlg.deleteLater()


def test_cannot_close_while_installing():
    dlg, _, _ = _dialog()
    dlg._spawn = lambda fn: None                # 走ったまま
    dlg._on_install()
    assert dlg.is_busy()
    dlg.show()
    dlg.reject()
    assert dlg.isVisible()
    ev = QCloseEvent()
    dlg.closeEvent(ev)
    assert not ev.isAccepted()
    assert not dlg._close_btn.isEnabled() and not dlg._cancel_btn.isHidden()
    dlg._cancel_btn.click()
    assert dlg._cancel.is_set()
    dlg._on_finished("install", ("mathjax-dejavu", mi.InstallResult(False, "cancelled")))
    assert not dlg.is_busy()
    dlg.reject()
    assert not dlg.isVisible()
    dlg.deleteLater()


def test_disable_stops_engine(monkeypatch):
    _fake_install()
    update_ui_pref("math_render", True)
    eng = math_engine.MathEngine()
    stops = []
    real_stop = eng.stop
    monkeypatch.setattr(eng, "stop", lambda: (stops.append(1), real_stop()))
    Starts(eng, monkeypatch)
    dlg, _, calls = _dialog(eng)
    assert dlg._enable.isChecked()
    n = len(stops)
    dlg._enable.setChecked(False)
    assert len(stops) > n
    assert read_ui_pref("math_render") is False and not eng.prefs.enabled
    assert calls
    dlg.deleteLater()


def test_uninstalled_font_requires_explicit_install(monkeypatch):
    _fake_install(("mathjax-dejavu",))
    called = []
    monkeypatch.setattr(mi, "install", lambda *a: called.append(a) or mi.InstallResult(True))
    dlg, eng, _ = _dialog()
    dlg._font.setCurrentIndex(mi.FONTS.index("mathjax-stix2"))
    assert called == []
    assert dlg._install_btn.text() == tr("math.btn.install_font") and not dlg._install_btn.isHidden()
    assert dlg._font_note.text() == tr("math.font.not_installed")
    assert read_ui_pref("math_font", mi.DEFAULT_FONT) == mi.DEFAULT_FONT
    assert "STIX Two" in dlg._origin.text()
    dlg.deleteLater()


def test_font_change_restarts_engine(monkeypatch):
    _fake_install(("mathjax-dejavu", "mathjax-fira"))
    update_ui_pref("math_render", True)
    eng = math_engine.MathEngine()
    starts = Starts(eng, monkeypatch)
    stops = []
    real_stop = eng.stop
    monkeypatch.setattr(eng, "stop", lambda: (stops.append(1), real_stop()))
    dlg, _, _ = _dialog(eng)
    n_start, n_stop = starts.n, len(stops)
    dlg._font.setCurrentIndex(mi.FONTS.index("mathjax-fira"))
    assert read_ui_pref("math_font") == "mathjax-fira" and eng.prefs.font == "mathjax-fira"
    assert len(stops) > n_stop and starts.n > n_start         # 止めて、プレビューのために起こし直す
    assert dlg._preview.text() == tr("math.preview.rendering")
    dlg.deleteLater()


def test_scale_and_bold_saved():
    _fake_install()
    dlg, eng, calls = _dialog()
    dlg._scale.setCurrentIndex(mi.SCALES.index(1.35))
    dlg._bold.setChecked(False)
    assert read_ui_pref("math_scale") == 1.35 and read_ui_pref("math_bold") is False
    assert eng.prefs.scale == 1.35 and eng.prefs.bold is False
    assert len(calls) >= 2
    dlg.deleteLater()


@pytest.mark.parametrize("key,bad", [
    ("math_font", "comic-sans"), ("math_scale", 7.0), ("math_scale", "big"), ("math_bold", "yes"),
    ("math_render", "on"),
])
def test_unknown_prefs_fall_back(key, bad):
    update_ui_pref(key, bad)
    p = math_engine.load_prefs()
    assert p == math_engine.MathPrefs()
    dlg, _, _ = _dialog()
    assert dlg.selected_font() == mi.DEFAULT_FONT
    assert dlg._scale.currentData() == mi.DEFAULT_SCALE
    assert dlg._bold.isChecked() is mi.DEFAULT_BOLD
    dlg.deleteLater()


def test_uninstall_disables(monkeypatch):
    _fake_install()
    update_ui_pref("math_render", True)
    dlg, eng, calls = _dialog()
    dlg._uninstall_btn.click()
    assert not (mi.install_root() / mi.VERSION).exists()
    assert read_ui_pref("math_render") is False
    assert dlg.status_text() == tr("math.status.missing")
    assert calls
    dlg.deleteLater()


def test_uninstall_in_use_message(monkeypatch):
    _fake_install()
    monkeypatch.setattr(mi, "uninstall", lambda: mi.InstallResult(False, "in_use"))
    dlg, _, _ = _dialog()
    dlg._uninstall_btn.click()
    assert dlg.message_text() == tr("math.error.in_use")
    dlg.deleteLater()
