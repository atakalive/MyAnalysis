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


def test_tab_reload_allowed_while_chat_busy(window, monkeypatch):
    """エージェントは自分のターンの中で reload scope=tab を実行する。"""
    cw = window.chat_widget()
    monkeypatch.setattr(cw, "is_busy", lambda: True)
    result = window.dispatch_command("reload", scope="tab", target="__no_such_tab__")
    assert not str(result).startswith("reload-busy:")


def test_app_reload_still_blocked_while_chat_busy(window, monkeypatch):
    cw = window.chat_widget()
    monkeypatch.setattr(cw, "is_busy", lambda: True)
    result = window.dispatch_command("reload", scope="app")
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


def test_manifest_restore_when_active_differs(window, monkeypatch, tmp_path):
    """scope=app 再構築では「復元先セッション ≠ 現アクティブ」が普通のケース
    (_last_active_by_ds は作り直され _sync_active_to_visible が vis[0] を選ぶ)。

    下書きは per-tab なので set_active_session_by_id が composer を載せ替える →
    復元は「セッション選択 → 下書き」の順である必要がある。逆順だと復元した下書きが
    無関係なセッションへ退避され、空 draft(非永続) で composer が上書きされて消える。
    上の round-trip テストは同一 widget/同一 id を使うため id ガードで載せ替えが
    起きず、この退行を検出できない。"""
    from devtools import qt_integration
    from llm_bridge import chat_store, paths

    mpath = tmp_path / "reload_manifest.json"
    monkeypatch.setattr(paths, "reload_manifest_path", lambda: mpath)

    cw = window.chat_widget()
    target = cw._active
    cw.set_input_draft("half-written question I did not want to lose")
    qt_integration.write_manifest(window)

    # 再構築後を模す: アクティブが別セッション（新しい blank）を指している。
    other = chat_store.new_session("mock", "sys", dataset=None, title="blank")
    cw._sessions.append(other)
    cw._active = other
    cw._rebuild_tab_bar()
    cw.set_input_draft("")

    assert qt_integration.consume_manifest(window) is True
    assert cw._active.id == target.id
    assert cw.input_draft() == "half-written question I did not want to lose"
    assert other.draft == ""   # 無関係セッションへ流出していない


def test_manifest_view_state_by_dataset_shape(window, probe_analysis, monkeypatch, tmp_path):
    """Issue #51 B4: write_manifest nests view_state under dataset in a
    distinctly-keyed field, and records active_dataset."""
    import json

    from devtools import qt_integration
    from llm_bridge import paths

    mpath = tmp_path / "reload_manifest.json"
    monkeypatch.setattr(paths, "reload_manifest_path", lambda: mpath)

    name, _af = probe_analysis
    window.dispatch_command("add-tab", name=name, dataset=HR_DS)
    window.set_active_dataset(HR_DS)

    qt_integration.write_manifest(window)
    data = json.loads(mpath.read_text(encoding="utf-8"))
    # New nested key present; the flat legacy key is NOT written.
    assert "view_state_by_dataset" in data
    assert "view_state" not in data
    assert HR_DS in data["view_state_by_dataset"]
    assert name in data["view_state_by_dataset"][HR_DS]
    assert data["active_dataset"] == HR_DS


def test_manifest_restore_reads_nested_and_flat_view_state(window, probe_analysis, monkeypatch, tmp_path):
    """Restore reads view_state_by_dataset when present, and falls back to the
    old flat view_state for a mid-migration manifest (no crash either way)."""
    import json

    from devtools import qt_integration
    from llm_bridge import paths

    mpath = tmp_path / "reload_manifest.json"
    monkeypatch.setattr(paths, "reload_manifest_path", lambda: mpath)

    from llm_bridge import session as lbsession

    name, _af = probe_analysis
    # A session.json so open-dataset restores the analysis tab, then the flat
    # (legacy) view_state applies to it without crashing.
    lbsession.write_session(HR_DS, {
        "version": 1, "dataset": HR_DS, "active_tab": name,
        "tabs": [{"name": name, "kind": "analysis", "module": name}],
    })
    mpath.write_text(json.dumps({
        "datasets": [HR_DS],
        "active_tab": name,
        "active_dataset": HR_DS,
        "view_state": {name: {"panels": {}, "splitter": None}},
    }), encoding="utf-8")
    assert qt_integration.consume_manifest(window) is True
    assert name in window.tab_names()
    assert window.current_dataset == HR_DS


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


def test_consume_manifest_open_failure_warns_deferred(window, monkeypatch, tmp_path):
    """manifest 中の開けない DS（error:）は黙殺されず警告 1 回で顕在化する。

    表示は singleShot(0) の遅延（consume_manifest は window.show() 前に走る）
    なので、consume 直後は未表示 → イベント処理後に 1 回。
    """
    import json

    from devtools import qt_integration
    from llm_bridge import paths

    mpath = tmp_path / "reload_manifest.json"
    mpath.write_text(json.dumps({"datasets": ["dsGone"]}), encoding="utf-8")
    monkeypatch.setattr(paths, "reload_manifest_path", lambda: mpath)

    calls: list[str] = []
    monkeypatch.setattr(
        qt_integration.QMessageBox, "warning",
        lambda parent, title, body, *a, **k: calls.append(body),
    )

    assert qt_integration.consume_manifest(window) is True
    assert calls == []          # まだ出ていない（遅延表示）
    from PySide6.QtWidgets import QApplication
    for _ in range(5):
        QApplication.processEvents()
        if calls:
            break
    assert len(calls) == 1
    assert "dsGone" in calls[0]


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


def test_add_tab_future_annotations_dataclass(window, probe_analysis):
    """`from __future__ import annotations` + 最上位の @dataclass を含む解析が開ける。
    登録は build の間だけで、終わると sys.modules から外れる（Issue #100 D-2）。"""
    name, af = probe_analysis
    af.write_text(
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n"
        "import llm_bridge\n"
        f"NAME = {name!r}\n"
        "@dataclass\n"
        "class P:\n"
        "    x: int = 1\n"
        "def build_tab(parent, data):\n"
        "    from gui.tab import AnalysisTab\n"
        "    tab = AnalysisTab(name=NAME, parent=parent)\n"
        "    tab._watchers = llm_bridge.attach_tab(tab, lambda: {\"x\": P().x})\n"
        "    return tab\n",
        encoding="utf-8",
    )
    assert window.dispatch_command("add-tab", name=name, dataset=HR_DS) \
        == f"added:{name}"
    assert not any(k.startswith("_myanalysis_analysis_") for k in sys.modules)


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


def test_tier2_build_failure_preserves_unreadable_current_json(window, probe_analysis):
    # Regression (mount durability): a transiently-unreadable current.json must NOT
    # be overwritten with {} on the build-failure restore path — mirror of _add_tab.
    import dataset_config

    name, af = probe_analysis
    window.dispatch_command("add-tab", name=name, dataset=HR_DS)
    old_tab = next(t for t in window.tabs() if t.name == name)
    cj = dataset_config.state_dir(HR_DS, name, create=False) / "current.json"
    cj.write_bytes(b"")   # transient 0-byte / 未同期 view on the rclone/WinFsp mount

    af.write_text(
        "def build_tab(parent, data):\n    raise RuntimeError('boom')\n",
        encoding="utf-8",
    )
    result = window.dispatch_command("reload", scope="tab", target=name)
    assert result.startswith("reload-tab-error:")
    assert next(t for t in window.tabs() if t.name == name) is old_tab   # retained
    assert cj.read_bytes() == b""   # unreadable → restore-write skipped (not b"{}")


def test_tier2_empty_analysis_retains_tab_with_recover_hint(window, probe_analysis):
    # A 0-byte analysis.py (mount truncation) must be detected at build with a
    # diagnostic carrying the recover command, and the old tab must survive.
    name, af = probe_analysis
    window.dispatch_command("add-tab", name=name, dataset=HR_DS)
    old_tab = next(t for t in window.tabs() if t.name == name)

    af.write_text("", encoding="utf-8")   # 0-byte truncation on the mount
    result = window.dispatch_command("reload", scope="tab", target=name)
    assert result.startswith("reload-tab-error:build failed:")
    assert "recover-analysis" in result
    assert next(t for t in window.tabs() if t.name == name) is old_tab   # retained


def test_tier2_reload_success_updates_bak(window, probe_analysis):
    from common.paths import bak_path

    name, af = probe_analysis
    window.dispatch_command("add-tab", name=name, dataset=HR_DS)
    # add-tab already snapshotted v1 to .bak.
    assert bak_path(af).read_text(encoding="utf-8") == _ANALYSIS_SRC.format(name=name, v=1)

    new_src = _ANALYSIS_SRC.format(name=name, v=2)
    af.write_text(new_src, encoding="utf-8")
    assert window.dispatch_command("reload", scope="tab", target=name) \
        == f"reloaded-tab:{name}"
    assert bak_path(af).read_text(encoding="utf-8") == new_src   # .bak advanced


def test_tier2_name_mismatch_does_not_update_bak(window, probe_analysis):
    from common.paths import bak_path

    name, af = probe_analysis
    window.dispatch_command("add-tab", name=name, dataset=HR_DS)
    v1 = _ANALYSIS_SRC.format(name=name, v=1)
    assert bak_path(af).read_text(encoding="utf-8") == v1

    af.write_text(
        "import llm_bridge\n"
        "def build_tab(parent, data):\n"
        "    from gui.tab import AnalysisTab\n"
        "    tab = AnalysisTab(name='WRONG', parent=parent)\n"
        "    tab._watchers = llm_bridge.attach_tab(tab, lambda: {})\n"
        "    return tab\n",
        encoding="utf-8",
    )
    result = window.dispatch_command("reload", scope="tab", target=name)
    assert result.startswith("reload-tab-error:")
    assert bak_path(af).read_text(encoding="utf-8") == v1   # unchanged


def test_tier2_sync_failure_does_not_update_bak(window, probe_analysis):
    from common.paths import bak_path

    name, af = probe_analysis
    window.dispatch_command("add-tab", name=name, dataset=HR_DS)
    v1 = _ANALYSIS_SRC.format(name=name, v=1)
    assert bak_path(af).read_text(encoding="utf-8") == v1

    af.write_text(
        "import llm_bridge\n"
        f"NAME = {name!r}\n"
        "def apply_state(tab, state):\n"
        "    raise RuntimeError('boom')\n"
        "def build_tab(parent, data):\n"
        "    from gui.tab import AnalysisTab\n"
        "    tab = AnalysisTab(name=NAME, parent=parent)\n"
        "    tab._watchers = llm_bridge.attach_tab(tab, lambda: {})\n"
        "    return tab\n",
        encoding="utf-8",
    )
    result = window.dispatch_command("reload", scope="tab", target=name)
    assert result.startswith("reload-tab-error:")
    assert "apply_state failed: RuntimeError: boom" in result
    assert bak_path(af).read_text(encoding="utf-8") == v1   # unchanged


def test_tier2_reload_rejects_non_analysis_tab(window, qapp, tmp_path):
    """同名の figure/viewer タブを解析として reload しない (kind ガード)。

    reload_tab が old_tab の kind を見ないと、同名 viewer を
    解析タブへ置き換え得る。kind!=analysis のタブは reload-tab-error で fail-fast。
    """
    from PIL import Image

    png = tmp_path / "fig.png"
    Image.new("L", (4, 4)).save(png)
    window.dispatch_command("show", path=str(png), name="vw")
    assert "vw" in window.tab_names()

    result = window.dispatch_command("reload", scope="tab", target="vw")
    assert result.startswith("reload-tab-error:")
    assert "not an analysis tab" in result
    assert "vw" in window.tab_names()  # viewer は破壊されない


# ---------------------------------------------------------------------------
# requires_app 確認ダイアログ (Issue #83)
# ---------------------------------------------------------------------------


def _mk_requires_app_report():
    from devtools import hotreload

    return hotreload.ReloadReport(
        reloaded=["relay"],
        requires_app=["_RelayWorker: Signal set changed ({'x'}) — requires scope=app"],
    )


def test_menu_patch_requires_app_accept_rebuilds(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from devtools import qt_integration

    controller = window._hotreload
    report = _mk_requires_app_report()
    monkeypatch.setattr(controller, "do_reload", lambda: report.summary())
    controller._last_report = report

    calls = []
    monkeypatch.setattr(
        controller, "reload_app", lambda: (calls.append("app"), "reload-scheduled")[1]
    )
    monkeypatch.setattr(
        QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Yes
    )

    qt_integration._menu_patch(window, controller)
    assert calls == ["app"]   # 既定「app で再ビルド」→ reload_app 実行


def test_menu_patch_requires_app_ignore_does_not_rebuild(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from devtools import qt_integration

    controller = window._hotreload
    report = _mk_requires_app_report()
    monkeypatch.setattr(controller, "do_reload", lambda: report.summary())
    controller._last_report = report

    calls = []
    monkeypatch.setattr(controller, "reload_app", lambda: calls.append("app"))
    monkeypatch.setattr(
        QMessageBox, "exec", lambda self: QMessageBox.StandardButton.No
    )

    qt_integration._menu_patch(window, controller)
    assert calls == []   # 「無視」→ reload_app は呼ばれない


def test_menu_patch_soft_warning_uses_info_dialog(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from devtools import hotreload, qt_integration

    controller = window._hotreload
    report = hotreload.ReloadReport(
        reloaded=["m"],
        warnings=["C.__init__ changed — scope=app recommended"],
    )
    monkeypatch.setattr(controller, "do_reload", lambda: report.summary())
    controller._last_report = report

    info_calls = []
    monkeypatch.setattr(
        QMessageBox, "information", lambda *a, **k: info_calls.append(a)
    )
    rebuilt = []
    monkeypatch.setattr(controller, "reload_app", lambda: rebuilt.append("x"))

    qt_integration._menu_patch(window, controller)
    assert rebuilt == []          # requires_app 空 → 再ビルドしない
    assert len(info_calls) == 1   # 既存のソフト警告 info 経路のまま


def test_do_restart_aborts_before_side_effects_when_busy(window, monkeypatch):
    """Issue #101 E-2: 予約後に応答が始まっていたら、Popen・保存・quit の前に中止する。"""
    import llm_bridge.session as session
    from PySide6.QtWidgets import QApplication

    from devtools import qt_integration

    monkeypatch.setattr(window.chat_widget(), "is_busy", lambda: True)
    calls = []
    monkeypatch.setattr(session, "save_all", lambda w: calls.append("save_all") or ([], []))
    monkeypatch.setattr(qt_integration.subprocess, "Popen", lambda *a, **k: calls.append("popen"))
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: calls.append("quit")))
    logged = []
    monkeypatch.setattr(
        window._hotreload, "_log_reload_result",
        lambda cmd_id, status, tier, error=None: logged.append((cmd_id, status, tier, error)),
    )

    window._hotreload._do_restart("cmd-x")

    assert calls == []
    assert len(logged) == 1
    assert logged[0][:3] == ("cmd-x", "failed", 4)
    assert logged[0][3].startswith("reload-busy:")


def test_dev_menu_has_no_keyboard_shortcut(window):
    """Issue #101 E-5: 開発メニューの項目にはショートカットを割り当てない（誤操作防止）。"""
    from PySide6.QtGui import QAction, QKeySequence
    from PySide6.QtWidgets import QMenu

    from common.i18n import tr

    dev = [m for m in window.menuBar().findChildren(QMenu) if m.title() == tr("menu.dev")]
    assert len(dev) == 1
    assert dev[0].actions()
    assert all(a.shortcut().isEmpty() for a in dev[0].actions())
    assert all(
        a.shortcut() != QKeySequence("Ctrl+F5") for a in window.findChildren(QAction)
    )
