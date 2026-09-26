"""Nested panel split via the bridge (Issue #97) — Qt (offscreen)."""
import numpy as np
import pytest


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _window(qapp):
    from gui.window import ToolWindow
    from llm_bridge import _make_show_handler, _make_show_image_handler
    w = ToolWindow()
    w.register_command("show", _make_show_handler(w))
    w.register_command("show-image", _make_show_image_handler(w))
    return w


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """Hermetic dataset 'myds' → tmp_path; PC-local app state under tmp_path."""
    import config
    import dataset_config
    from llm_bridge import paths, session
    monkeypatch.setattr(config, "DATASETS", {"myds": {}})
    monkeypatch.setattr(config, "get_dataset_dir", lambda name: tmp_path)
    monkeypatch.setattr(config, "reload_datasets", lambda *a, **k: None)
    monkeypatch.setattr(
        dataset_config, "get_work_dir", lambda name, create=True: tmp_path
    )
    monkeypatch.setattr(session, "_touched", set())
    llm_state = tmp_path / "llm_state"

    def _fake_global_state_dir():
        llm_state.mkdir(parents=True, exist_ok=True)
        return llm_state

    monkeypatch.setattr(paths, "global_state_dir", _fake_global_state_dir)
    return tmp_path


@pytest.fixture()
def win(qapp, env):
    return _window(qapp)


@pytest.fixture()
def pngs(env, qapp):
    from PySide6.QtGui import QPixmap
    out = []
    for i in range(6):
        p = env / f"f{i}.png"
        QPixmap(10, 10).save(str(p))
        out.append(p)
    return out


@pytest.fixture()
def tifs(env):
    tifffile = pytest.importorskip("tifffile")
    a = np.zeros((3, 16, 16), dtype=np.uint16)
    a[0] = 1000
    p1 = env / "i3.tif"
    tifffile.imwrite(str(p1), a, imagej=True, metadata={"axes": "CYX"})
    p2 = env / "i2.tif"
    tifffile.imwrite(str(p2), np.zeros((2, 16, 16), dtype=np.uint16),
                     imagej=True, metadata={"axes": "CYX"})
    return p1, p2


@pytest.fixture()
def bad_tif(env, qapp):
    from gui.imageviewer import ImageViewerPanel
    p = env / "bad.tif"
    p.write_bytes(b"this is not a tiff at all")
    with pytest.raises(Exception):
        ImageViewerPanel().set_image(p)   # precondition: really unreadable
    return p


# ---- helpers ----

def _assert_tree_invariants(tab):
    from PySide6.QtWidgets import QSplitter
    from gui.tab import _leaf_widgets, _leaves, _nodes
    for node in _nodes(tab._splitter):
        assert node.count() == 2, "I1: every split has exactly 2 children"
    for leaf in _leaves(tab._splitter):
        assert not isinstance(leaf, QSplitter)
        n = sum(1 for w in _leaf_widgets(leaf) if tab._bridge_key_of(w) is not None)
        assert n <= 1, "I2: at most one bridge panel per leaf"


def _slots(tab):
    return {s: (k, p) for s, k, p in tab.pane_contents()}


def _name(p):
    from pathlib import Path
    return Path(p).name if p is not None else None


def _sig(tab):
    return (
        tab._all_slots(),
        [(s, k, _name(p)) for s, k, p in tab.pane_contents()],
        {k: id(w) for k, w in tab._panels.items()},
        {k: dict(v) for k, v in tab._bridge_panes.items()},
        [tab._is_shown(w) for w in tab._panels.values()],
    )


def _grid(win, pngs, order=("top/left", "top/right", "bottom/left", "bottom/right"),
          name="q", dataset=None):
    kw = {"dataset": dataset} if dataset else {}
    for i, s in enumerate(order):
        win.dispatch_command("show", path=str(pngs[i]), name=name, slot=s, **kw)
    return win.active_tab()


# ---- basic placement ----

def test_new_tab_single_show(win, pngs):
    win.dispatch_command("show", path=str(pngs[0]), name="q")
    tab = win.active_tab()
    _assert_tree_invariants(tab)
    assert not tab._left_container.isHidden()
    assert len(tab.pane_contents()) == 1
    fig = tab.panel("figure")
    assert tab.grab_full().cacheKey() == fig._pixmap.cacheKey()


@pytest.mark.parametrize("order", [
    ("top/left", "top/right", "bottom/left", "bottom/right"),
    ("bottom/right", "top/left", "bottom/left", "top/right"),
    ("top/right", "bottom/right", "top/left", "bottom/left"),
])
def test_grid_2x2_any_order(win, pngs, order):
    from PySide6.QtCore import Qt
    tab = _grid(win, pngs, order)
    _assert_tree_invariants(tab)
    got = _slots(tab)
    assert set(got) == {"top/left", "top/right", "bottom/left", "bottom/right"}
    for i, s in enumerate(order):
        assert got[s] == ("figure", str(pngs[i].resolve()))
    assert tab._splitter.orientation() == Qt.Orientation.Vertical
    for s in ("top", "bottom"):
        assert tab.node_at(s).orientation() == Qt.Orientation.Horizontal
    for s, k, w in tab.bridge_panels():
        assert tab._is_shown(w)
        assert not w._pixmap.isNull()


def test_same_slot_reshow_in_place(win, pngs):
    tab = _grid(win, pngs)
    w = tab.node_at("bottom/left")
    key, panel = tab.bridge_panel_in(w)
    tab.set_split_ratio(3, 1)
    tab.set_split_ratio(1, 4, slot="bottom")
    before = tab.capture_layout()
    n = len(tab._panels)
    r = win.dispatch_command("show", path=str(pngs[5]), name="q", slot="bottom/left")
    assert r == "updated:q"
    assert tab.bridge_panel_in(tab.node_at("bottom/left")) == (key, panel)
    assert len(tab._panels) == n
    assert tab.capture_layout() == before
    assert _slots(tab)["bottom/left"][1] == str(pngs[5].resolve())
    _assert_tree_invariants(tab)


def test_depth3_side_by_side(win, pngs):
    from PySide6.QtCore import Qt
    win.dispatch_command("show", path=str(pngs[0]), name="q", slot="left/top/left")
    win.dispatch_command("show", path=str(pngs[1]), name="q", slot="left/top/right")
    tab = win.active_tab()
    _assert_tree_invariants(tab)
    node = tab.node_at("left/top")
    assert node.orientation() == Qt.Orientation.Horizontal
    assert set(_slots(tab)) == {"left/top/left", "left/top/right"}
    assert all(tab._is_shown(w) for _s, _k, w in tab.bridge_panels())


def test_split_occupied_pane_pushes_content(win, pngs):
    win.dispatch_command("show", path=str(pngs[0]), name="q")
    win.dispatch_command("show", path=str(pngs[1]), name="q", slot="right")
    tab = win.active_tab()
    q_panel = tab.bridge_panel_in(tab.node_at("right"))[1]
    root_before = tab._splitter.sizes()
    win.dispatch_command("show", path=str(pngs[2]), name="q", slot="right/bottom")
    _assert_tree_invariants(tab)
    got = _slots(tab)
    assert got["left"][1] == str(pngs[0].resolve())
    assert got["right/top"][1] == str(pngs[1].resolve())
    assert got["right/bottom"][1] == str(pngs[2].resolve())
    assert tab.bridge_panel_in(tab.node_at("right/top"))[1] is q_panel
    assert tab._splitter.sizes() == root_before


def test_region_collapse(win, pngs):
    tab = _grid(win, pngs)
    win.dispatch_command("show", path=str(pngs[4]), name="q", slot="top")
    _assert_tree_invariants(tab)
    got = _slots(tab)
    assert set(got) == {"top", "bottom/left", "bottom/right"}
    assert got["top"][1] == str(pngs[4].resolve())


def test_slot_omitted_collapses_to_single(win, pngs):
    tab = _grid(win, pngs)
    win.dispatch_command("show", path=str(pngs[4]), name="q")
    _assert_tree_invariants(tab)
    assert len(tab.pane_contents()) == 1
    assert tab.pane_contents()[0][2] == str(pngs[4].resolve())
    assert len(tab._panels) == 1                      # C4: bridge panes destroyed
    assert tab._right_container.isHidden()


# ---- mixed figures / images ----

def test_mixed_grid_and_image_verbs(win, pngs, tifs):
    t3, t2 = tifs
    win.dispatch_command("show", path=str(pngs[0]), name="q", slot="top/left")
    win.dispatch_command("show-image", path=str(t3), name="q", slot="top/right")
    win.dispatch_command("show-image", path=str(t2), name="q", slot="bottom/left")
    win.dispatch_command("show", path=str(pngs[1]), name="q", slot="bottom/right")
    tab = win.active_tab()
    _assert_tree_invariants(tab)
    kinds = {s: k for s, k, _p in tab.pane_contents()}
    assert kinds == {"top/left": "figure", "top/right": "image",
                     "bottom/left": "image", "bottom/right": "figure"}
    tr = tab.bridge_panel_in(tab.node_at("top/right"))[1]
    bl = tab.bridge_panel_in(tab.node_at("bottom/left"))[1]
    tab.dispatch_command("set-channel", index=1, slot="bottom/left")
    assert bl.active_channel == 1 and tr.active_channel == 0
    tab.dispatch_command("set-channel", index=2)      # default = first image (tree order)
    assert tr.active_channel == 2 and bl.active_channel == 1
    with pytest.raises(ValueError):
        tab.dispatch_command("set-channel", index=0, slot="bottom/right")  # figure pane
    with pytest.raises(ValueError):
        tab.dispatch_command("set-channel", index=0, slot="left/top")      # wrong axis


def test_viewer_host_accepts_both_kinds(win, pngs, tifs):
    """C2: a figure host takes show-image, an image host takes show."""
    win.dispatch_command("show", path=str(pngs[0]), name="a")
    win.dispatch_command("show-image", path=str(tifs[0]), name="a")
    tab = win.active_tab()
    assert [k for _s, k, _p in tab.pane_contents()] == ["image"]   # no image pane → single
    win.dispatch_command("show-image", path=str(tifs[1]), name="b")
    win.dispatch_command("show", path=str(pngs[1]), name="b", slot="right")
    tab = win.active_tab()
    assert [(s, k) for s, k, _p in tab.pane_contents()] == [("left", "image"), ("right", "figure")]
    # slot-less show-image updates the image pane in place, keeping the split.
    v = tab.panel("viewer")
    win.dispatch_command("show-image", path=str(tifs[0]), name="b")
    assert tab.panel("viewer") is v and v.nC == 3
    assert len(tab.pane_contents()) == 2


def test_broken_image_leaves_tab_unchanged(win, pngs, tifs, bad_tif):
    win.dispatch_command("show", path=str(pngs[0]), name="q", slot="left")
    win.dispatch_command("show-image", path=str(tifs[0]), name="q", slot="right")
    tab = win.active_tab()
    before = _sig(tab)
    for kw in ({"slot": "right"}, {"slot": "left/bottom"}, {}):
        with pytest.raises(Exception):
            win.dispatch_command("show-image", path=str(bad_tif), name="q", **kw)
        assert _sig(tab) == before
    n = len(win.tab_names())
    with pytest.raises(Exception):
        win.dispatch_command("show-image", path=str(bad_tif), name="new", slot="top")
    with pytest.raises(Exception):
        win.dispatch_command("show-image", path=str(bad_tif), name="new2")
    assert len(win.tab_names()) == n


def test_c1_show_image_slot_right_new_tab(win, tifs):
    win.dispatch_command("show-image", path=str(tifs[0]), name="q", slot="right")
    tab = win.active_tab()
    assert tab._right_container.isAncestorOf(tab.panel("viewer-2"))
    assert tab._left_container.isHidden()
    assert "viewer" not in tab._panels
    _assert_tree_invariants(tab)


def test_c3_hand_built_tab_in_place_only(win, pngs, qapp):
    from PySide6.QtWidgets import QLabel
    from gui.panels import FigurePanel
    from gui.tab import AnalysisTab
    tab = AnalysisTab("hand")
    fig = FigurePanel()
    tab.add_panel("figure", fig, "left")
    tab.add_panel("side", QLabel("x"), "right")
    spec = {"kind": "analysis", "name": "hand", "module": "hand", "dataset": "myds"}
    tab.session_spec = dict(spec)
    win.add_tab(tab)
    assert win.dispatch_command("show", path=str(pngs[0]), name="hand") == "updated:hand"
    assert not fig._pixmap.isNull()
    assert tab.session_spec == spec
    assert not tab._right_container.isHidden()
    with pytest.raises(LookupError):
        win.dispatch_command("show", path=str(pngs[0]), name="hand", slot="right")


# ---- close-pane / set-split / list-panes ----

def test_close_pane(win, pngs):
    tab = _grid(win, pngs)
    r = tab.dispatch_command("close-pane", slot="bottom/right")
    assert r == "closed:bottom/right"
    _assert_tree_invariants(tab)
    assert set(_slots(tab)) == {"top/left", "top/right", "bottom/left"}
    assert tab.node_at("bottom/right").isHidden()
    tab.dispatch_command("close-pane", slot="top")
    assert set(_slots(tab)) == {"bottom/left"}
    assert tab._left_container.isHidden()
    with pytest.raises(ValueError, match="close-tab"):
        tab.dispatch_command("close-pane", slot="bottom/left")
    with pytest.raises(ValueError, match="empty"):
        tab.dispatch_command("close-pane", slot="bottom/right")
    assert set(_slots(tab)) == {"bottom/left"}


def test_close_pane_strict_orientation(win, pngs):
    tab = _grid(win, pngs)
    before = _sig(tab)
    with pytest.raises(ValueError, match="current slots"):
        tab.dispatch_command("close-pane", slot="left/top")
    assert _sig(tab) == before


def test_set_split_variants(win, pngs):
    tab = _grid(win, pngs)
    tab.dispatch_command("set-split", top=1, bottom=3)
    assert tab._splitter.sizes()[0] * 3 == pytest.approx(tab._splitter.sizes()[1], abs=3)
    tab.dispatch_command("set-split", left=1, right=4, slot="top")
    s = tab.node_at("top").sizes()
    assert s[0] < s[1]
    for bad in ({"left": 1}, {"left": 1, "top": 1, "right": 1},
                {"left": 1, "right": 1, "bottom": 1}):
        with pytest.raises(ValueError):
            tab.dispatch_command("set-split", **bad)
    with pytest.raises(ValueError):
        tab.dispatch_command("set-split", left=1, right=1, slot="top/left")   # leaf
    with pytest.raises(ValueError):
        tab.dispatch_command("set-split", left=1, right=1, slot="left")       # axis


def test_nested_splitter_moved_marks_dirty(win, pngs):
    tab = _grid(win, pngs, dataset="myds")
    win.clear_session_dirty()
    assert not win.is_session_dirty()
    tab.node_at("bottom").splitterMoved.emit(10, 1)
    assert win.is_session_dirty()


def test_list_panes(win, pngs, tifs):
    win.dispatch_command("show", path=str(pngs[0]), name="q", slot="left")
    win.dispatch_command("show-image", path=str(tifs[0]), name="q", slot="right")
    tab = win.active_tab()
    got = tab.dispatch_command("list-panes")
    assert got == [
        {"slot": "left", "key": "figure", "kind": "figure", "path": str(pngs[0].resolve())},
        {"slot": "right", "key": "viewer-2", "kind": "image", "path": str(tifs[0].resolve())},
    ]


# ---- ownership (I3) ----

def test_analysis_panels_block_structure_ops(qapp):
    from PySide6.QtWidgets import QLabel
    from gui.panels import FigurePanel
    from gui.tab import AnalysisTab
    tab = AnalysisTab("an")
    fig = FigurePanel()
    lab = QLabel("x")
    tab.add_panel("fig", fig, "left")
    tab.add_panel("lab", lab, "right")
    before = _sig(tab)
    for op in (lambda: tab.ensure_pane("left/top"),
               lambda: tab.ensure_pane("right"),
               lambda: tab.ensure_pane("top"),          # re-orient root
               lambda: tab.close_pane("left"),
               lambda: tab.clear_panes(),
               lambda: tab.check_bridge_only()):
        with pytest.raises(LookupError):
            op()
        assert _sig(tab) == before
    assert tab.panel("fig") is fig and tab.panel("lab") is lab


def test_ownership_precheck_leaves_everything_unchanged(win, pngs, tifs):
    from PySide6.QtWidgets import QLabel
    # (a) in-place candidate leaf also holds a non-bridge panel
    win.dispatch_command("show", path=str(pngs[0]), name="a")
    tab = win.active_tab()
    tab.add_panel("extra", QLabel("x"), "left")
    fig = tab.panel("figure")
    key0 = fig._pixmap.cacheKey()
    before = _sig(tab)
    with pytest.raises(LookupError):
        win.dispatch_command("show", path=str(pngs[1]), name="a", slot="left")
    assert _sig(tab) == before
    assert fig._path == pngs[0].resolve() and fig._pixmap.cacheKey() == key0

    # (b) opposite leaf holds a non-bridge panel; slot-less show
    win.dispatch_command("show", path=str(pngs[0]), name="b")
    tab = win.active_tab()
    tab.add_panel("extra", QLabel("x"), "right")
    fig = tab.panel("figure")
    key0 = fig._pixmap.cacheKey()
    before = _sig(tab)
    with pytest.raises(LookupError):
        win.dispatch_command("show", path=str(pngs[1]), name="b")
    assert _sig(tab) == before
    assert fig._path == pngs[0].resolve() and fig._pixmap.cacheKey() == key0

    # (c) bridge image leaf also holds a non-bridge panel; slot-less show-image
    win.dispatch_command("show-image", path=str(tifs[0]), name="c")
    tab = win.active_tab()
    tab.add_panel("extra", QLabel("x"), "left")
    v = tab.panel("viewer")
    before = _sig(tab)
    with pytest.raises(LookupError):
        win.dispatch_command("show-image", path=str(tifs[1]), name="c")
    assert _sig(tab) == before
    assert v.nC == 3


# ---- grab_full ----

def test_grab_full_2x2(win, pngs):
    tab = _grid(win, pngs)
    full = tab.grab_full()
    assert (full.width(), full.height()) == (28, 28)
    tab.dispatch_command("close-pane", slot="bottom")
    full = tab.grab_full()
    assert (full.width(), full.height()) == (28, 10)


# ---- session (Qt) ----

def _save_and_entry(win):
    from llm_bridge import session
    saved, failed = session.save_all(win)
    assert "myds" in saved and not failed
    return session.read_session("myds")["tabs"]


def _restore_new(qapp, name):
    from llm_bridge import session
    w2 = _window(qapp)
    assert session.open_dataset(w2, "myds").startswith("restored:")
    return w2, next(t for t in w2.tabs() if t.name == name)


def _write(entry):
    from llm_bridge import session
    session.write_session("myds", {"version": 1, "dataset": "myds",
                                   "active_tab": None, "tabs": [entry]})


def test_session_grid_mixed_roundtrip(qapp, win, pngs, tifs):
    from PySide6.QtCore import Qt
    win.dispatch_command("show", path=str(pngs[0]), name="q", slot="top/left",
                         dataset="myds")
    win.dispatch_command("show-image", path=str(tifs[0]), name="q", slot="top/right")
    win.dispatch_command("show", path=str(pngs[1]), name="q", slot="bottom/left")
    win.dispatch_command("show", path=str(pngs[2]), name="q", slot="bottom/right")
    tab = win.active_tab()
    tab.set_split_ratio(3, 1, slot="top")
    tabs = _save_and_entry(win)
    entry = tabs[0]
    assert [p["slot"] for p in entry["panes"]] == [
        "top/left", "top/right", "bottom/left", "bottom/right"]
    assert entry["panes"][1] == {"slot": "top/right", "kind": "image", "path": "i3.tif"}
    assert "top" in entry["layout"]["splits"]
    w2, t = _restore_new(qapp, "q")
    _assert_tree_invariants(t)
    assert [(s, k, _name(p)) for s, k, p in t.pane_contents()] == [
        (s, k, _name(p)) for s, k, p in tab.pane_contents()]
    assert t._splitter.orientation() == Qt.Orientation.Vertical
    assert t.node_at("top").orientation() == Qt.Orientation.Horizontal


def test_session_flat_two_uses_legacy_fields(win, pngs):
    win.dispatch_command("show", path=str(pngs[0]), name="q", dataset="myds")
    win.dispatch_command("show", path=str(pngs[1]), name="q", slot="right")
    entry = _save_and_entry(win)[0]
    assert "panes" not in entry
    assert entry["figure"] == "f0.png" and entry["figure2"] == "f1.png"


def test_session_depth3_exact(qapp, win, pngs):
    for i, s in enumerate(("left/top/left", "left/top/right", "left/bottom", "right")):
        win.dispatch_command("show", path=str(pngs[i]), name="q", slot=s, dataset="myds")
    tab = win.active_tab()
    _save_and_entry(win)
    w2, t = _restore_new(qapp, "q")
    assert t._all_slots() == tab._all_slots()
    assert [(s, _name(p)) for s, _k, p in t.pane_contents()] == [
        (s, _name(p)) for s, _k, p in tab.pane_contents()]


def test_session_missing_pane_no_ghost(qapp, win, pngs):
    _write({"name": "q", "kind": "figure", "figure": "f0.png", "panes": [
        {"slot": "top/left", "kind": "figure", "path": "f0.png"},
        {"slot": "top/right", "kind": "figure", "path": "gone.png"},
        {"slot": "bottom", "kind": "figure", "path": "f1.png"},
    ], "layout": {"orientation": "vertical", "sizes": [1, 1]}})
    w2, t = _restore_new(qapp, "q")
    _assert_tree_invariants(t)
    assert set(_slots(t)) == {"top/left", "bottom"}
    assert t.node_at("top/right").isHidden()
    assert t._is_shown(t.node_at("top/left"))


def test_session_restore_into_existing_host_converges(qapp, win, pngs, tifs):
    from llm_bridge import session
    _grid(win, pngs, dataset="myds")
    _write({"name": "q", "kind": "figure", "figure": "f4.png", "panes": [
        {"slot": "left", "kind": "figure", "path": "f4.png"},
        {"slot": "right", "kind": "image", "path": "i2.tif"},
    ], "layout": {"orientation": "horizontal", "sizes": [1, 1]}})
    session.open_dataset(win, "myds")
    t = next(t for t in win.tabs() if t.name == "q")
    _assert_tree_invariants(t)
    assert [(s, k, _name(p)) for s, k, p in t.pane_contents()] == [
        ("left", "figure", "f4.png"), ("right", "image", "i2.tif")]


def test_session_same_name_analysis_tab_untouched(qapp, win, pngs):
    from PySide6.QtWidgets import QLabel
    from gui.tab import AnalysisTab
    from llm_bridge import session
    tab = AnalysisTab("q")
    lab = QLabel("x")
    tab.add_panel("lab", lab, "left")
    tab.session_spec = {"kind": "analysis", "name": "q", "module": "q", "dataset": "myds"}
    win.add_tab(tab)
    _write({"name": "q", "kind": "figure", "figure": "f0.png", "panes": [
        {"slot": "left", "kind": "figure", "path": "f0.png"}]})
    session.open_dataset(win, "myds")
    assert list(tab._panels) == ["lab"] and tab.panel("lab") is lab


def test_session_no_visible_panes_not_saved(win, pngs):
    from llm_bridge import session
    win.dispatch_command("show", path=str(pngs[0]), name="q", dataset="myds")
    win.active_tab().clear_panes()
    saved, failed = session.save_all(win)
    assert not failed
    assert session.read_session("myds")["tabs"] == []


def test_restore_failure_contract(win, pngs, tifs, bad_tif):
    from llm_bridge import session
    _grid(win, pngs, dataset="myds")
    tab = next(t for t in win.tabs() if t.name == "q")
    before = _sig(tab)
    _write({"name": "q", "kind": "image", "image": "bad.tif", "panes": [
        {"slot": "left", "kind": "image", "path": "bad.tif"},
        {"slot": "right", "kind": "image", "path": "bad.tif"},
    ]})
    assert session.open_dataset(win, "myds") == "restored:0"
    assert _sig(tab) == before

    _write({"name": "q", "kind": "image", "image": "i3.tif", "panes": [
        {"slot": "top/left", "kind": "image", "path": "i3.tif"},
        {"slot": "bottom/right", "kind": "image", "path": "bad.tif"},
    ]})
    assert session.open_dataset(win, "myds") == "restored:1"
    _assert_tree_invariants(tab)
    assert [(s, k, _name(p)) for s, k, p in tab.pane_contents()] == [
        ("top/left", "image", "i3.tif")]
    assert tab.node_at("bottom").isHidden()        # emptied region collapsed + hidden


# ---- legacy entries into existing tabs ----

_H = {"orientation": "horizontal", "sizes": [1, 1],
      "left_hidden": False, "right_hidden": False}


def _open(win):
    from llm_bridge import session
    session.open_dataset(win, "myds")
    return next(t for t in win.tabs() if t.name == "v")


def test_legacy_a_c1_tab_two_images(win, tifs):
    win.dispatch_command("show-image", path=str(tifs[0]), name="v", slot="right",
                         dataset="myds")
    _write({"name": "v", "kind": "image", "image": "i3.tif", "image2": "i2.tif",
            "layout": _H})
    t = _open(win)
    _assert_tree_invariants(t)
    assert [(s, _name(p)) for s, _k, p in t.pane_contents()] == [
        ("left", "i3.tif"), ("right", "i2.tif")]


def test_legacy_b_single_after_close(win, tifs):
    win.dispatch_command("show-image", path=str(tifs[0]), name="v", dataset="myds")
    win.dispatch_command("show-image", path=str(tifs[1]), name="v", slot="right")
    tab = win.active_tab()
    tab.dispatch_command("close-pane", slot="left")
    _write({"name": "v", "kind": "image", "image": "i3.tif",
            "layout": {**_H, "right_hidden": True}})
    t = _open(win)
    _assert_tree_invariants(t)
    assert [(s, _name(p)) for s, _k, p in t.pane_contents()] == [("left", "i3.tif")]


@pytest.mark.parametrize("slot", ["bottom", "top"])
def test_legacy_c_vertical_single_image_uses_panes(qapp, win, tifs, slot):
    win.dispatch_command("show-image", path=str(tifs[0]), name="v", slot=slot,
                         dataset="myds")
    entry = _save_and_entry(win)[0]
    assert entry["panes"] == [{"slot": slot, "kind": "image", "path": "i3.tif"}]
    w2, t = _restore_new(qapp, "v")
    assert [s for s, _k, _p in t.pane_contents()] == [slot]
    t = _open(win)
    assert [s for s, _k, _p in t.pane_contents()] == [slot]


def test_legacy_d_image2_missing_left_hidden(qapp, win, tifs):
    entry = {"name": "v", "kind": "image", "image": "i3.tif", "image2": "gone.tif",
             "layout": {**_H, "left_hidden": True}}
    _write(entry)
    w2, t = _restore_new(qapp, "v")
    assert [s for s, _k, _p in t.pane_contents()] == ["right"]
    win.dispatch_command("show-image", path=str(tifs[1]), name="v", dataset="myds")
    _write(entry)
    t = _open(win)
    assert [s for s, _k, _p in t.pane_contents()] == ["right"]


def test_legacy_e_reuse_keeps_identity_and_lut(win, tifs):
    win.dispatch_command("show-image", path=str(tifs[0]), name="v", dataset="myds")
    tab = win.active_tab()
    v = tab.panel("viewer")
    tab.dispatch_command("set-lut", lut="Fire", channel=0)
    _write({"name": "v", "kind": "image", "image": "i3.tif",
            "layout": {**_H, "left_hidden": True}})
    t = _open(win)
    assert t.panel("viewer") is v
    assert t._right_container.isAncestorOf(v)
    assert v.channels[0]["lut_name"] == "Fire"
    assert t._left_container.isHidden()


def test_legacy_f_swapped_keys_converge(win, tifs):
    win.dispatch_command("show-image", path=str(tifs[0]), name="v", panel="right",
                         dataset="myds")
    win.dispatch_command("show-image", path=str(tifs[1]), name="v", slot="left")
    tab = win.active_tab()
    assert tab._left_container.isAncestorOf(tab.panel("viewer-2"))
    _write({"name": "v", "kind": "image", "image": "i3.tif", "image2": "i2.tif",
            "layout": _H})
    t = _open(win)
    _assert_tree_invariants(t)
    assert t._left_container.isAncestorOf(t.panel("viewer"))
    assert t._right_container.isAncestorOf(t.panel("viewer-2"))
    assert _name(t._bridge_panes["viewer"]["path"]) == "i3.tif"
    assert _name(t._bridge_panes["viewer-2"]["path"]) == "i2.tif"


def test_legacy_g_no_layout_single_figure_collapses(win, pngs):
    win.dispatch_command("show", path=str(pngs[0]), name="v", dataset="myds")
    win.dispatch_command("show", path=str(pngs[1]), name="v", slot="right")
    _write({"name": "v", "kind": "figure", "figure": "f2.png"})
    t = _open(win)
    assert [(s, _name(p)) for s, _k, p in t.pane_contents()] == [("left", "f2.png")]
    assert t._right_container.isHidden()


def test_legacy_h_claimed_prevents_double_use_image(win, tifs):
    win.dispatch_command("show-image", path=str(tifs[0]), name="v", panel="right",
                         dataset="myds")
    win.dispatch_command("show-image", path=str(tifs[1]), name="v", slot="left")
    tab = win.active_tab()
    tab.dispatch_command("close-pane", slot="right")
    assert list(tab._bridge_panes) == ["viewer-2"]
    _write({"name": "v", "kind": "image", "image": "i3.tif", "image2": "i2.tif",
            "layout": _H})
    t = _open(win)
    _assert_tree_invariants(t)
    left = t.bridge_panel_in(t.root_pane(0))
    right = t.bridge_panel_in(t.root_pane(1))
    assert left and right and left[1] is not right[1]
    assert _name(t._bridge_panes[left[0]]["path"]) == "i3.tif"
    assert _name(t._bridge_panes[right[0]]["path"]) == "i2.tif"
    assert _name(left[1]._path) == "i3.tif" and _name(right[1]._path) == "i2.tif"


def test_legacy_h_claimed_prevents_double_use_figure(win, pngs):
    from gui.panels import FigurePanel
    win.dispatch_command("show", path=str(pngs[0]), name="v", dataset="myds")
    tab = win.active_tab()
    tab.remove_panel("figure")
    f2 = FigurePanel()
    f2.set_path(pngs[3])
    tab.add_bridge_panel("figure-2", f2, tab.root_pane(0), "figure", str(pngs[3]))
    tab.set_pane_visible("left", True)
    _write({"name": "v", "kind": "figure", "figure": "f0.png", "figure2": "f1.png",
            "layout": _H})
    t = _open(win)
    _assert_tree_invariants(t)
    left = t.bridge_panel_in(t.root_pane(0))
    right = t.bridge_panel_in(t.root_pane(1))
    assert left and right and left[1] is not right[1]
    assert _name(t._bridge_panes[left[0]]["path"]) == "f0.png"
    assert _name(t._bridge_panes[right[0]]["path"]) == "f1.png"
    assert _name(left[1]._path) == "f0.png" and _name(right[1]._path) == "f1.png"


def _flush_deletes(qapp):
    from PySide6.QtCore import QCoreApplication, QEvent
    qapp.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_legacy_i_nested_to_ancestor_image(qapp, win, tifs):
    import shiboken6
    win.dispatch_command("show-image", path=str(tifs[0]), name="v", dataset="myds")
    win.dispatch_command("show-image", path=str(tifs[1]), name="v", slot="left/right")
    tab = win.active_tab()
    v = tab.panel("viewer")
    assert tab.slot_of(v) == "left/left"
    _write({"name": "v", "kind": "image", "image": "i2.tif", "layout": _H})
    t = _open(win)
    assert t.panel("viewer") is v
    assert t.slot_of(v) == "left"
    assert _name(t._bridge_panes["viewer"]["path"]) == "i2.tif"
    assert v.nC == 2
    assert "viewer-2" not in t._panels
    _flush_deletes(qapp)
    assert shiboken6.isValid(t.panel("viewer"))
    _assert_tree_invariants(t)


def test_legacy_i_nested_to_ancestor_figure(qapp, win, pngs):
    import shiboken6
    win.dispatch_command("show", path=str(pngs[0]), name="v", dataset="myds")
    win.dispatch_command("show", path=str(pngs[1]), name="v", slot="left/right")
    tab = win.active_tab()
    f = tab.panel("figure")
    assert tab.slot_of(f) == "left/left"
    _write({"name": "v", "kind": "figure", "figure": "f2.png",
            "layout": {**_H, "right_hidden": True}})
    t = _open(win)
    assert t.panel("figure") is f
    assert t.slot_of(f) == "left"
    assert _name(t._bridge_panes["figure"]["path"]) == "f2.png"
    assert len(t._bridge_panes) == 1
    _flush_deletes(qapp)
    assert shiboken6.isValid(t.panel("figure"))
    _assert_tree_invariants(t)


def test_legacy_j_orientation_change_reuses(win, pngs):
    from PySide6.QtCore import Qt
    win.dispatch_command("show", path=str(pngs[0]), name="v", dataset="myds")
    win.dispatch_command("show", path=str(pngs[1]), name="v", slot="right")
    tab = win.active_tab()
    f1, f2 = tab.panel("figure"), tab.panel("figure-2")
    _write({"name": "v", "kind": "figure", "figure": "f0.png", "figure2": "f1.png",
            "layout": {**_H, "orientation": "vertical"}})
    t = _open(win)
    assert t._splitter.orientation() == Qt.Orientation.Vertical
    assert t.bridge_panel_in(t.node_at("top"))[1] is f1
    assert t.bridge_panel_in(t.node_at("bottom"))[1] is f2
    _assert_tree_invariants(t)
