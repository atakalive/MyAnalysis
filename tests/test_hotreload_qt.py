"""Qt-side hot-reload tests (offscreen): controller wiring, busy guard, Tier 1
patch of a tmp-based repo module, and manifest round-trip.

All file I/O uses tmp_path — nothing is written to the real repo tree.
"""

from __future__ import annotations

import sys

import pytest


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def window(qapp, monkeypatch, tmp_path):
    from llm_bridge import paths

    llm_state = tmp_path / "llm_state"

    def _fake_global_state_dir():
        llm_state.mkdir(parents=True, exist_ok=True)
        return llm_state

    monkeypatch.setattr(paths, "global_state_dir", _fake_global_state_dir)

    import tool

    win = tool.create_main_window(qapp)
    yield win
    win._hotreload._teardown_watchers(win)
    win.deleteLater()


@pytest.fixture()
def probe(monkeypatch, tmp_path):
    """A throwaway .py in tmp_path (hermetic — no writes to the real repo)."""
    name = "_hotreload_probe_xyz"
    path = tmp_path / f"{name}.py"
    path.write_text("def value():\n    return 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    import importlib

    importlib.invalidate_caches()
    mod = importlib.import_module(name)
    yield name, path, mod
    sys.modules.pop(name, None)


# ---------------------------------------------------------------------------


def test_reload_verb_and_menu_installed(window):
    from common.i18n import tr

    assert window.has_command("reload")
    titles = [
        m.title()
        for m in window.menuBar().findChildren(type(window.menuBar().addMenu("_tmp")))
    ]
    assert tr("menu.dev") in titles


def test_reload_no_changes(window):
    assert window.dispatch_command("reload", scope="patch") == "no changes"


def test_busy_guard_blocks_reload(window, monkeypatch):
    cw = window.chat_widget()
    monkeypatch.setattr(cw, "is_busy", lambda: True)
    result = window.dispatch_command("reload", scope="patch")
    assert result.startswith("reload-busy:")


def test_tier1_patch_reflects_new_code(qapp, probe, monkeypatch, tmp_path):
    from llm_bridge import paths

    llm_state = tmp_path / "llm_state"

    def _fake_global_state_dir():
        llm_state.mkdir(parents=True, exist_ok=True)
        return llm_state

    monkeypatch.setattr(paths, "global_state_dir", _fake_global_state_dir)

    import tool

    name, path, mod = probe
    win = tool.create_main_window(qapp)
    # Override the reloader root so it recognises the tmp_path probe module.
    win._hotreload._reloader.root = path.parent.resolve()
    win._hotreload._reloader.records.clear()
    win._hotreload._reloader.scan()
    try:
        fn = mod.value
        assert fn() == 1
        path.write_text("def value():\n    return 42\n", encoding="utf-8")
        report = win.dispatch_command("reload", scope="patch")
        assert "reloaded" in report and name in report
        assert fn() == 42  # same live function object, new code
        assert mod.value is fn
    finally:
        win._hotreload._teardown_watchers(win)
        win.deleteLater()


def test_unknown_scope_rejected(window):
    assert window.dispatch_command("reload", scope="bogus").startswith("reload-error:")


def test_scope_tab_requires_target(window):
    assert window.dispatch_command("reload", scope="tab").startswith("reload-error:")


# ---------------------------------------------------------------------------
# manifest round-trip
# ---------------------------------------------------------------------------


def test_manifest_write_consume_roundtrip(window, monkeypatch, tmp_path):
    from devtools import qt_integration
    from llm_bridge import paths

    mpath = tmp_path / "reload_manifest.json"
    monkeypatch.setattr(paths, "reload_manifest_path", lambda: mpath)

    cw = window.chat_widget()
    cw.set_input_draft("a half-written question")

    qt_integration.write_manifest(window)
    assert mpath.is_file()

    cw.set_input_draft("")  # clobber, then restore from manifest
    consumed = qt_integration.consume_manifest(window)
    assert consumed is True
    assert cw.input_draft() == "a half-written question"
    assert not mpath.exists()  # consumed → deleted


def test_consume_missing_manifest_is_noop(window, monkeypatch, tmp_path):
    from llm_bridge import paths

    monkeypatch.setattr(paths, "reload_manifest_path", lambda: tmp_path / "absent.json")
    from devtools import qt_integration

    assert qt_integration.consume_manifest(window) is False


def test_consume_corrupt_manifest_deletes_and_noops(window, monkeypatch, tmp_path):
    from llm_bridge import paths

    mpath = tmp_path / "reload_manifest.json"
    mpath.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(paths, "reload_manifest_path", lambda: mpath)
    from devtools import qt_integration

    assert qt_integration.consume_manifest(window) is False
    assert not mpath.exists()


def test_consume_manifest_missing_keys_no_crash(window, monkeypatch, tmp_path):
    from llm_bridge import paths

    mpath = tmp_path / "reload_manifest.json"
    mpath.write_text("{}", encoding="utf-8")  # valid JSON, all keys absent
    monkeypatch.setattr(paths, "reload_manifest_path", lambda: mpath)
    from devtools import qt_integration

    assert qt_integration.consume_manifest(window) is True
    assert not mpath.exists()


# ---------------------------------------------------------------------------
# Tier 2 — reload_tab (sandbox build → swap)
# ---------------------------------------------------------------------------

_ANALYSIS_SRC = """\
import llm_bridge
NAME = "{name}"
def load():
    return {{"v": {v}}}
def build_tab(parent, data):
    from PySide6.QtWidgets import QLabel
    from gui.tab import AnalysisTab
    tab = AnalysisTab(name=NAME, parent=parent)
    tab.add_panel("main", QLabel(str(data.get("v"))), "left")
    tab._v = data.get("v")
    tab._watchers = llm_bridge.attach_tab(tab, lambda: {{"v": tab._v}})
    tab.dispatch_command("refresh-state")
    return tab
"""


HR_DS = "_hr_probe_ds"


@pytest.fixture()
def probe_analysis(monkeypatch, tmp_path):
    """A throwaway analysis under tmp_path — hermetic, no real repo writes.

    Points config.get_dataset_dir at tmp_path/<dataset>, so analyses live at
    tmp_path/HR_DS/analyses/<name>/ and state at tmp_path/HR_DS/_work/... .
    """
    name = "_hr_probe_analysis_xyz"
    d = tmp_path / HR_DS / "analyses" / name
    d.mkdir(parents=True)
    af = d / "analysis.py"
    af.write_text(_ANALYSIS_SRC.format(name=name, v=1), encoding="utf-8")

    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)

    yield name, af


def test_tier2_reload_tab_swaps_and_syncs_state(window, probe_analysis):
    from llm_bridge import state

    name, af = probe_analysis
    assert window.dispatch_command("add-tab", name=name, dataset=HR_DS) \
        == f"added:{name}"
    assert name in window.tab_names()
    assert state.read(HR_DS, name) == {"v": 1}
    old_tab = next(t for t in window.tabs() if t.name == name)

    af.write_text(_ANALYSIS_SRC.format(name=name, v=2), encoding="utf-8")
    result = window.dispatch_command("reload", scope="tab", target=name)
    assert result == f"reloaded-tab:{name}"
    new_tab = next(t for t in window.tabs() if t.name == name)
    assert new_tab is not old_tab  # swapped
    assert state.read(HR_DS, name) == {"v": 2}  # refresh-state synced new UI


def test_tier2_build_failure_retains_old_tab(window, probe_analysis):
    from llm_bridge import state

    name, af = probe_analysis
    window.dispatch_command("add-tab", name=name, dataset=HR_DS)
    old_tab = next(t for t in window.tabs() if t.name == name)

    af.write_text(
        "def build_tab(parent, data):\n    raise RuntimeError('boom')\n",
        encoding="utf-8",
    )
    result = window.dispatch_command("reload", scope="tab", target=name)
    assert result.startswith("reload-tab-error:")
    assert next(t for t in window.tabs() if t.name == name) is old_tab  # retained
    assert state.read(HR_DS, name) == {"v": 1}  # captured state restored
