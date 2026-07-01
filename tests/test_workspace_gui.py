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
    """reviewer code P1: the NEW-tab path of add-tab must sync current_dataset,
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
