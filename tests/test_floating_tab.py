"""Issue #56: analysis-tab floating (browser-style tear-off) — acceptance tests.

Covers the design's Verification contract: the tear-off boundary predicate,
detach→float wiring, the enumeration/membership invariant (object identity, no
double/zero count), currentChanged single-membership on the synchronous re-entry,
the two P1 regressions fixed in design review (canonical key, _reanchor data
loss), plus lifecycle/leak and the analysis-only detach gate.

Hermetic: offscreen platform + config.get_dataset_dir patched to tmp_path, so no
real dataset / session file is touched. Tab names are globally unique within
each test (name-based counts assume this; the data model allows same-name across
datasets, so cross-tab dedup is by object identity, not by name).
"""
from __future__ import annotations

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


def _add(win, ds, name, tmp_path):
    _write_analysis(tmp_path, ds, name)
    return win.dispatch_command("add-tab", name=name, dataset=ds)


def _docked_names(grp):
    # Real docked tabs only — skip float-position stubs (#56), which hold the
    # slot+label of a floated tab but are not enumerated as real tabs.
    return [
        grp.tabs.widget(i).name
        for i in range(grp.tabs.count())
        if not getattr(grp.tabs.widget(i), "is_float_stub", False)
    ]


def _bar_names(grp):
    """Raw tab-bar labels including any float-position stubs (#56)."""
    return [grp.tabs.tabText(i) for i in range(grp.tabs.count())]


def _n_float_windows(win, qapp):
    """Live FloatingTabWindows belonging to *win* (scoped so a shared-QApplication
    session isn't polluted by float windows other tests left open).

    Float windows are ownerless top-levels (no Qt parent — #56 dropped the owner
    to avoid always-on-top), so they can't be found via win.findChildren(); scan
    topLevelWidgets() and scope by their _main back-reference instead."""
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication
    from gui.floating_window import FloatingTabWindow
    qapp.processEvents()
    # deleteLater posts a DeferredDelete event that processEvents() does not
    # drain on its own — force it so destroyed windows actually drop out.
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    return sum(
        1
        for w in QApplication.topLevelWidgets()
        if isinstance(w, FloatingTabWindow) and getattr(w, "_main", None) is win
    )


# --------------------------------------------------------------------------- #
# 1. tear-off boundary predicate (pure, offscreen unit)                       #
# --------------------------------------------------------------------------- #

def test_outside_bar_vertical_boundary(qapp):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QTabWidget, QWidget
    from gui.tabbar import MultiRowTabBar
    host = QTabWidget()
    bar = MultiRowTabBar()
    host.setTabBar(bar)
    for i in range(3):
        host.addTab(QWidget(), f"Tab{i}")
    host.resize(360, 700)
    bar._relayout(360)
    bar.resize(360, 40)
    h = bar.height()
    rh = bar._row_height
    assert bar._outside_bar(QPoint(10, h // 2)) is False        # inside the bar
    assert bar._outside_bar(QPoint(10, -rh - 5)) is True         # above by > row_height
    assert bar._outside_bar(QPoint(10, h + rh + 5)) is True      # below by > row_height
    assert bar._outside_bar(QPoint(10, 0)) is False             # top edge, inside


# --------------------------------------------------------------------------- #
# 2. detach signal (name-based) → float_tab wiring                            #
# --------------------------------------------------------------------------- #

def test_detach_signal_floats_by_name(win, tmp_path):
    from PySide6.QtCore import QPoint
    _add(win, "ds", "T", tmp_path)
    grp = win._groups["ds"]
    grp.tabs.tabBar().tabDetachRequested.emit("T", QPoint(100, 100))
    assert ("ds", "T") in win._float_windows
    assert "T" in grp._floated
    assert "T" not in _docked_names(grp)
    assert "T" in win.tab_names()             # still enumerated (single source)


# --------------------------------------------------------------------------- #
# 3. enumeration contract — no double-count / zero-count (object identity)     #
# --------------------------------------------------------------------------- #

def test_enumeration_single_membership(win, tmp_path):
    _add(win, "ds", "U", tmp_path)
    _add(win, "ds", "T", tmp_path)
    win.set_active_tab("U", dataset="ds")
    assert win.float_tab("T", dataset="ds") is True
    names = [w.name for w in win.tabs()]
    assert names.count("T") == 1                      # exactly once, not 0 or 2
    objs = win.tabs()
    assert len(objs) == len({id(w) for w in objs})    # no duplicate widget (identity)
    assert win.tab_names().count("T") == 1
    assert win.find_tab("T", "ds") is not None
    assert win.active_tab().name == "U"               # float is not active


# --------------------------------------------------------------------------- #
# 4. currentChanged synchronous re-entry sees single membership               #
# --------------------------------------------------------------------------- #

def test_current_changed_reentry_count_is_one(win, tmp_path):
    _add(win, "ds", "U", tmp_path)
    _add(win, "ds", "T", tmp_path)
    win.set_active_tab("T", dataset="ds")            # T current so removeTab fires currentChanged
    grp = win._groups["ds"]
    counts: list[int] = []
    grp.tabs.currentChanged.connect(
        lambda _i: counts.append([w.name for w in win.tabs()].count("T"))
    )
    win.float_tab("T", dataset="ds")
    assert counts                                    # currentChanged did fire
    assert all(c == 1 for c in counts)               # T counted exactly once throughout


# --------------------------------------------------------------------------- #
# 5. canonical key: public float_tab("T") resolves to grp.name, not raw None   #
# --------------------------------------------------------------------------- #

def test_float_public_path_uses_canonical_key(win, qapp, tmp_path):
    _add(win, "D", "T", tmp_path)
    win.set_active_dataset("D")
    assert win.float_tab("T") is True                # dataset omitted = public path
    assert ("D", "T") in win._float_windows
    assert (None, "T") not in win._float_windows     # NOT the raw dataset=None key
    # [X] must re-dock cleanly (no zombie window, key matches on the read side)
    win._float_windows[("D", "T")].close()
    assert _n_float_windows(win, qapp) == 0
    assert ("D", "T") not in win._float_windows
    assert "T" in _docked_names(win._groups["D"])    # docked back into D


# --------------------------------------------------------------------------- #
# 6. _reanchor data-loss regression: floated tab survives None-group close     #
# --------------------------------------------------------------------------- #

def test_reanchor_keeps_floated_in_none_group(win, tmp_path):
    from gui.tab import AnalysisTab
    _add(win, "D", "d1", tmp_path)                   # a real dataset stays open
    win.add_tab(AnalysisTab("T1"))                   # no session_spec → None group (docked)
    win.add_tab(AnalysisTab("T2"))
    assert None in win._groups
    assert set(_docked_names(win._groups[None])) == {"T1", "T2"}
    assert win.float_tab("T2", dataset=None) is True  # None-group tab floated
    win.close_tab("T1", dataset=None)                # closes the last DOCKED None-group tab
    assert None in win._groups                        # None group NOT destroyed
    assert "T2" in win._groups[None]._floated
    assert "T2" in [w.name for w in win.tabs()]      # T2 not dropped from the single source


# --------------------------------------------------------------------------- #
# 7. lifecycle / leak: [X] re-docks, close_tab destroys — no orphan windows    #
# --------------------------------------------------------------------------- #

def test_lifecycle_no_orphan_windows(win, qapp, tmp_path):
    _add(win, "ds", "T", tmp_path)
    win.float_tab("T", dataset="ds")
    assert _n_float_windows(win, qapp) == 1
    # [X] → re-dock, window gone
    win._float_windows[("ds", "T")].close()
    assert _n_float_windows(win, qapp) == 0
    assert win.find_tab("T", "ds") is not None        # re-docked, not destroyed
    assert not win._groups["ds"]._floated
    # float again, then close_tab → window AND tab destroyed
    win.float_tab("T", dataset="ds")
    assert _n_float_windows(win, qapp) == 1
    assert win.close_tab("T", dataset="ds") is True
    assert _n_float_windows(win, qapp) == 0
    assert win.find_tab("T", "ds") is None            # gone from enumeration too


def test_close_dataset_destroys_floats(win, qapp, tmp_path):
    _add(win, "ds", "T", tmp_path)
    win.float_tab("T", dataset="ds")
    assert _n_float_windows(win, qapp) == 1
    win.close_dataset("ds")
    assert _n_float_windows(win, qapp) == 0
    assert "ds" not in win.open_dataset_names()
    assert win.find_tab("T", "ds") is None


# --------------------------------------------------------------------------- #
# 8. hermetic save→restore: floated tab persists and comes back docked         #
# --------------------------------------------------------------------------- #

def test_save_all_includes_floated_tab(win, tmp_path):
    from llm_bridge import session
    _add(win, "ds", "T", tmp_path)
    win.float_tab("T", dataset="ds")
    # save_dataset iterates window.tabs() (which folds floats) and filters by
    # dataset — scoped so it round-trips just this dataset hermetically.
    assert session.save_dataset(win, "ds") is True
    payload = session.read_session("ds")
    assert payload is not None
    assert "T" in [t["name"] for t in payload["tabs"]]   # floated tab saved as a normal tab


# --------------------------------------------------------------------------- #
# 9. detach gate is analysis-only (chat / DatasetSwitcher never detachable)     #
# --------------------------------------------------------------------------- #

def test_detach_gate_analysis_only(win, tmp_path):
    _add(win, "ds", "T", tmp_path)
    analysis_bar = win._groups["ds"].tabs.tabBar()
    assert analysis_bar._detachable is True
    # the DatasetSwitcher bar shares MultiRowTabBar but must not be detachable
    assert win._switcher._detachable is False


# --------------------------------------------------------------------------- #
# 10. placeholder is never floatable; set_active_tab on a float returns True    #
# --------------------------------------------------------------------------- #

def test_placeholder_not_floatable(win, tmp_path):
    from gui.tab import AnalysisTab
    _add(win, "ds", "real", tmp_path)                        # a real group exists
    ph = AnalysisTab("PH", is_placeholder=True)
    win.add_tab(ph)                                          # → None group (no session_spec)
    assert win.float_tab("PH", dataset=None) is False        # rejected at the single gate
    assert not getattr(win._groups.get(None), "_floated", {})
    # an absent tab is likewise not floatable
    assert win.float_tab("does-not-exist") is False


def test_set_active_tab_on_floated_returns_true(win, tmp_path):
    _add(win, "ds", "U", tmp_path)
    _add(win, "ds", "T", tmp_path)
    win.set_active_tab("U", dataset="ds")
    win.float_tab("T", dataset="ds")
    assert win.set_active_tab("T", dataset="ds") is True     # raises the float window
    assert win.active_tab().name == "U"                      # front group current unchanged


# --------------------------------------------------------------------------- #
# 11. FloatingTabWindow is an ownerless top-level (no always-on-top) — #56      #
# --------------------------------------------------------------------------- #

def test_floating_window_is_ownerless_toplevel(win, tmp_path):
    _add(win, "ds", "T", tmp_path)
    win.float_tab("T", dataset="ds")
    fw = win._float_windows[("ds", "T")]
    # No Qt/native owner: an owned top-level is forced above its owner on Windows
    # (de-facto always-on-top). Ownerless → the user can send it behind main. #56
    assert fw.parent() is None
    assert fw.isWindow()                          # independent top-level window
    assert win._float_windows[("ds", "T")] is fw  # registry strong ref = anti-GC


# --------------------------------------------------------------------------- #
# 12. floating an *inactive* tab keeps it visible (blank-window regression) #56  #
# --------------------------------------------------------------------------- #

def test_float_inactive_tab_is_visible(win, tmp_path):
    # QTabWidget explicitly hides non-current pages; that hidden flag survives
    # removeTab/setParent and win.show() won't re-show it, so an inactive tab
    # floated via the right-click menu came up blank. FloatingTabWindow now
    # re-shows the tab after re-parenting.
    _add(win, "ds", "a", tmp_path)
    _add(win, "ds", "b", tmp_path)
    win.set_active_tab("a", dataset="ds")          # a = active, b = inactive (hidden)
    grp = win._groups["ds"]
    _, docked_b = grp.find("b")
    assert docked_b.isHidden() is True             # precondition: non-current page hidden
    assert win.float_tab("b", dataset="ds") is True
    assert grp._floated["b"].isHidden() is False   # pre-fix: True (blank window)


# --------------------------------------------------------------------------- #
# 13. float leaves an inert position stub; re-dock restores the original slot   #
# --------------------------------------------------------------------------- #

def test_float_leaves_inert_position_stub(win, tmp_path):
    _add(win, "ds", "a", tmp_path)
    _add(win, "ds", "b", tmp_path)
    _add(win, "ds", "c", tmp_path)
    grp = win._groups["ds"]
    assert win.float_tab("b", dataset="ds") is True
    # A stub holds b's slot (index 1), but is NOT a real enumerated tab.
    assert _docked_names(grp) == ["a", "c"]          # b is not a real docked tab
    stub_idx = grp.tabs.indexOf(grp._float_stubs["b"])
    assert stub_idx == 1                             # stub holds b's original slot
    assert grp.tabs.isTabEnabled(stub_idx) is False  # stub is not selectable
    # label keeps the name but is visually marked distinct from a normal tab (#56)
    label = grp.tabs.tabText(stub_idx)
    assert "b" in label and label != "b"
    assert grp.tabs.tabBar().tabData(stub_idx) == "b"  # identity stays the bare name
    # single membership preserved despite the stub sharing b's name
    assert [w.name for w in win.tabs()].count("b") == 1
    assert win.tab_names().count("b") == 1


def test_redock_restores_original_position(win, tmp_path):
    _add(win, "ds", "a", tmp_path)
    _add(win, "ds", "b", tmp_path)
    _add(win, "ds", "c", tmp_path)
    grp = win._groups["ds"]
    win.float_tab("b", dataset="ds")                 # float the MIDDLE tab
    win._float_windows[("ds", "b")].close()          # [X] → re-dock
    assert _docked_names(grp) == ["a", "b", "c"]     # b back in its original slot, not appended
    assert not grp._float_stubs                       # stub cleaned up


def test_redock_position_robust_to_reorder(win, tmp_path):
    # The stub is a real bar tab, so it tracks position through other tabs
    # closing while floated — re-dock uses the stub's *current* slot.
    _add(win, "ds", "a", tmp_path)
    _add(win, "ds", "b", tmp_path)
    _add(win, "ds", "c", tmp_path)
    grp = win._groups["ds"]
    win.float_tab("a", dataset="ds")                 # float the FIRST tab (stub at 0)
    win.close_tab("b", dataset="ds")                 # close a middle real tab while floated
    win._float_windows[("ds", "a")].close()          # re-dock a
    assert _docked_names(grp) == ["a", "c"]          # a restored ahead of c (relative order kept)


def test_close_floated_tab_removes_stub(win, tmp_path):
    _add(win, "ds", "a", tmp_path)
    _add(win, "ds", "b", tmp_path)
    grp = win._groups["ds"]
    win.float_tab("b", dataset="ds")
    assert win.close_tab("b", dataset="ds") is True
    assert _bar_names(grp) == ["a"]                   # stub gone from the bar
    assert not grp._float_stubs
    assert win.find_tab("b", "ds") is None            # gone from enumeration too
