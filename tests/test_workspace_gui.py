"""Issue #51: multi-dataset workspace — verbs, close_dataset semantics, restore.

Uses a real ToolWindow (offscreen) with the window verbs wired via
llm_bridge._rewire_window (no command watcher). Analyses are throwaway files
under tmp_path; config.get_dataset_dir is patched so nothing touches real data.
"""
from __future__ import annotations

import json

import pytest

import llm_bridge


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _write_analysis(tmp_path, ds, name):
    d = tmp_path / ds / "analyses" / name
    d.mkdir(parents=True)
    (d / "analysis.py").write_text(
        "import llm_bridge\n"
        "from gui.tab import AnalysisTab\n"
        "def load():\n    return None\n"
        "def build_tab(parent, data):\n"
        f"    tab = AnalysisTab({name!r})\n"
        "    llm_bridge.attach_tab(tab, lambda: {})\n"
        "    return tab\n",
        encoding="utf-8",
    )


@pytest.fixture()
def win(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)
    from gui.window import ToolWindow
    w = ToolWindow()
    llm_bridge._rewire_window(w)   # wire window verbs (no command watcher)
    return w


# ---- verbs: list-open-datasets / set-active-dataset / close-dataset ----

def test_verbs_list_switch_close(win, tmp_path):
    _write_analysis(tmp_path, "dsA", "a")
    _write_analysis(tmp_path, "dsB", "b")
    assert win.dispatch_command("add-tab", name="a", dataset="dsA") == "added:a"
    assert win.dispatch_command("add-tab", name="b", dataset="dsB") == "added:b"

    res = win.dispatch_command("list-open-datasets")
    assert set(res["open"]) == {"dsA", "dsB"}

    assert win.dispatch_command("set-active-dataset", name="dsB") == "active:dsB"
    assert win.current_dataset == "dsB"
    assert win.dispatch_command("switch-dataset", name="dsA") == "active:dsA"  # alias
    assert win.current_dataset == "dsA"

    with pytest.raises(LookupError):
        win.dispatch_command("set-active-dataset", name="nope")

    assert win.dispatch_command("close-dataset", name="dsB") == "closed:dsB:1"
    assert "dsB" not in win.open_dataset_names()


def test_close_dataset_no_misclose(win, tmp_path):
    """dsA/dsB each hold a 'summary' tab; closing dsA leaves dsB's summary."""
    _write_analysis(tmp_path, "dsA", "summary")
    _write_analysis(tmp_path, "dsB", "summary")
    win.dispatch_command("add-tab", name="summary", dataset="dsA")
    win.dispatch_command("add-tab", name="summary", dataset="dsB")
    win.set_active_dataset("dsB")

    assert win.dispatch_command("close-dataset", name="dsA") == "closed:dsA:1"
    assert "dsA" not in win.open_dataset_names()
    assert win.find_tab("summary", "dsB") is not None   # dsB's summary survived


def test_close_tab_verb_dataset_scoped(win, tmp_path):
    _write_analysis(tmp_path, "dsA", "summary")
    _write_analysis(tmp_path, "dsB", "summary")
    win.dispatch_command("add-tab", name="summary", dataset="dsA")
    win.dispatch_command("add-tab", name="summary", dataset="dsB")

    win.dispatch_command("close-tab", name="summary", dataset="dsA")
    assert win.find_tab("summary", "dsA") is None
    assert win.find_tab("summary", "dsB") is not None


def test_close_dataset_flush_survives_save_all(win, tmp_path):
    """close_dataset flushes dsA's layout; a later save_all must NOT clobber it
    with empty tabs (forget_dataset removed it from _touched)."""
    _write_analysis(tmp_path, "dsA", "a")
    _write_analysis(tmp_path, "dsB", "b")
    win.dispatch_command("add-tab", name="a", dataset="dsA")
    win.dispatch_command("add-tab", name="b", dataset="dsB")

    assert win.dispatch_command("close-dataset", name="dsA") == "closed:dsA:1"

    from llm_bridge import session
    session.save_all(win)   # simulate exit-time save
    data = session.read_session("dsA")
    assert data is not None and [t["name"] for t in data["tabs"]] == ["a"]


def test_close_dataset_abort_on_save_failure(win, tmp_path, monkeypatch):
    _write_analysis(tmp_path, "dsA", "a")
    win.dispatch_command("add-tab", name="a", dataset="dsA")

    import gui.window as gw
    monkeypatch.setattr(gw.QMessageBox, "warning", lambda *a, **k: None)
    from llm_bridge import session
    monkeypatch.setattr(session, "save_dataset", lambda w, ds: False)

    # abort sentinel (-1) → verb returns error:<name>; group/tab retained.
    assert win.dispatch_command("close-dataset", name="dsA") == "error:dsA"
    assert "dsA" in win.open_dataset_names()
    assert win.find_tab("a", "dsA") is not None


def test_open_dataset_zero_tabs_comes_to_front(win, tmp_path):
    """A dataset with no session.json still opens as an (empty) front group."""
    (tmp_path / "dsA").mkdir()   # dataset dir exists, no session.json
    r = win.dispatch_command("open-dataset", name="dsA")
    assert r.startswith("no-session")
    assert "dsA" in win.open_dataset_names()
    assert win.current_dataset == "dsA"   # front even with zero tabs
    assert win.active_tab() is None


def test_restore_last_session_additive(win, tmp_path, monkeypatch):
    _write_analysis(tmp_path, "dsA", "a")
    _write_analysis(tmp_path, "dsB", "b")
    from llm_bridge import paths, session
    session.write_session("dsA", {
        "version": 1, "dataset": "dsA", "active_tab": "a",
        "tabs": [{"name": "a", "kind": "analysis", "module": "a"}]})
    session.write_session("dsB", {
        "version": 1, "dataset": "dsB", "active_tab": "b",
        "tabs": [{"name": "b", "kind": "analysis", "module": "b"}]})
    lw = tmp_path / "last_window.json"
    lw.write_text(json.dumps(
        {"version": 1, "datasets": ["dsA", "dsB"], "active": "dsB"}), encoding="utf-8")
    monkeypatch.setattr(paths, "last_window_path", lambda: lw)

    # Pre-open dsA to prove restore is ADDITIVE (dsA not closed / duplicated).
    win.dispatch_command("open-dataset", name="dsA")
    win._restore_last_session()

    assert set(win.open_dataset_names()) == {"dsA", "dsB"}
    assert win.current_dataset == "dsB"          # active restored
    assert win.tab_names().count("a") == 1       # dsA not duplicated
    assert win.find_tab("b", "dsB") is not None


def test_add_tab_new_path_syncs_current_dataset(win, tmp_path, monkeypatch):
    """The NEW-tab path of add-tab must sync current_dataset,
    like the idempotent path and _show. Without it, window.add_tab only does
    _ensure_group+addTab (Qt auto-selects the first group's tab as active) while
    current_dataset stays None → active.json publishes dataset:null with
    active_analysis_dataset:dsA (violating the "active tab is in current group"
    invariant), chat never switches, and a follow-up dataset-less add-tab raises
    'no dataset open' — degrading the #51 marquee flow."""
    _write_analysis(tmp_path, "dsA", "a")
    _write_analysis(tmp_path, "dsA", "b")
    assert win.current_dataset is None                 # fresh window
    assert win.dispatch_command("add-tab", name="a", dataset="dsA") == "added:a"
    # the new tab's dataset becomes the explicit current dataset (was None = bug).
    assert win.current_dataset == "dsA"
    # active.json serializes it (dataset / active_dataset == dsA, not null).
    active = tmp_path / "active.json"
    monkeypatch.setattr(llm_bridge, "active_state_path", lambda: active)
    llm_bridge._write_active(win)
    data = json.loads(active.read_text(encoding="utf-8"))
    assert data["dataset"] == "dsA"
    assert data["active_dataset"] == "dsA"
    # a follow-up add-tab WITHOUT dataset= now resolves via current_dataset
    # (raised ValueError 'no dataset open' before the fix).
    assert win.dispatch_command("add-tab", name="b") == "added:b"


def test_close_tab_reseeds_when_window_empties(win):
    """#55: 非プレースホルダの dataset-less タブが唯一のタブで実データセットも無い
    場合、close_tab はゼロタブ窓を作らず (empty) を再生成する。"""
    from gui.tab import AnalysisTab
    win.add_tab(AnalysisTab("orphan"))              # → None グループ（唯一のタブ）
    # 前提固定: win は素の ToolWindow()（create_main_window 非経由）で (empty) 未種まき。
    # orphan のみが載ることを確認し、close 後の names()==["(empty)"] が既存 (empty) 残存
    # ではなく真の再シード結果であることを保証する（fixture 変化時の偽陽性 pass 防止）。
    assert win._groups[None].names() == ["orphan"]
    assert win.close_tab("orphan") is True
    grp = win._groups.get(None)
    assert grp is not None
    assert grp.names() == ["(empty)"]               # 再シード済み
    _, ph = grp.find("(empty)")
    assert ph is not None and ph.is_placeholder is True
    assert win.current_dataset is None              # None グループが前面


def test_close_tab_no_reseed_when_real_dataset_open(win, tmp_path):
    """#55: dataset-less タブを閉じても実データセットが開いていれば (empty) は作らず、
    空の None グループを除去して実データセットへ再アンカーする。"""
    from gui.tab import AnalysisTab
    _write_analysis(tmp_path, "dsA", "a")
    win.dispatch_command("add-tab", name="a", dataset="dsA")  # dsA グループ
    win.add_tab(AnalysisTab("orphan"))                        # None グループ
    win._select_dataset_group(None)                           # None を前面に
    assert win.close_tab("orphan", dataset=None) is True
    assert None not in win._groups                            # 空 None グループを除去
    assert "dsA" in win.open_dataset_names()                  # dsA は健在
    assert win.current_dataset == "dsA"                       # 実データセットへ再アンカー


def test_close_tab_keeps_zero_tab_real_dataset(win, tmp_path):
    """#55: 実データセットの最後のタブを閉じても、そのデータセットはゼロタブの開き状態
    として残す（(empty) は作らない = _ensure_group 不変条件を維持）。これは新コード経路の
    結果弁別ではなく、grp.name is not None 分岐が (empty) を作らない不変条件を将来の
    リグレッションから守る回帰ガード（この分岐は現行 close_tab でも実質 no-op のため
    fix 無しでも pass する）。"""
    _write_analysis(tmp_path, "dsA", "a")
    win.dispatch_command("add-tab", name="a", dataset="dsA")
    assert win.close_tab("a", dataset="dsA") is True
    assert "dsA" in win.open_dataset_names()
    grpA = win._groups.get("dsA")
    assert grpA is not None and grpA.tabs.count() == 0
    assert None not in win._groups                            # (empty) は作らない


# ---- Issue #90: 復元スキップの警告表示 + ログ ----

def _patch_warning(monkeypatch):
    """gw.QMessageBox.warning を記録用に差し替え、(呼び出し本文の) list を返す。"""
    import gui.window as gw
    calls: list[str] = []
    monkeypatch.setattr(
        gw.QMessageBox, "warning",
        lambda parent, title, body, *a, **k: calls.append(body),
    )
    return calls


def test_restore_partial_failure_warns_and_continues(win, tmp_path, monkeypatch):
    """dsA は不在（error:）、dsB はディレクトリのみ（no-session:）。dsB は復元・active
    になり、警告が 1 回・本文に dsA のみ列挙され dsB は含まれない。"""
    from llm_bridge import paths
    (tmp_path / "dsB").mkdir()             # dsB: dir only → no-session:dsB
    # dsA: no dir → error:dsA
    lw = tmp_path / "last_window.json"
    lw.write_text(json.dumps(
        {"version": 1, "datasets": ["dsA", "dsB"], "active": "dsB"}), encoding="utf-8")
    monkeypatch.setattr(paths, "last_window_path", lambda: lw)
    warns = _patch_warning(monkeypatch)

    win._restore_last_session()

    assert win.current_dataset == "dsB"
    assert "dsB" in win.open_dataset_names()
    assert len(warns) == 1
    assert "dsA" in warns[0]
    assert "dsB" not in warns[0]


def test_restore_all_fail_still_warns(win, tmp_path, monkeypatch):
    """dsA/dsB とも不在（両方 error:）。何も開かないが警告は必ず 1 回出る。"""
    from llm_bridge import paths
    lw = tmp_path / "last_window.json"
    lw.write_text(json.dumps(
        {"version": 1, "datasets": ["dsA", "dsB"], "active": "dsA"}), encoding="utf-8")
    monkeypatch.setattr(paths, "last_window_path", lambda: lw)
    warns = _patch_warning(monkeypatch)

    win._restore_last_session()

    assert "dsA" not in win.open_dataset_names()
    assert "dsB" not in win.open_dataset_names()
    assert len(warns) == 1
    assert "dsA" in warns[0] and "dsB" in warns[0]


def test_restore_dispatch_exception_logged_and_listed(
    win, tmp_path, monkeypatch, caplog
):
    """dispatch_command が特定 DS で例外 → _log.exception が記録され、その DS が
    警告本文に列挙される。"""
    import logging
    from llm_bridge import paths
    _write_analysis(tmp_path, "dsA", "a")
    from llm_bridge import session
    session.write_session("dsA", {
        "version": 1, "dataset": "dsA", "active_tab": "a",
        "tabs": [{"name": "a", "kind": "analysis", "module": "a"}]})
    lw = tmp_path / "last_window.json"
    lw.write_text(json.dumps(
        {"version": 1, "datasets": ["dsBoom", "dsA"], "active": "dsA"}), encoding="utf-8")
    monkeypatch.setattr(paths, "last_window_path", lambda: lw)
    warns = _patch_warning(monkeypatch)

    orig = win.dispatch_command

    def boom(verb, **kw):
        if verb == "open-dataset" and kw.get("name") == "dsBoom":
            raise RuntimeError("boom")
        return orig(verb, **kw)

    monkeypatch.setattr(win, "dispatch_command", boom)

    with caplog.at_level(logging.WARNING, logger="gui.window"):
        win._restore_last_session()

    assert "dsA" in win.open_dataset_names()
    assert len(warns) == 1
    assert "dsBoom" in warns[0]
    assert any("dsBoom" in r.getMessage() for r in caplog.records)


def test_open_dataset_dialog_error_warns(win, tmp_path, monkeypatch):
    """_open_dataset 単体: ダイアログが実体無し DS を返す → error: 結果で警告 1 回・
    本文に当該 DS 名。"""
    import config
    monkeypatch.setattr(config, "reload_datasets", lambda: None)
    monkeypatch.setattr(config, "DATASETS", {"dsGhost": {}})
    warns = _patch_warning(monkeypatch)

    from PySide6.QtWidgets import QDialog
    import gui.open_dataset_dialog as odd

    class _FakeDialog:
        def __init__(self, parent=None):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selected_dataset(self):
            return "dsGhost"

    monkeypatch.setattr(odd, "OpenDatasetDialog", _FakeDialog)

    win._open_dataset()

    assert len(warns) == 1
    assert "dsGhost" in warns[0]


# ---- session.json 破損（unreadable ≠ no-session）の顕在化 ----

def test_restore_unreadable_session_warns_but_opens(win, tmp_path, monkeypatch):
    """0 バイト session.json の DS → 警告 1 回・本文に DS 名。DS 自体は開き
    active になれる（タブだけ未復元）。破損ファイルは上書きされない。"""
    from llm_bridge import paths
    wd = tmp_path / "dsB" / "_work"
    wd.mkdir(parents=True)
    (wd / "session.json").write_text("", encoding="utf-8")
    lw = tmp_path / "last_window.json"
    lw.write_text(json.dumps(
        {"version": 1, "datasets": ["dsB"], "active": "dsB"}), encoding="utf-8")
    monkeypatch.setattr(paths, "last_window_path", lambda: lw)
    warns = _patch_warning(monkeypatch)

    win._restore_last_session()

    assert "dsB" in win.open_dataset_names()
    assert win.current_dataset == "dsB"
    assert len(warns) == 1
    assert "dsB" in warns[0]
    assert (wd / "session.json").read_text(encoding="utf-8") == ""


def test_open_dataset_dialog_unreadable_warns(win, tmp_path, monkeypatch):
    """_open_dataset 単体: 破損 session.json の DS → 警告 1 回・本文に DS 名、
    DS は開く。"""
    import config
    wd = tmp_path / "dsU" / "_work"
    wd.mkdir(parents=True)
    (wd / "session.json").write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "reload_datasets", lambda: None)
    monkeypatch.setattr(config, "DATASETS", {"dsU": {}})
    warns = _patch_warning(monkeypatch)

    from PySide6.QtWidgets import QDialog
    import gui.open_dataset_dialog as odd

    class _FakeDialog:
        def __init__(self, parent=None):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selected_dataset(self):
            return "dsU"

    monkeypatch.setattr(odd, "OpenDatasetDialog", _FakeDialog)

    win._open_dataset()

    assert len(warns) == 1
    assert "dsU" in warns[0]
    assert "dsU" in win.open_dataset_names()


# ---- Issue #59: DS タブのドラッグ並べ替え＋順序永続化 ----

def _open_three(win, tmp_path):
    """dsA/dsB/dsC を挿入順に open（switcher タブ [dsA, dsB, dsC]）。"""
    for ds, name in (("dsA", "a"), ("dsB", "b"), ("dsC", "c")):
        _write_analysis(tmp_path, ds, name)
        win.dispatch_command("add-tab", name=name, dataset=ds)


def test_dataset_reorder_syncs_groups_and_marks_dirty(win, tmp_path):
    _open_three(win, tmp_path)
    win._session_dirty = False                       # add-tab が dirty 済み → リセット
    win._switcher.moveTab(0, 2)                       # dsA を末尾へ ⇒ [dsB, dsC, dsA]
    assert win.open_dataset_names() == ["dsB", "dsC", "dsA"]
    assert win._session_dirty is True
    assert win.dispatch_command("list-open-datasets")["open"] == ["dsB", "dsC", "dsA"]


def test_dataset_reorder_switch_still_correct(win, tmp_path):
    _open_three(win, tmp_path)
    win._switcher.moveTab(0, 2)                       # ⇒ [dsB, dsC, dsA]
    win.set_active_dataset("dsA")
    assert win.current_dataset == "dsA"
    assert win._switcher.tabData(win._switcher.currentIndex()) == "dsA"


def test_dataset_reorder_persists_to_last_window(win, tmp_path, monkeypatch):
    _open_three(win, tmp_path)
    win._switcher.moveTab(0, 2)                       # ⇒ [dsB, dsC, dsA]
    from llm_bridge import paths as lb_paths, session
    lw = tmp_path / "last_window.json"
    monkeypatch.setattr(lb_paths, "last_window_path", lambda: lw)
    session.write_last_window(win)
    assert json.loads(lw.read_text(encoding="utf-8"))["datasets"] == ["dsB", "dsC", "dsA"]


def test_dataset_order_restored(win, tmp_path, monkeypatch):
    _write_analysis(tmp_path, "dsB", "b")
    _write_analysis(tmp_path, "dsA", "a")
    from llm_bridge import paths, session
    session.write_session("dsB", {
        "version": 1, "dataset": "dsB", "active_tab": "b",
        "tabs": [{"name": "b", "kind": "analysis", "module": "b"}]})
    session.write_session("dsA", {
        "version": 1, "dataset": "dsA", "active_tab": "a",
        "tabs": [{"name": "a", "kind": "analysis", "module": "a"}]})
    lw = tmp_path / "last_window.json"
    lw.write_text(json.dumps(
        {"version": 1, "datasets": ["dsB", "dsA"], "active": "dsB"}), encoding="utf-8")
    monkeypatch.setattr(paths, "last_window_path", lambda: lw)

    win._restore_last_session()                      # pre-open せず純粋な保存順再現
    assert win.open_dataset_names() == ["dsB", "dsA"]


def test_dataset_switcher_is_movable(win):
    assert win._switcher.isMovable() is True


def test_dataset_reorder_keeps_none_group(win, tmp_path):
    from gui.tab import AnalysisTab
    _write_analysis(tmp_path, "dsA", "a")
    _write_analysis(tmp_path, "dsB", "b")
    win.dispatch_command("add-tab", name="a", dataset="dsA")
    win.dispatch_command("add-tab", name="b", dataset="dsB")
    win.add_tab(AnalysisTab("orphan"))               # None グループ（switcher タブ無し）
    win._switcher.moveTab(0, 1)                       # [dsA, dsB] ⇒ [dsB, dsA]
    assert win.open_dataset_names() == ["dsB", "dsA"]
    assert None in win._groups
    assert list(win._groups)[-1] is None
    assert win.find_tab("orphan", dataset=None) is not None


# --------------------------------------------------------------------------- #
# 生成中の終了確認（Issue #101 E-2）
# --------------------------------------------------------------------------- #
class _FakeChat:
    def __init__(self, busy: bool):
        self.busy = busy

    def is_busy(self) -> bool:
        return self.busy


def _stub_question(monkeypatch, replies, on_ask=None):
    """QMessageBox.question を差し替え、(title, default) を記録して replies を順に返す。

    on_ask(title) は返答の直前に呼ぶ（ダイアログ表示中に状態が変わるのを再現する）。
    """
    from PySide6.QtWidgets import QMessageBox

    asked = []

    def fake(parent, title, text, buttons=None, default=None):
        asked.append((title, default))
        if on_ask is not None:
            on_ask(title)
        return replies.pop(0)

    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake))
    return asked


def _spy_close_side_effects(monkeypatch, win):
    stopped = []
    for attr in ("_stop_meeting_relay", "_stop_config_pusher", "_close_all_floats"):
        monkeypatch.setattr(win, attr, lambda a=attr: stopped.append(a))
    return stopped


_ALL_STOPPED = ["_stop_meeting_relay", "_stop_config_pusher", "_close_all_floats"]


def test_close_while_busy_no_keeps_window(win, monkeypatch):
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QMessageBox

    from common.i18n import tr

    stopped = _spy_close_side_effects(monkeypatch, win)
    chat = _FakeChat(True)
    monkeypatch.setattr(win, "chat_widget", lambda: chat)
    win.clear_session_dirty()
    asked = _stub_question(monkeypatch, [QMessageBox.StandardButton.No])
    ev = QCloseEvent()
    win.closeEvent(ev)
    assert not ev.isAccepted()
    assert asked == [(tr("dlg.quit_busy.title"), QMessageBox.StandardButton.No)]
    assert stopped == []


def test_close_while_busy_yes_closes(win, monkeypatch):
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QMessageBox

    from common.i18n import tr

    stopped = _spy_close_side_effects(monkeypatch, win)
    chat = _FakeChat(True)
    monkeypatch.setattr(win, "chat_widget", lambda: chat)
    win.clear_session_dirty()
    asked = _stub_question(monkeypatch, [QMessageBox.StandardButton.Yes])
    ev = QCloseEvent()
    win.closeEvent(ev)
    assert ev.isAccepted()
    assert [t for t, _ in asked] == [tr("dlg.quit_busy.title")]
    assert stopped == _ALL_STOPPED


def test_close_while_busy_yes_then_unsaved_prompt(win, monkeypatch):
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QMessageBox

    from common.i18n import tr

    stopped = _spy_close_side_effects(monkeypatch, win)
    chat = _FakeChat(True)
    monkeypatch.setattr(win, "chat_widget", lambda: chat)
    win.set_session_saver(lambda: ([], []))
    win.mark_session_dirty()
    asked = _stub_question(
        monkeypatch,
        [QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Cancel],
    )
    ev = QCloseEvent()
    win.closeEvent(ev)
    assert not ev.isAccepted()
    assert [t for t, _ in asked] == [tr("dlg.quit_busy.title"), tr("dlg.unsaved.title")]
    assert stopped == []


def test_close_when_idle_skips_busy_prompt(win, monkeypatch):
    from PySide6.QtGui import QCloseEvent

    stopped = _spy_close_side_effects(monkeypatch, win)
    chat = _FakeChat(False)
    monkeypatch.setattr(win, "chat_widget", lambda: chat)
    win.clear_session_dirty()
    asked = _stub_question(monkeypatch, [])
    ev = QCloseEvent()
    win.closeEvent(ev)
    assert ev.isAccepted()
    assert asked == []
    assert stopped == _ALL_STOPPED


def test_close_without_chat_widget_skips_busy_prompt(win, monkeypatch):
    from PySide6.QtGui import QCloseEvent

    stopped = _spy_close_side_effects(monkeypatch, win)
    win.clear_session_dirty()
    asked = _stub_question(monkeypatch, [])
    ev = QCloseEvent()
    win.closeEvent(ev)
    assert ev.isAccepted()
    assert asked == []
    assert stopped == _ALL_STOPPED


def test_close_busy_consent_is_not_asked_twice(win, monkeypatch):
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QMessageBox

    from common.i18n import tr

    stopped = _spy_close_side_effects(monkeypatch, win)
    chat = _FakeChat(True)
    monkeypatch.setattr(win, "chat_widget", lambda: chat)
    win.set_session_saver(lambda: ([], []))
    win.mark_session_dirty()
    asked = _stub_question(
        monkeypatch,
        [QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No],
    )
    ev = QCloseEvent()
    win.closeEvent(ev)
    assert ev.isAccepted()
    assert [t for t, _ in asked] == [tr("dlg.quit_busy.title"), tr("dlg.unsaved.title")]
    assert stopped == _ALL_STOPPED


def _start_busy_during_unsaved(monkeypatch, win, replies):
    from common.i18n import tr

    chat = _FakeChat(False)
    monkeypatch.setattr(win, "chat_widget", lambda: chat)
    saves = []
    win.set_session_saver(lambda: saves.append(1) or ([], []))
    win.mark_session_dirty()

    def on_ask(title):
        if title == tr("dlg.unsaved.title"):
            chat.busy = True

    asked = _stub_question(monkeypatch, replies, on_ask=on_ask)
    return asked, saves


def test_close_generation_started_during_unsaved_prompt_no_keeps_window(win, monkeypatch):
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QMessageBox

    from common.i18n import tr

    stopped = _spy_close_side_effects(monkeypatch, win)
    asked, saves = _start_busy_during_unsaved(
        monkeypatch, win,
        [QMessageBox.StandardButton.No, QMessageBox.StandardButton.No],
    )
    ev = QCloseEvent()
    win.closeEvent(ev)
    assert not ev.isAccepted()
    assert asked == [
        (tr("dlg.unsaved.title"), None),
        (tr("dlg.quit_busy.title"), QMessageBox.StandardButton.No),
    ]
    assert stopped == []
    assert saves == []


def test_close_generation_started_during_unsaved_prompt_yes_closes(win, monkeypatch):
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QMessageBox

    from common.i18n import tr

    stopped = _spy_close_side_effects(monkeypatch, win)
    asked, saves = _start_busy_during_unsaved(
        monkeypatch, win,
        [QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Yes],
    )
    ev = QCloseEvent()
    win.closeEvent(ev)
    assert ev.isAccepted()
    assert [t for t, _ in asked] == [tr("dlg.unsaved.title"), tr("dlg.quit_busy.title")]
    assert saves == [1]
    assert stopped == _ALL_STOPPED


# --------------------------------------------------------------------------- #
# ファイル → データセットを閉じる（Issue #101 E-3）
# --------------------------------------------------------------------------- #
def test_file_menu_close_dataset(win, tmp_path):
    """Issue #101 E-3: ファイル → データセットを閉じる は前面のデータセットを閉じ、
    切替バーが出ない 1 つだけの状態でも使える。"""
    from common.i18n import tr

    _write_analysis(tmp_path, "dsA", "a")
    _write_analysis(tmp_path, "dsB", "b")
    act = win._close_dataset_action
    assert act in win._file_menu.actions()
    assert act.text() == tr("menu.file.close_dataset")
    assert not act.isEnabled()                      # 何も開いていない

    win.dispatch_command("add-tab", name="a", dataset="dsA")
    win.dispatch_command("add-tab", name="b", dataset="dsB")
    win.dispatch_command("set-active-dataset", name="dsB")
    assert act.isEnabled()

    act.trigger()                                   # 前面の dsB だけ閉じる
    assert win.open_dataset_names() == ["dsA"]
    assert win.current_dataset == "dsA"
    assert win._switcher.isHidden()                 # 1 つだけ＝切替バーは出ない
    assert act.isEnabled()

    act.trigger()                                   # 最後の 1 つも閉じられる
    assert win.open_dataset_names() == []
    assert win.current_dataset is None
    assert not act.isEnabled()


# ---- Issue #106 A-4: 壊れた session.json の上書き確認 ----

def _unreadable_ds_with_tab(win, tmp_path, monkeypatch):
    import config
    from llm_bridge import session
    wd = tmp_path / "dsU" / "_work"
    wd.mkdir(parents=True)
    (wd / "session.json").write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "reload_datasets", lambda *a, **k: None)
    monkeypatch.setattr(config, "DATASETS", {"dsU": {}})
    assert session.open_dataset(win, "dsU") == "unreadable-session:dsU"
    _write_analysis(tmp_path, "dsU", "a")
    assert win.dispatch_command("add-tab", name="a", dataset="dsU") == "added:a"
    win.set_session_saver(lambda: session.save_all(win))
    return wd


def _patch_question(monkeypatch, answer):
    import gui.window as gw
    monkeypatch.setattr(gw.QMessageBox, "question", lambda *a, **k: answer)


def test_save_session_overwrite_unreadable_yes(win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from llm_bridge import session
    wd = _unreadable_ds_with_tab(win, tmp_path, monkeypatch)
    _patch_question(monkeypatch, QMessageBox.StandardButton.Yes)
    _patch_warning(monkeypatch)
    win._save_session()
    assert (wd / "session.json").read_text(encoding="utf-8") != ""
    assert [t["name"] for t in session.read_session("dsU")["tabs"]] == ["a"]


def test_save_session_overwrite_unreadable_no(win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    wd = _unreadable_ds_with_tab(win, tmp_path, monkeypatch)
    _patch_question(monkeypatch, QMessageBox.StandardButton.No)
    _patch_warning(monkeypatch)
    win._save_session()
    assert (wd / "session.json").read_text(encoding="utf-8") == ""
    assert "dsU" in win.statusBar().currentMessage()


# ---- Issue #111: コマンドキュー経由の dataset 既定（caller_dataset） ----

@pytest.fixture()
def queue_log(monkeypatch, tmp_path):
    from llm_bridge import commands
    log = tmp_path / "command_log.jsonl"
    monkeypatch.setattr(commands, "command_log_path", lambda: log)
    monkeypatch.setattr(commands, "rotated_command_log_path",
                        lambda: tmp_path / "command_log.1.jsonl")
    return log


def _queue(win, log, tier, verb, args, *, target=None, caller=None) -> dict:
    """commands._execute でキュー経路を通し、その監査ログ行を返す。"""
    import uuid
    from llm_bridge import commands
    payload = {"id": uuid.uuid4().hex, "ts": "t", "tier": tier, "target": target,
               "verb": verb, "args": args}
    if caller is not None:
        payload["caller_dataset"] = caller
    commands._execute(win, payload)
    entries = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]
    return next(e for e in entries if e["id"] == payload["id"])


def _png(tmp_path, name="p.png"):
    from PySide6.QtGui import QPixmap
    p = tmp_path / name
    assert QPixmap(10, 10).save(str(p))
    return p


def test_queue_show_resolves_dataset_with_and_without_caller(
        win, tmp_path, monkeypatch, queue_log):
    _write_analysis(tmp_path, "dsA", "a")
    _write_analysis(tmp_path, "dsB", "b")
    assert win.dispatch_command("add-tab", name="a", dataset="dsA") == "added:a"
    assert win.dispatch_command("add-tab", name="b", dataset="dsB") == "added:b"
    win.dispatch_command("set-active-dataset", name="dsB")
    monkeypatch.setattr("llm_bridge.session.infer_dataset", lambda p: "dsA")
    png = _png(tmp_path)

    e1 = _queue(win, queue_log, "window", "show", {"path": str(png), "name": "fig1"})
    assert e1["status"] == "ok", e1
    assert win.find_tab("fig1", None).session_spec["dataset"] == "dsA"   # パス推定 > 前面

    e2 = _queue(win, queue_log, "window", "show", {"path": str(png), "name": "fig2"},
                caller="dsB")
    assert e2["status"] == "ok", e2
    assert win.find_tab("fig2", None).session_spec["dataset"] == "dsB"   # チャットの DS > 推定


def test_queue_without_caller_keeps_existing_lookup(win, tmp_path, monkeypatch, queue_log):
    _write_analysis(tmp_path, "dsA", "a")
    _write_analysis(tmp_path, "dsB", "b")
    assert win.dispatch_command("add-tab", name="a", dataset="dsA") == "added:a"
    png = _png(tmp_path)
    png2 = _png(tmp_path, "p2.png")
    assert win.dispatch_command("show", path=str(png), name="fig", dataset="dsA") \
        == "shown:fig"
    fig = win.find_tab("fig", "dsA")
    assert fig is not None
    monkeypatch.setattr("llm_bridge.session.infer_dataset", lambda p: "dsB")

    e = _queue(win, queue_log, "window", "show", {"path": str(png2), "name": "fig"})
    assert e["status"] == "ok", e
    assert e["result"] == "updated:fig"
    assert win.find_tab("fig", "dsA") is fig
    assert win.find_tab("fig", "dsB") is None

    # 他 DS の唯一一致（前面は dsA のまま）
    assert win.dispatch_command("add-tab", name="b", dataset="dsB") == "added:b"
    win.dispatch_command("set-active-dataset", name="dsA")
    e = _queue(win, queue_log, "tab", "list-panes", {}, target="b")
    assert e["status"] == "ok", e


def test_queue_defaulted_dataset_outcomes(win, tmp_path, queue_log):
    _write_analysis(tmp_path, "dsA", "a")
    _write_analysis(tmp_path, "dsB", "b")
    assert win.dispatch_command("add-tab", name="a", dataset="dsA") == "added:a"
    assert win.dispatch_command("add-tab", name="b", dataset="dsB") == "added:b"
    win.dispatch_command("set-active-dataset", name="dsB")

    e = _queue(win, queue_log, "window", "set-active-tab", {"name": "a"}, caller="dsB")
    assert e["status"] == "ok", e
    assert e["result"] is False
    assert win.current_dataset == "dsB"     # dsA の a を前面にしない

    e = _queue(win, queue_log, "window", "set-active-tab", {"name": "a"}, caller="dsZ")
    assert e["status"] == "error"
    assert "is not open" in e["error"]
