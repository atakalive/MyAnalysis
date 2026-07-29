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


def test_settings_menu_holds_the_preference_items(qapp):
    """表示 keeps window commands only; the persisted preferences live under 設定.

    Asserted by object identity, not by label text, so the check is independent
    of the active language. A bare ToolWindow() has no 開発 menu (installed later
    by devtools.install_hotreload).
    """
    from gui.window import ToolWindow

    win = ToolWindow()

    # Compare via menuAction(); QAction.menu() hands the QMenu's ownership to
    # Python and the C++ object dies as soon as the wrapper is dropped.
    assert win.menuBar().actions() == [
        win._file_menu.menuAction(),
        win._view_menu.menuAction(),
        win._settings_menu.menuAction(),
        win._help_menu.menuAction(),
    ]

    assert win._view_menu.actions() == [win._chat_action, win._meeting_share_action]

    # Submenus appear in the parent's action list as their menuAction().
    settings = win._settings_menu.actions()
    for act in (win._language_menu.menuAction(),
                win._tool_display_menu.menuAction(),
                win._provider_prompt_action,
                win._backend_selector_action):
        assert act in settings


def test_retranslate_hooks_fire_and_never_raise(qapp):
    """Menus built outside ToolWindow (e.g. the devtools 開発 menu) re-translate
    via register_retranslate_hook; a broken hook must not block the others."""
    from gui.window import ToolWindow

    win = ToolWindow()
    calls = []
    win.register_retranslate_hook(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    win.register_retranslate_hook(lambda: calls.append(1))

    win.retranslate()  # must not raise despite the first hook throwing

    assert calls == [1]


def test_language_survives_app_rebuild(qapp, tmp_path, monkeypatch):
    """Tier 3 (app / blue-green) rebuild calls tool.create_main_window() directly
    after purging common.i18n — which re-imports fresh and resets _active to the
    "en" default. The rebuilt window must re-hydrate the saved language from
    ui_prefs.json instead of reverting to English. Regression for the
    language-resets-to-English-on-app-rebuild bug.
    """
    import json

    from llm_bridge import paths

    llm_state = tmp_path / "llm_state"

    def _fake_global_state_dir():
        llm_state.mkdir(parents=True, exist_ok=True)
        return llm_state

    # ui_prefs_path() resolves under global_state_dir(), so this redirect makes
    # the whole read/write path hermetic (no touch of the real repo tree).
    monkeypatch.setattr(paths, "global_state_dir", _fake_global_state_dir)
    llm_state.mkdir(parents=True, exist_ok=True)
    (llm_state / "ui_prefs.json").write_text(
        json.dumps({"language": "ja"}), encoding="utf-8"
    )

    import common.i18n as i18n
    # Reproduce the state right after purge + fresh re-import of common.i18n:
    # module global back at the "en" default, catalogs not yet loaded.
    i18n._active = "en"
    i18n._catalogs = {}

    import tool

    win = tool.create_main_window(qapp)
    try:
        # create_main_window must have called init_language() before building the
        # window, restoring the persisted language (uses the real i18n/ catalogs).
        assert i18n.current_language() == "ja"
        assert win._file_menu.title() == "ファイル(&F)"
    finally:
        win._hotreload._teardown_watchers(win)
        win.deleteLater()
