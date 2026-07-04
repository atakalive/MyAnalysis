"""Tests for gui/imageviewer.py (Issue #60). GUI (offscreen)."""
import numpy as np
import pytest


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def Panel(qapp):
    from gui.imageviewer import ImageViewerPanel
    return ImageViewerPanel


def _rgb_meta(arr, is_rgb=False):
    from common.image_io import _infer_meta, ImageMeta
    m = _infer_meta(arr)
    if is_rgb:
        m = ImageMeta(
            axes=m.axes, shape=m.shape, sizes=m.sizes, channels=m.channels,
            dtype=m.dtype, bit_depth=m.bit_depth, is_rgb=True,
        )
    return arr, m


def test_set_image_5d_state(Panel):
    p = Panel()
    arr = np.zeros((2, 3, 4, 8, 8), dtype=np.uint16)
    p.set_image(arr)
    st = p.get_state()
    assert set(st) == {"mode", "active_channel", "z", "t", "channels"}
    assert len(st["channels"]) == 4
    assert p.nZ == 3 and p.nT == 2 and p.nC == 4


def test_set_lut_and_invert(Panel):
    p = Panel()
    p.set_image(np.zeros((2, 8, 8), dtype=np.uint16))
    p.set_channel_lut(0, "Fire", invert=True)
    st = p.get_state()
    assert st["channels"][0]["lut"] == "Fire"
    assert st["channels"][0]["invert"] is True


def test_auto_contrast_brackets_percentiles(Panel):
    p = Panel()
    arr = np.zeros((1, 32, 32), dtype=np.uint16)
    arr[0, :16, :] = 1000
    arr[0, 16:, :] = 3000
    p.set_image(arr)
    p.auto_contrast_channel(0)
    lo, hi = p.channels[0]["levels"]
    assert lo < hi
    assert 1000 <= lo <= 3000


def test_composite_render_colors(Panel):
    p = Panel()
    arr = np.zeros((2, 8, 8), dtype=np.uint8)
    arr[0] = 255  # ch0
    arr[1] = 255  # ch1
    p.set_image(arr)
    p.set_channel_lut(0, "Red")
    p.set_channel_lut(1, "Green")
    p.set_channel_range(0, 0, 255)
    p.set_channel_range(1, 0, 255)
    p.set_mode("composite")
    rgb = p._last_rgb
    # red + green channels both max -> yellow (255,255,0)
    assert rgb[0, 0, 0] == 255
    assert rgb[0, 0, 1] == 255
    assert rgb[0, 0, 2] == 0


def test_composite_additive_overflow_clips(Panel):
    p = Panel()
    arr = np.zeros((2, 4, 4), dtype=np.uint8)
    arr[0] = 200
    arr[1] = 200
    p.set_image(arr)
    p.set_channel_lut(0, "Red")
    p.set_channel_lut(1, "Red")   # both map to red -> 400 before clip
    p.set_channel_range(0, 0, 255)
    p.set_channel_range(1, 0, 255)
    p.set_mode("composite")
    assert p._last_rgb[0, 0, 0] == 255   # clipped, not wrapped to a dark value


def test_rgb_pixel_equivalence(Panel):
    p = Panel()
    rgb = np.zeros((3, 6, 8), dtype=np.uint8)
    rgb[0] = 200
    rgb[1] = 100
    rgb[2] = 50
    p.set_image(_rgb_meta(rgb, is_rgb=True))
    out = p._last_rgb
    assert np.all(out[..., 0] == 200)
    assert np.all(out[..., 1] == 100)
    assert np.all(out[..., 2] == 50)


def test_sane_levels_constant_image_no_warning(Panel):
    p = Panel()
    arr = np.full((1, 8, 8), 500, dtype=np.uint16)
    p.set_image(arr)
    with np.errstate(divide="raise", invalid="raise"):
        p.auto_contrast_channel(0)
        p._on_reset_clicked(0)
        p._render()
    lo, hi = p.channels[0]["levels"]
    assert hi > lo


def test_nan_inf_render(Panel):
    p = Panel()
    arr = np.zeros((1, 8, 8), dtype=np.float32)
    arr[0, 0, 0] = np.nan
    arr[0, 0, 1] = np.inf
    arr[0, 1, 1] = 5.0
    p.set_image(arr)
    with np.errstate(divide="raise", invalid="raise"):
        p._render()
    assert p._last_rgb is not None
    # NaN pixel maps to black
    assert tuple(p._last_rgb[0, 0]) == (0, 0, 0)


def test_composite_constant_channel(Panel):
    p = Panel()
    arr = np.zeros((2, 8, 8), dtype=np.uint16)
    arr[0] = 500          # constant channel
    arr[1, :4] = 3000     # varying channel
    p.set_image(arr)
    p.set_mode("composite")
    with np.errstate(divide="raise", invalid="raise"):
        p._render()
    assert p._last_rgb is not None


def test_reload_channel_count_change(Panel):
    p = Panel()
    p.set_image(np.zeros((2, 8, 8), dtype=np.uint16))
    p.set_image(np.zeros((5, 8, 8), dtype=np.uint16))
    assert p.nC == 5
    assert len(p.channels) == 5
    p.set_active_channel(4)
    p.set_image(np.zeros((2, 8, 8), dtype=np.uint16))
    assert p.nC == 2
    assert p.active_channel < 2
    assert len(p.channels) == 2


def test_apply_state_channel_mismatch(Panel):
    p1 = Panel()
    p1.set_image(np.zeros((5, 8, 8), dtype=np.uint16))
    st = p1.get_state()
    p2 = Panel()
    p2.set_image(np.zeros((2, 8, 8), dtype=np.uint16))
    p2.apply_state(st)   # 5 saved channels, only 2 present -> no error
    assert len(p2.channels) == 2


def test_state_roundtrip(Panel):
    p1 = Panel()
    arr = np.arange(2 * 3 * 4 * 8 * 8, dtype=np.uint16).reshape(2, 3, 4, 8, 8)
    p1.set_image(arr)
    p1.set_z(1)
    p1.set_t(1)
    p1.set_active_channel(2)
    p1.set_channel_lut(0, "Fire")
    p1.set_channel_range(1, 10, 500)
    p1.set_mode("single")
    st = p1.get_state()
    p2 = Panel()
    p2.set_image(arr)
    p2.apply_state(st)
    assert p2.get_state() == st


def test_hover_coordinate_correspondence(Panel):
    from PySide6.QtCore import QPointF
    p = Panel()
    arr = np.zeros((1, 8, 10), dtype=np.uint16)  # (C=1, Y=8, X=10)
    arr[0, 3, 5] = 1234
    p.set_image(arr)

    class _Pt:
        def __init__(self, x, y):
            self._x, self._y = x, y

        def x(self):
            return self._x

        def y(self):
            return self._y

    # Patch mapSceneToView to return a known data coordinate at (x=5.4, y=3.4).
    p._vb.mapSceneToView = lambda pos: _Pt(5.4, 3.4)
    p._on_mouse_moved([QPointF(0, 0)])
    assert "x=5" in p._status.text()
    assert "y=3" in p._status.text()
    assert "1234" in p._status.text()


def test_hover_out_of_range_clears(Panel):
    from PySide6.QtCore import QPointF
    p = Panel()
    p.set_image(np.zeros((1, 8, 10), dtype=np.uint16))  # Y=8, X=10

    class _Pt:
        def __init__(self, x, y):
            self._x, self._y = x, y

        def x(self):
            return self._x

        def y(self):
            return self._y

    for (x, y) in [(-1, 3), (10, 3), (3, 8)]:
        p._status.setText("stale")
        p._vb.mapSceneToView = lambda pos, x=x, y=y: _Pt(x, y)
        p._on_mouse_moved([QPointF(0, 0)])
        assert p._status.text() == ""


def test_z_t_sliders_and_full_pixmap(Panel):
    p = Panel()
    arr = np.zeros((2, 3, 8, 8), dtype=np.uint16)  # ZCYX (4D)
    p.set_image(arr)
    p.set_z(1)
    assert p.get_state()["z"] == 1
    pm = p.full_pixmap()
    assert not pm.isNull()
    assert pm.width() == 8 and pm.height() == 8


def test_fire_lut_monotonic():
    from gui.imageviewer import lut_table
    t = lut_table("Fire")
    lum = 0.299 * t[:, 0] + 0.587 * t[:, 1] + 0.114 * t[:, 2]
    assert np.all(np.diff(lum) >= 0)


def test_spinbox_roundtrip_preserves_range(Panel):
    p = Panel()
    for arr in [
        np.zeros((1, 8, 8), dtype=np.uint16),
        (np.random.default_rng(0).random((1, 8, 8)) * 1e-6).astype(np.float64),
        (np.arange(64, dtype=np.float32).reshape(1, 8, 8) - 30.0),
    ]:
        p.set_image(arr)
        w = p._ch_widgets[0]
        lo = w["lo"].value()
        hi = w["hi"].value()
        w["lo"].setValue(lo)
        w["hi"].setValue(hi)
        p._on_spin_changed(0)
        clo, chi = p.channels[0]["levels"]
        assert chi > clo
