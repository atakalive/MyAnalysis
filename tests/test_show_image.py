"""Tests for the show-image window verb + image-kind session persistence (Issue #60)."""
import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def win(qapp):
    from gui.window import ToolWindow
    from llm_bridge import _make_show_image_handler
    w = ToolWindow()
    w.register_command("show-image", _make_show_image_handler(w))
    return w


@pytest.fixture()
def tif_path(tmp_path):
    arr = np.zeros((3, 32, 40), dtype=np.uint16)  # multi-channel CYX
    arr[0] = 1000
    p = tmp_path / "img.tif"
    tifffile.imwrite(str(p), arr, imagej=True, metadata={"axes": "CYX"})
    return p


@pytest.fixture()
def tif_path2(tmp_path):
    arr = np.zeros((2, 16, 16), dtype=np.uint16)
    p = tmp_path / "img2.tif"
    tifffile.imwrite(str(p), arr, imagej=True, metadata={"axes": "CYX"})
    return p


def test_show_image_new_tab(win, tif_path):
    from gui.imageviewer import ImageViewerPanel
    result = win.dispatch_command("show-image", path=str(tif_path))
    assert result == "shown:viewer"
    assert "viewer" in win.tab_names()
    assert isinstance(win.active_tab().panel("viewer"), ImageViewerPanel)


def test_show_image_update_same_tab(win, tif_path, tif_path2):
    win.dispatch_command("show-image", path=str(tif_path))
    result = win.dispatch_command("show-image", path=str(tif_path2), name="viewer")
    assert result == "updated:viewer"
    assert win.tab_names().count("viewer") == 1
    assert win.active_tab().panel("viewer").nC == 2


def test_show_image_invalid_panel(win, tif_path):
    with pytest.raises(ValueError, match="panel must be"):
        win.dispatch_command("show-image", path=str(tif_path), panel="center")


def test_show_image_nonexistent(win, tmp_path):
    with pytest.raises(LookupError):
        win.dispatch_command("show-image", path=str(tmp_path / "missing.tif"))
    assert win.tab_names() == []


def test_show_image_name_collision_non_viewer(win, tif_path, qapp):
    from gui.tab import AnalysisTab
    existing = AnalysisTab("conflict")
    win.add_tab(existing)
    with pytest.raises(LookupError, match="not an image-viewer tab"):
        win.dispatch_command("show-image", path=str(tif_path), name="conflict")
    assert "conflict" in win.tab_names()
    assert len(win.tab_names()) == 1


def test_show_image_tab_verbs(win, tif_path):
    win.dispatch_command("show-image", path=str(tif_path))
    tab = win.active_tab()
    tab.dispatch_command("set-lut", lut="Fire", channel=0)
    assert tab.panel("viewer").channels[0]["lut_name"] == "Fire"
    tab.dispatch_command("set-mode", mode="single")
    assert tab.panel("viewer").mode == "single"
    tab.dispatch_command("set-channel", index=1)
    assert tab.panel("viewer").active_channel == 1
    tab.dispatch_command("set-range", min=10, max=500, channel=0)
    assert tab.panel("viewer").channels[0]["levels"] == (10.0, 500.0)


def test_show_image_bool_coerce(win, tif_path):
    """CLI delivers invert=false as string 'false' -> must coerce to False."""
    win.dispatch_command("show-image", path=str(tif_path))
    tab = win.active_tab()
    tab.dispatch_command("set-lut", lut="Red", channel=0, invert="false")
    assert tab.panel("viewer").channels[0]["invert"] is False
    tab.dispatch_command("set-visible", channel=1, visible="false")
    assert tab.panel("viewer").channels[1]["visible"] is False


def test_show_image_update_marks_dirty(win, tif_path, tif_path2, tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "DATASETS", {"myds": {}})
    monkeypatch.setattr(config, "get_dataset_dir", lambda name: tmp_path)
    win.set_session_saver(lambda: ([], []))

    win.dispatch_command("show-image", path=str(tif_path), name="v", dataset="myds")
    win.clear_session_dirty()
    assert not win.is_session_dirty()

    result = win.dispatch_command("show-image", path=str(tif_path2), name="v", dataset="myds")
    assert result == "updated:v"
    assert win.is_session_dirty()
    assert win.active_tab().session_spec["image"] == str(tif_path2.resolve())
    assert win.active_tab().session_spec["dataset"] == "myds"


def test_load_image_volatile(win, tif_path, tif_path2, tmp_path, monkeypatch):
    """tab verb load-image swaps the displayed image but does NOT touch session_spec."""
    import config
    monkeypatch.setattr(config, "DATASETS", {"myds": {}})
    monkeypatch.setattr(config, "get_dataset_dir", lambda name: tmp_path)
    win.set_session_saver(lambda: ([], []))

    win.dispatch_command("show-image", path=str(tif_path), name="v", dataset="myds")
    tab = win.active_tab()
    spec_before = dict(tab.session_spec)
    tab.dispatch_command("load-image", path=str(tif_path2))
    assert tab.panel("viewer").nC == 2               # image swapped
    assert tab.session_spec == spec_before           # session_spec unchanged
    # a following show-image updates the spec to the new image
    win.dispatch_command("show-image", path=str(tif_path2), name="v", dataset="myds")
    assert tab.session_spec["image"] == str(tif_path2.resolve())


def test_show_image_session_roundtrip(qapp, tif_path, tmp_path, monkeypatch):
    import config
    import dataset_config
    from llm_bridge import session, _make_show_image_handler
    from gui.window import ToolWindow
    from gui.imageviewer import ImageViewerPanel

    monkeypatch.setattr(config, "DATASETS", {"myds": {}})
    monkeypatch.setattr(config, "get_dataset_dir", lambda name: tmp_path)
    monkeypatch.setattr(config, "reload_datasets", lambda: None)
    monkeypatch.setattr(dataset_config, "get_work_dir", lambda name, create=True: tmp_path)
    # Isolate the module-level save-target set from other tests that call
    # note_dataset (else a foreign dataset shares this test's patched work_dir
    # and overwrites myds's session.json).
    monkeypatch.setattr(session, "_touched", set())

    w1 = ToolWindow()
    w1.register_command("show-image", _make_show_image_handler(w1))
    w1.dispatch_command("show-image", path=str(tif_path), name="v", dataset="myds")
    saved, failed = session.save_all(w1)
    assert "myds" in saved and not failed

    w2 = ToolWindow()
    w2.register_command("show-image", _make_show_image_handler(w2))
    result = session.open_dataset(w2, "myds")
    assert result.startswith("restored:")
    t = next((t for t in w2.tabs() if t.name == "v"), None)
    assert t is not None
    panel = t.panel("viewer")
    assert isinstance(panel, ImageViewerPanel)
    # The restored panel must have re-loaded the real image, not an empty shell.
    assert panel._arr is not None
    assert panel.nC == 3           # matches the (3, 32, 40) CYX source tif
    assert (panel.nY, panel.nX) == (32, 40)


# ---- 生画像 2枚分割（Part B: viewer-2 / slot / image2 round-trip）----

def test_show_image_split_adds_viewer2(win, tif_path, tif_path2):
    from gui.imageviewer import ImageViewerPanel
    win.dispatch_command("show-image", path=str(tif_path), name="v")
    win.dispatch_command("show-image", path=str(tif_path2), name="v", slot="right")
    tab = win.active_tab()
    assert isinstance(tab.panel("viewer"), ImageViewerPanel)
    assert isinstance(tab.panel("viewer-2"), ImageViewerPanel)
    assert tab.panel("viewer") is not tab.panel("viewer-2")
    assert not tab._left_container.isHidden()
    assert not tab._right_container.isHidden()
    assert tab._right_container.isAncestorOf(tab.panel("viewer-2"))


def test_show_image_split_preserves_primary_verbs(win, tif_path, tif_path2):
    """clobber 回帰: 2枚目追加後も set-channel 等の verb は 1枚目に効く
    （viewer-2 配置時は only_missing=True で既存 verb を clobber しない）。"""
    win.dispatch_command("show-image", path=str(tif_path), name="v")     # nC=3
    win.dispatch_command("show-image", path=str(tif_path2), name="v", slot="right")  # nC=2
    tab = win.active_tab()
    tab.dispatch_command("set-channel", index=2)
    assert tab.panel("viewer").active_channel == 2     # 1枚目に効く
    assert tab.panel("viewer-2").active_channel == 0   # 2枚目は不変


def test_show_image_panel_slot_conflict(win, tif_path, tif_path2):
    """panel=right + slot=right の矛盾は slot 優先・panel 無視（決定的）。"""
    win.dispatch_command("show-image", path=str(tif_path), name="v")
    win.dispatch_command(
        "show-image", path=str(tif_path2), name="v", panel="right", slot="right"
    )
    tab = win.active_tab()
    assert "viewer-2" in tab._panels
    assert tab._right_container.isAncestorOf(tab.panel("viewer-2"))
    assert tab._left_container.isAncestorOf(tab.panel("viewer"))


def test_show_image_invalid_slot(win, tif_path):
    with pytest.raises(ValueError, match="invalid slot"):
        win.dispatch_command("show-image", path=str(tif_path), slot="center")


def _img_split_env(monkeypatch, tmp_path):
    import config
    import dataset_config
    from llm_bridge import session
    monkeypatch.setattr(config, "DATASETS", {"myds": {}})
    monkeypatch.setattr(config, "get_dataset_dir", lambda name: tmp_path)
    monkeypatch.setattr(config, "reload_datasets", lambda *a, **k: None)
    monkeypatch.setattr(dataset_config, "get_work_dir", lambda name, create=True: tmp_path)
    monkeypatch.setattr(session, "_touched", set())


def test_image_split_roundtrip(qapp, tif_path, tif_path2, tmp_path, monkeypatch):
    from llm_bridge import session, _make_show_image_handler
    from gui.window import ToolWindow
    from gui.imageviewer import ImageViewerPanel
    _img_split_env(monkeypatch, tmp_path)

    w1 = ToolWindow()
    w1.register_command("show-image", _make_show_image_handler(w1))
    w1.dispatch_command("show-image", path=str(tif_path), name="v", dataset="myds")
    w1.dispatch_command("show-image", path=str(tif_path2), name="v", slot="right")
    saved, failed = session.save_all(w1)
    assert "myds" in saved and not failed
    entry = session.read_session("myds")["tabs"][0]
    assert entry["image2"] == "img2.tif"

    w2 = ToolWindow()
    w2.register_command("show-image", _make_show_image_handler(w2))
    assert session.open_dataset(w2, "myds").startswith("restored:")
    t = next(t for t in w2.tabs() if t.name == "v")
    assert isinstance(t.panel("viewer-2"), ImageViewerPanel)
    assert not t._left_container.isHidden()
    assert not t._right_container.isHidden()   # 右ペインが hide されていない


def test_image_layout_without_image2_collapses_on_restore(
    qapp, tif_path, tmp_path, monkeypatch
):
    """画像側の desync 対称: 両可視 layout + image2 なし → 収束型で単一へ畳む。"""
    from llm_bridge import session, _make_show_image_handler
    from gui.window import ToolWindow
    _img_split_env(monkeypatch, tmp_path)
    session.write_session("myds", {
        "version": 1, "dataset": "myds", "active_tab": "v",
        "tabs": [{
            "name": "v", "kind": "image", "image": "img.tif",
            "layout": {"orientation": "horizontal", "sizes": [600, 400],
                       "left_hidden": False, "right_hidden": False},
        }],
    })
    w = ToolWindow()
    w.register_command("show-image", _make_show_image_handler(w))
    assert session.open_dataset(w, "myds").startswith("restored:")
    t = next(t for t in w.tabs() if t.name == "v")
    assert not t._left_container.isHidden()
    assert t._right_container.isHidden()
