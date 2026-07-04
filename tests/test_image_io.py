"""Tests for common/image_io.py (Issue #60). Qt-free."""
import numpy as np
import pytest

from common.image_io import (
    _infer_meta,
    _normalize_axes,
    load_image,
)


@pytest.fixture
def tifffile():
    """tifffile only for the TIFF-positive tests; the PIL and graceful-
    ImportError tests must still run when tifffile is absent (design §4)."""
    return pytest.importorskip("tifffile")


def test_tiff_16bit_roundtrip(tmp_path, tifffile):
    arr = (np.arange(64 * 48, dtype=np.uint16).reshape(64, 48) * 7) % 65535
    p = tmp_path / "g.tif"
    tifffile.imwrite(str(p), arr)
    out, meta = load_image(p)
    assert out.dtype == np.uint16
    assert out.shape == (64, 48)
    assert meta.axes == "YX"
    assert meta.channels == 1
    assert meta.is_rgb is False
    assert np.array_equal(out, arr)


def test_tiff_ndim_tzcyx(tmp_path, tifffile):
    arr = np.zeros((2, 3, 4, 16, 20), dtype=np.uint16)
    p = tmp_path / "stack.tif"
    tifffile.imwrite(str(p), arr, imagej=True)
    out, meta = load_image(p)
    assert meta.axes == "TZCYX"
    assert meta.sizes == {"T": 2, "Z": 3, "C": 4, "Y": 16, "X": 20}
    assert meta.channels == 4
    assert out.shape == (2, 3, 4, 16, 20)


def test_tiff_naive_multipage_q_to_z(tmp_path, tifffile):
    """Plain multi-page TIFF (series.axes == 'QYX') opens as a Z=5 stack."""
    arr = np.zeros((5, 64, 64), dtype=np.uint16)
    p = tmp_path / "pages.tif"
    tifffile.imwrite(str(p), arr)
    with tifffile.TiffFile(str(p)) as tf:
        assert tf.series[0].axes == "QYX"
    out, meta = load_image(p)
    assert meta.sizes.get("Z") == 5
    assert meta.axes == "ZYX"


def test_normalize_axes_ambiguous(tmp_path):
    arr = np.zeros((2, 3, 8, 8), dtype=np.uint8)
    with pytest.raises(ValueError, match="ambiguous"):
        _normalize_axes(arr, "QIYX")


def test_normalize_axes_len_mismatch():
    arr = np.zeros((3, 8, 8), dtype=np.uint8)
    with pytest.raises(ValueError, match="mismatch"):
        _normalize_axes(arr, "CYXT")


def test_normalize_axes_duplicate_c():
    # A true C plus an S->C duplicate must be rejected (C appears twice).
    arr = np.zeros((2, 3, 8, 8, 3), dtype=np.uint8)
    with pytest.raises(ValueError, match="non-canonical"):
        _normalize_axes(arr, "ZCYXS")


def test_normalize_axes_missing_y():
    arr = np.zeros((3, 8), dtype=np.uint8)
    with pytest.raises(ValueError, match="non-canonical|unsupported"):
        _normalize_axes(arr, "CX")


def _save_rgb(path, saver):
    rgb = np.zeros((10, 12, 3), dtype=np.uint8)
    rgb[..., 0] = 200
    rgb[..., 1] = 100
    rgb[..., 2] = 50
    saver(path, rgb)
    return rgb


def test_is_rgb_structure_png_and_tiff(tmp_path, tifffile):
    from PIL import Image as PILImage

    png = tmp_path / "c.png"
    rgb = _save_rgb(png, lambda p, a: PILImage.fromarray(a).save(str(p)))
    tif = tmp_path / "c.tif"
    tifffile.imwrite(str(tif), rgb, photometric="rgb")

    expected = np.moveaxis(rgb, -1, 0)   # (3, Y, X) canonical CYX
    outs = []
    for path in (png, tif):
        out, meta = load_image(path)
        assert meta.is_rgb is True
        assert meta.axes == "CYX"
        assert meta.channels == 3
        assert np.array_equal(out, expected)   # pixel-equivalent to source
        outs.append(out)
    assert np.array_equal(outs[0], outs[1])    # png and tif identical


def test_multichannel_fluor_not_rgb(tmp_path, tifffile):
    arr = np.zeros((3, 16, 16), dtype=np.uint16)  # true C axis (CYX)
    p = tmp_path / "fl.tif"
    tifffile.imwrite(str(p), arr, imagej=True, metadata={"axes": "CYX"})
    out, meta = load_image(p)
    assert meta.is_rgb is False


def test_zstack_rgb_not_rgb(tmp_path, tifffile):
    arr = np.zeros((4, 8, 8, 3), dtype=np.uint8)  # ZYXS
    p = tmp_path / "zrgb.tif"
    tifffile.imwrite(str(p), arr)
    out, meta = load_image(p)
    assert meta.is_rgb is False


def test_pil_palette_png(tmp_path):
    from PIL import Image as PILImage

    img = PILImage.new("P", (12, 10))
    p = tmp_path / "pal.png"
    img.save(str(p))
    out, meta = load_image(p)
    assert out.shape == (3, 10, 12)
    assert meta.is_rgb is True


def test_pil_gray_L(tmp_path):
    from PIL import Image as PILImage

    img = PILImage.new("L", (12, 10))
    p = tmp_path / "g.png"
    img.save(str(p))
    out, meta = load_image(p)
    assert out.shape == (10, 12)
    assert meta.is_rgb is False


def test_pil_16bit_gray(tmp_path):
    from PIL import Image as PILImage

    arr = (np.arange(10 * 12, dtype=np.uint16).reshape(10, 12) * 5)
    img = PILImage.fromarray(arr)   # uint16 2D -> mode "I;16"
    p = tmp_path / "g16.png"
    img.save(str(p))
    out, meta = load_image(p)
    assert out.dtype == np.uint16
    assert meta.channels == 1
    assert meta.is_rgb is False
    assert np.array_equal(out, arr)   # 16bit values preserved (no 8bit downscale)


def test_pil_float_mode_preserved(tmp_path, monkeypatch):
    """PIL mode 'F' (float32) grayscale loads as 1ch float, dtype preserved."""
    from PIL import Image as PILImage

    farr = np.linspace(-1.0, 5.0, 10 * 12, dtype=np.float32).reshape(10, 12)
    fimg = PILImage.fromarray(farr)   # float32 2D -> mode "F"
    assert fimg.mode == "F"
    p = tmp_path / "f.bmp"            # a PIL-routed extension
    p.write_bytes(b"stub")
    monkeypatch.setattr(PILImage, "open", lambda _p: fimg)
    out, meta = load_image(p)
    assert out.dtype == np.float32
    assert meta.channels == 1
    assert meta.is_rgb is False
    assert np.allclose(out, farr)


def test_pil_la_drops_alpha(tmp_path):
    from PIL import Image as PILImage

    img = PILImage.new("LA", (12, 10))
    p = tmp_path / "la.png"
    img.save(str(p))
    out, meta = load_image(p)
    assert out.shape == (10, 12)
    assert meta.channels == 1
    assert meta.is_rgb is False


def test_rgba_symmetry_png_tiff(tmp_path, tifffile):
    from PIL import Image as PILImage

    rgba = np.zeros((10, 12, 4), dtype=np.uint8)
    rgba[..., 0] = 200
    rgba[..., 1] = 100
    rgba[..., 2] = 50
    rgba[..., 3] = 255
    png = tmp_path / "a.png"
    PILImage.fromarray(rgba).save(str(png))   # (H,W,4) uint8 -> mode "RGBA"
    tif = tmp_path / "a.tif"
    tifffile.imwrite(str(tif), rgba, photometric="rgb", extrasamples=["unassalpha"])

    outs = []
    for path in (png, tif):
        out, meta = load_image(path)
        assert meta.channels == 3
        assert meta.is_rgb is True
        outs.append(out)
    assert np.array_equal(outs[0], outs[1])


def test_infer_meta():
    assert _infer_meta(np.zeros((8, 8))).axes == "YX"
    assert _infer_meta(np.zeros((3, 8, 8))).axes == "CYX"
    assert _infer_meta(np.zeros((5, 4, 8, 8))).axes == "ZCYX"
    assert _infer_meta(np.zeros((2, 3, 4, 8, 8))).axes == "TZCYX"
    assert _infer_meta(np.zeros((3, 8, 8))).is_rgb is False
    with pytest.raises(ValueError):
        _infer_meta(np.zeros(8))               # 1D unsupported
    with pytest.raises(ValueError):
        _infer_meta(np.zeros((2, 2, 3, 4, 8, 8)))   # 6D unsupported


def test_infer_meta_bit_depth():
    assert _infer_meta(np.zeros((8, 8), dtype=np.uint16)).bit_depth == 16
    assert _infer_meta(np.zeros((8, 8), dtype=np.float64)).bit_depth == 64
    assert _infer_meta(np.zeros((8, 8), dtype=np.uint8)).bit_depth == 8


def test_tifffile_missing_graceful(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "tifffile":
            raise ImportError("no tifffile")
        return real_import(name, *a, **k)

    p = tmp_path / "x.tif"
    p.write_bytes(b"not a tiff")
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="pip install tifffile"):
        load_image(p)


def test_unknown_extension(tmp_path):
    p = tmp_path / "x.xyz"
    p.write_bytes(b"data")
    with pytest.raises(ValueError, match="unsupported image extension"):
        load_image(p)
