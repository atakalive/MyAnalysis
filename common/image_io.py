"""Image loader for the ImageJ-style viewer (Issue #60).

`load_image(path) -> (np.ndarray, ImageMeta)` returns an array whose spatial
axes Y/X are the last two axes (row-major, matching `gui/__init__` pyqtgraph
config) plus a frozen `ImageMeta` describing the canonical axis string, sizes,
channel count, dtype and whether the pixel structure is a plain RGB(A) colour
image.

Design notes:
- tifffile / PIL are imported lazily inside the per-extension loaders so the
  core module has no hard image-library dependency; a missing library yields a
  graceful ImportError pointing at `pip install ...`.
- `is_rgb` is decided by PIXEL STRUCTURE (a sample-derived C axis of size 3/4
  with no T/Z), not by the source library, so the same colour image opened as
  .png or .tif renders identically.
- Vendor formats (ND2/CZI/LIF) are future work via `enable_vendor_backends()` +
  `register_loader()`; the core registers only .tif/.tiff and PIL formats.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class ImageMeta:
    """Metadata for a loaded image (attribute access, e.g. `meta.axes`)."""

    axes: str                # canonical axis string, Y/X last (e.g. "TZCYX")
    shape: tuple[int, ...]   # arr.shape
    sizes: dict[str, int]    # axis -> size (includes C/Z/T when present)
    channels: int            # size of C axis, or 1 when absent
    dtype: np.dtype
    bit_depth: int           # dtype.itemsize * 8 (display hint only)
    is_rgb: bool


# Extension -> loader callable. Core registers .tif/.tiff and PIL formats.
_LOADERS: dict[str, Callable[[Path], tuple[np.ndarray, ImageMeta]]] = {}

_PIL_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".ppm", ".pgm", ".tga")


def register_loader(
    exts: list[str], loader: Callable[[Path], tuple[np.ndarray, ImageMeta]]
) -> None:
    """Register `loader` for each extension in `exts` (lower-case, dot-prefixed)."""
    for ext in exts:
        _LOADERS[ext.lower()] = loader


def load_image(path: str | Path) -> tuple[np.ndarray, ImageMeta]:
    """Load `path` into `(ndarray, ImageMeta)` via the extension registry."""
    p = Path(path)
    ext = p.suffix.lower()
    loader = _LOADERS.get(ext)
    if loader is None:
        known = ", ".join(sorted(_LOADERS)) or "(none)"
        raise ValueError(
            f"unsupported image extension {ext!r}; known extensions: {known}"
        )
    return loader(p)


def _bit_depth(dtype: np.dtype) -> int:
    return int(np.dtype(dtype).itemsize) * 8


def _make_meta(arr: np.ndarray, axes: str, is_rgb: bool) -> ImageMeta:
    sizes = dict(zip(axes, arr.shape))
    return ImageMeta(
        axes=axes,
        shape=tuple(arr.shape),
        sizes=sizes,
        channels=sizes.get("C", 1),
        dtype=arr.dtype,
        bit_depth=_bit_depth(arr.dtype),
        is_rgb=is_rgb,
    )


def _infer_meta(arr: np.ndarray) -> ImageMeta:
    """Meta for a bare ndarray (no source file). Last 2 axes = Y,X.

    The leading k = ndim-2 axes are named from the tail of canonical order
    "TZC": 2D->"YX", 3D->"CYX", 4D->"ZCYX", 5D->"TZCYX". `is_rgb` is always
    False (a bare array's colour intent is unknown; open via a file for RGB).
    """
    if arr.ndim < 2 or arr.ndim > 5:
        raise ValueError(
            f"unsupported ndim for image: {arr.ndim} (expected 2..5)"
        )
    lead = arr.ndim - 2
    axes = "TZC"[3 - lead:] + "YX"
    return _make_meta(arr, axes, is_rgb=False)


def _normalize_axes(arr: np.ndarray, axes: str) -> tuple[np.ndarray, str]:
    """Normalise a tifffile axis string to canonical form (T,Z,C then Y,X).

    - S (RGB sample axis) -> C.
    - A single unknown sequence axis Q/I -> Z (naive multi-page TIFF).
    - Y/X moved to the last two axes.
    Raises ValueError on ambiguous / unsupported / non-canonical axis sets.
    """
    if len(axes) != arr.ndim:
        raise ValueError(f"axes/ndim mismatch: {axes} vs {arr.shape}")
    chars = list(axes)
    # S -> C (sample-derived channel, feeds is_rgb decision downstream).
    chars = ["C" if c == "S" else c for c in chars]
    # Single unknown sequence axis Q/I -> Z; two or more is ambiguous.
    unknown_seq = [i for i, c in enumerate(chars) if c in ("Q", "I")]
    if len(unknown_seq) == 1:
        chars[unknown_seq[0]] = "Z"
    elif len(unknown_seq) >= 2:
        raise ValueError(f"ambiguous TIFF axes: {axes}")
    for c in chars:
        if c not in ("T", "Z", "C", "Y", "X"):
            raise ValueError(f"unsupported TIFF axes: {axes}")
    axes = "".join(chars)
    # Validate the canonical axis set BEFORE moving: exactly one Y and X, at most
    # one T/Z/C. Duplicate Y/X would otherwise make the moveaxis order malformed
    # and raise an opaque numpy error instead of this clear non-canonical one.
    if axes.count("Y") != 1 or axes.count("X") != 1:
        raise ValueError(f"non-canonical axes after normalize: {axes}")
    for c in ("T", "Z", "C"):
        if axes.count(c) > 1:
            raise ValueError(f"non-canonical axes after normalize: {axes}")
    # Move Y, X to the last two axes (data + label together).
    order = [i for i, c in enumerate(axes) if c not in ("Y", "X")]
    order += [axes.index("Y"), axes.index("X")]
    arr = np.moveaxis(arr, order, range(arr.ndim))
    axes = "".join(axes[i] for i in order)
    return arr, axes


def _is_rgb_structure(axes: str, sizes: dict, sample_c: bool) -> bool:
    """RGB iff a sample-derived C of size 3/4 with no T and no Z (pure 2D colour)."""
    if not sample_c or "C" not in axes:
        return False
    if "T" in axes or "Z" in axes:
        return False
    return sizes.get("C") in (3, 4)


def _load_tiff(p: Path) -> tuple[np.ndarray, ImageMeta]:
    try:
        import tifffile
    except ImportError as e:
        raise ImportError(
            "tifffile is required to open TIFF images. Run: pip install tifffile"
        ) from e
    with tifffile.TiffFile(str(p)) as tf:
        s = tf.series[0]
        arr = s.asarray()
        axes = s.axes
    sample_c = "S" in axes
    arr, axes = _normalize_axes(arr, axes)
    sizes = dict(zip(axes, arr.shape))
    is_rgb = _is_rgb_structure(axes, sizes, sample_c)
    # Drop alpha (4th sample) from sample-derived colour images so is_rgb is 3ch.
    if is_rgb and sizes.get("C") == 4:
        ci = axes.index("C")
        arr = np.take(arr, indices=range(3), axis=ci)
    return arr, _make_meta(arr, axes, is_rgb)


def _load_pil(p: Path) -> tuple[np.ndarray, ImageMeta]:
    """PIL loader. Grayscale family (1/L/LA/I/F and I;16*) -> single channel with
    dtype preserved: I->int32, F->float32, I;16*->uint16, 1->uint8, LA->uint8
    (alpha dropped). Everything else (P/RGB/RGBA/CMYK/YCbCr/...) -> convert("RGB")
    3ch, is_rgb=True (alpha/extra samples dropped; CMYK/YCbCr go through PIL's
    ICC-free approximation, so exact source colour is not guaranteed).
    """
    try:
        from PIL import Image as PILImage
    except ImportError as e:
        raise ImportError(
            "Pillow is required to load images from a path. Run: pip install Pillow"
        ) from e
    img = PILImage.open(p)
    mode = img.mode
    gray_modes = {"1", "L", "LA", "I", "F"}
    is_gray = mode in gray_modes or mode.startswith("I;16")
    if is_gray:
        if mode == "LA":
            img = img.convert("L")
        elif mode == "1":
            arr = np.asarray(img).astype(np.uint8)
            return arr, _make_meta(arr, "YX", is_rgb=False)
        arr = np.asarray(img)
        return arr, _make_meta(arr, "YX", is_rgb=False)
    # Colour / palette / unknown -> RGB (convert drops alpha + extra samples).
    arr = np.asarray(img.convert("RGB"))          # (Y, X, 3)
    arr = np.moveaxis(arr, -1, 0)                  # (3, Y, X)
    return arr, _make_meta(arr, "CYX", is_rgb=True)


register_loader([".tif", ".tiff"], _load_tiff)
register_loader(list(_PIL_EXTS), _load_pil)


# ---- extensible vendor backends (opt-in) ----

def enable_vendor_backends() -> None:
    """Register optional vendor-format loaders (ND2/CZI/LIF).

    The single explicit opt-in. Each loader lazily imports its reader only when
    a file of that extension is opened, so the core venv is never polluted.
    v1 ships no vendor loader bodies (§6); this is the extension seam.
    """
    return None
