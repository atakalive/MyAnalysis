"""ImageJ-style fluorescence image viewer panel (Issue #60).

A reusable, opt-in add-on component embedded into an analysis tab's split pane
by a single `attach_image_viewer(tab, image)` call, or opened ad-hoc via the
`show-image` window verb. It provides the ImageJ viewing essentials —
dynamic-range (min/max) control, LUTs, N-dimensional (max 5D `TZCYX`) z/t
stacks, and multi-channel composite — so agents can surface raw/source images
alongside quantitative analysis with one command.

This is a NEW module (not an extension of gui/panels.py) so the viewer's
dependencies and responsibilities stay local and the existing `ImagePanel`
stays untouched. It holds: the LUT registry, `ImageViewerPanel`,
`attach_image_viewer`, and `_register_viewer_verbs`.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from common.image_io import ImageMeta, load_image, _infer_meta


# ---------------------------------------------------------------------------
# LUT registry: each LUT is a (256, 3) uint8 index table; table[i] = RGB for
# input intensity i. Built once at import; both the normal and inverted table
# of every LUT are cached (composite references all visible channels per frame).
# ---------------------------------------------------------------------------

LUT_NAMES = [
    "Grays", "Red", "Green", "Blue", "Magenta", "Cyan", "Yellow",
    "Fire", "Ice", "Spectrum",
]

# Decorative (colour-map) LUT control points. Only Fire is required to be
# luminance-monotonic; Ice/Spectrum are decorative and are not.
_COLORMAP_STOPS = {
    "Fire": [
        (0.0, (0, 0, 0)), (0.2, (60, 0, 110)), (0.4, (160, 0, 90)),
        (0.6, (230, 70, 0)), (0.8, (255, 190, 0)), (1.0, (255, 255, 255)),
    ],
    "Ice": [
        (0.0, (0, 30, 120)), (0.25, (0, 150, 200)), (0.5, (150, 220, 220)),
        (0.75, (220, 200, 160)), (1.0, (255, 230, 240)),
    ],
    "Spectrum": [
        (0.0, (255, 0, 0)), (0.17, (255, 255, 0)), (0.33, (0, 255, 0)),
        (0.5, (0, 255, 255)), (0.67, (0, 0, 255)), (0.83, (255, 0, 255)),
        (1.0, (255, 0, 0)),
    ],
}


def _build_lut_tables() -> dict[str, np.ndarray]:
    r = np.arange(256, dtype=np.uint8)
    z = np.zeros(256, dtype=np.uint8)
    tables: dict[str, np.ndarray] = {
        # Monochrome ramps: identity mapping v -> (v,0,0) etc., no interpolation
        # bias (getLookupTable's linear ramp has a -1 truncation bias at some
        # indices which would break the RGB pixel-equivalence invariant).
        "Grays": np.column_stack([r, r, r]),
        "Red": np.column_stack([r, z, z]),
        "Green": np.column_stack([z, r, z]),
        "Blue": np.column_stack([z, z, r]),
        "Magenta": np.column_stack([r, z, r]),
        "Cyan": np.column_stack([z, r, r]),
        "Yellow": np.column_stack([r, r, z]),
    }
    for name, stops in _COLORMAP_STOPS.items():
        pos = np.array([s[0] for s in stops], dtype=float)
        colors = np.array([s[1] for s in stops], dtype=float)
        cmap = pg.ColorMap(pos, colors)
        tables[name] = cmap.getLookupTable(0.0, 1.0, 256).astype(np.uint8)[:, :3]
    return tables


_LUT_TABLES = _build_lut_tables()
# Cache the inverted tables too (row-reverse is mathematically correct for
# asymmetric colour maps and exact for the identity monochrome ramps).
_LUT_TABLES_INV = {name: t[::-1].copy() for name, t in _LUT_TABLES.items()}


def lut_table(name: str, invert: bool = False) -> np.ndarray:
    """Return the cached (256, 3) uint8 table for `name` (optionally inverted)."""
    if name not in _LUT_TABLES:
        name = "Grays"
    return _LUT_TABLES_INV[name] if invert else _LUT_TABLES[name]


# Default composite colour order for fluorescence (grayscale-family) images.
# Intentional to this tool; differs from ImageJ's strict C1=gray default.
_FLUOR_ORDER = ["Green", "Red", "Blue", "Magenta", "Cyan", "Yellow"]
_RGB_ORDER = ["Red", "Green", "Blue"]

_AUTO_LOW = 0.35
_AUTO_HIGH = 99.65


def _sane_levels(lo, hi, frame) -> tuple[float, float]:
    """Coerce (lo, hi) to a finite, strictly-increasing pair for `frame`.

    The stored `levels` invariant is always finite and hi > lo, so composite's
    (frame-lo)/(hi-lo) never divides by zero / injects NaN / warns.
    """
    dt = frame.dtype
    if np.issubdtype(dt, np.floating):
        finite = frame[np.isfinite(frame)]
    else:
        finite = frame.reshape(-1)
    if finite.size:
        fmin, fmax = float(finite.min()), float(finite.max())
    elif np.issubdtype(dt, np.integer):
        fmin, fmax = float(np.iinfo(dt).min), float(np.iinfo(dt).max)
    else:
        fmin, fmax = 0.0, 1.0
    lo = lo if np.isfinite(lo) else fmin
    hi = hi if np.isfinite(hi) else fmax
    if hi <= lo:
        hi = lo + 1 if np.issubdtype(dt, np.integer) else np.nextafter(float(lo), np.inf)
    if not (np.isfinite(lo) and np.isfinite(hi) and hi > lo):
        lo, hi = 0.0, 1.0
    return float(lo), float(hi)


def _finite_range(frame) -> tuple[float, float]:
    """Finite (min, max) of `frame`, falling back to dtype range when empty."""
    dt = frame.dtype
    if np.issubdtype(dt, np.floating):
        finite = frame[np.isfinite(frame)]
    else:
        finite = frame.reshape(-1)
    if finite.size:
        return float(finite.min()), float(finite.max())
    if np.issubdtype(dt, np.integer):
        return float(np.iinfo(dt).min), float(np.iinfo(dt).max)
    return 0.0, 1.0


def _percentile_levels(frame, low=_AUTO_LOW, high=_AUTO_HIGH) -> tuple[float, float]:
    dt = frame.dtype
    if np.issubdtype(dt, np.floating):
        vals = frame[np.isfinite(frame)]
    else:
        vals = frame.reshape(-1)
    if vals.size == 0:
        lo, hi = _finite_range(frame)
    else:
        lo = float(np.percentile(vals, low))
        hi = float(np.percentile(vals, high))
    return _sane_levels(lo, hi, frame)


def _clamp(v, n) -> int:
    return max(0, min(int(v), n - 1))


def _as_bool(v) -> bool:
    """Coerce a CLI/JSON value to bool (CLI delivers `false` as the string)."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


class ImageViewerPanel(QWidget):
    """N-dimensional fluorescence image viewer with composite + LUTs (Issue #60)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # State attributes initialised before any signal wiring.
        self._arr: np.ndarray | None = None
        self._meta: ImageMeta | None = None
        self._axes = ""
        self.nY = self.nX = 1
        self.nC = self.nZ = self.nT = 1
        self.active_channel = 0
        self.z_idx = 0
        self.t_idx = 0
        self.mode = "single"
        self.channels: list[dict] = []
        self._last_rgb: np.ndarray | None = None
        self._ch_widgets: list[dict] = []
        self._updating_ui = False

        outer = QVBoxLayout(self)

        self._glw = pg.GraphicsLayoutWidget()
        self._glw.setBackground(self.palette().window().color())
        self._vb = self._glw.addViewBox()
        self._vb.setAspectLocked(True)
        self._vb.invertY(True)   # y downward, matching ImageJ / row-major index
        self._img_item = pg.ImageItem()
        self._vb.addItem(self._img_item)
        outer.addWidget(self._glw, stretch=1)

        # Mode toggle + active-channel selector.
        mode_row = QHBoxLayout()
        self._mode_combo = QComboBox()
        self._mode_combo.addItems(["single", "composite"])
        self._mode_combo.currentTextChanged.connect(self._on_mode_changed)
        mode_row.addWidget(QLabel("mode"))
        mode_row.addWidget(self._mode_combo)
        self._active_combo = QComboBox()
        self._active_combo.currentIndexChanged.connect(self._on_active_changed)
        mode_row.addWidget(QLabel("active"))
        mode_row.addWidget(self._active_combo)
        mode_row.addStretch(1)
        outer.addLayout(mode_row)

        # Z / T sliders (shown only when the axis size > 1).
        self._z_row, self._z_slider, self._z_label = self._make_axis_slider("Z")
        self._t_row, self._t_slider, self._t_label = self._make_axis_slider("T")
        self._z_slider.valueChanged.connect(self._on_z_changed)
        self._t_slider.valueChanged.connect(self._on_t_changed)
        outer.addLayout(self._z_row)
        outer.addLayout(self._t_row)

        # Per-channel control grid (rebuilt per image).
        self._ch_grid = QGridLayout()
        outer.addLayout(self._ch_grid)

        # Hover pixel-value readout (ImageJ status bar).
        self._status = QLabel("")
        outer.addWidget(self._status)
        self._hover_proxy = pg.SignalProxy(
            self._vb.scene().sigMouseMoved, rateLimit=60, slot=self._on_mouse_moved
        )

    # ---- axis sliders --------------------------------------------------

    def _make_axis_slider(self, label: str):
        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setMinimum(0)
        readout = QLabel("0")
        row.addWidget(slider, stretch=1)
        row.addWidget(readout)
        return row, slider, readout

    def _set_row_visible(self, row, visible: bool) -> None:
        for i in range(row.count()):
            w = row.itemAt(i).widget()
            if w is not None:
                w.setVisible(visible)

    # ---- image loading -------------------------------------------------

    def set_image(self, image) -> None:
        """Load an image (path / ndarray / (ndarray, meta)) into the panel.

        Reload contract: keep existing per-channel state (levels/LUT/invert/
        visible) and position only when C/Z/T/Y/X exactly match the previous
        image; otherwise reinitialise all channels and clamp position.
        """
        arr, meta = self._resolve_image(image)
        same_shape = (
            self._arr is not None
            and self._axes == meta.axes
            and self._arr.shape == arr.shape
        )
        self._arr = arr
        self._meta = meta
        self._axes = meta.axes
        sizes = meta.sizes
        self.nY = sizes["Y"]
        self.nX = sizes["X"]
        self.nC = meta.channels
        self.nZ = sizes.get("Z", 1)
        self.nT = sizes.get("T", 1)
        # Clamp position into the new sizes (invariant at every entry point).
        self.active_channel = _clamp(self.active_channel, self.nC)
        self.z_idx = _clamp(self.z_idx, self.nZ)
        self.t_idx = _clamp(self.t_idx, self.nT)
        if not same_shape or len(self.channels) != self.nC:
            self._init_channels()
        self._build_dynamic_controls()
        self._render()

    def _resolve_image(self, image) -> tuple[np.ndarray, ImageMeta]:
        if isinstance(image, tuple) and len(image) == 2 and isinstance(image[1], ImageMeta):
            return image[0], image[1]
        if isinstance(image, (str, Path)):
            return load_image(image)
        if isinstance(image, np.ndarray):
            return image, _infer_meta(image)
        raise TypeError(f"unsupported image type: {type(image)!r}")

    def _init_channels(self) -> None:
        is_rgb = self._meta.is_rgb
        self.channels = []
        for c in range(self.nC):
            frame = self._plane(c)
            if is_rgb:
                name = _RGB_ORDER[c] if c < len(_RGB_ORDER) else "Grays"
                fmin, fmax = _finite_range(frame)
                if np.issubdtype(frame.dtype, np.integer):
                    fmin, fmax = float(np.iinfo(frame.dtype).min), float(
                        np.iinfo(frame.dtype).max
                    )
                lo, hi = _sane_levels(fmin, fmax, frame)
            else:
                name = _FLUOR_ORDER[c % len(_FLUOR_ORDER)] if self.nC > 1 else "Grays"
                lo, hi = _percentile_levels(frame)
            self.channels.append(
                {"lut_name": name, "invert": False, "levels": (lo, hi), "visible": True}
            )
        # Load-time mode default.
        if is_rgb:
            self.mode = "composite"
        else:
            self.mode = "composite" if self.nC > 1 else "single"

    # ---- plane extraction ---------------------------------------------

    def _plane(self, ch) -> np.ndarray:
        idx = []
        for ax in self._axes:
            if ax == "T":
                idx.append(self.t_idx)
            elif ax == "Z":
                idx.append(self.z_idx)
            elif ax == "C":
                idx.append(ch)
            else:
                idx.append(slice(None))
        return self._arr[tuple(idx)]

    # ---- rendering -----------------------------------------------------

    def _channel_rgb(self, c: int) -> np.ndarray:
        frame = self._plane(c)
        lo, hi = self.channels[c]["levels"]
        norm = np.clip((frame.astype(np.float32) - lo) / (hi - lo), 0.0, 1.0)
        norm = np.nan_to_num(norm, nan=0.0)
        idx = (norm * 255).astype(np.uint8)
        ch = self.channels[c]
        return lut_table(ch["lut_name"], ch["invert"])[idx]

    def _render(self) -> None:
        if self._arr is None:
            return
        if self.mode == "single":
            self._last_rgb = self._channel_rgb(self.active_channel)
        else:
            acc = np.zeros((self.nY, self.nX, 3), dtype=np.float32)
            for c in range(self.nC):
                if not self.channels[c]["visible"]:
                    continue
                acc += self._channel_rgb(c)   # float32 sum avoids uint8 wrap
            self._last_rgb = np.clip(acc, 0, 255).astype(np.uint8)
        self._img_item.setImage(
            self._last_rgb, autoLevels=False, levels=(0, 255), lut=None
        )

    def full_pixmap(self) -> QPixmap:
        """Return the rendered composite/single RGB as an origin-size QPixmap."""
        if self._last_rgb is None:
            return self.grab()
        buf = np.ascontiguousarray(self._last_rgb)
        h, w = buf.shape[0], buf.shape[1]
        img = QImage(buf.data, w, h, 3 * w, QImage.Format.Format_RGB888)
        # copy() detaches from `buf` so the pixmap owns independent data.
        return QPixmap.fromImage(img).copy()

    # ---- dynamic per-channel controls ---------------------------------

    def _build_dynamic_controls(self) -> None:
        self._updating_ui = True
        # Clear existing grid widgets.
        while self._ch_grid.count():
            item = self._ch_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._ch_widgets = []

        for c in range(self.nC):
            ch = self.channels[c]
            frame = self._plane(c)
            fmin, fmax = _finite_range(frame)

            vis = QCheckBox()
            vis.setChecked(ch["visible"])
            vis.stateChanged.connect(lambda _s, i=c: self._on_visible_toggled(i))

            lut = QComboBox()
            lut.addItems(LUT_NAMES)
            lut.setCurrentText(ch["lut_name"])
            lut.currentTextChanged.connect(lambda _t, i=c: self._on_lut_changed(i))

            inv = QCheckBox("inv")
            inv.setChecked(ch["invert"])
            inv.stateChanged.connect(lambda _s, i=c: self._on_lut_changed(i))

            lo_spin = QDoubleSpinBox()
            hi_spin = QDoubleSpinBox()
            self._configure_spinbox(lo_spin, frame, fmin, fmax)
            self._configure_spinbox(hi_spin, frame, fmin, fmax)
            lo_spin.setValue(ch["levels"][0])
            hi_spin.setValue(ch["levels"][1])
            lo_spin.valueChanged.connect(lambda _v, i=c: self._on_spin_changed(i))
            hi_spin.valueChanged.connect(lambda _v, i=c: self._on_spin_changed(i))

            auto_btn = QPushButton("Auto")
            auto_btn.clicked.connect(lambda _c=False, i=c: self._on_auto_clicked(i))
            reset_btn = QPushButton("Reset")
            reset_btn.clicked.connect(lambda _c=False, i=c: self._on_reset_clicked(i))

            widgets = {
                "visible": vis, "lut": lut, "invert": inv,
                "lo": lo_spin, "hi": hi_spin, "auto": auto_btn, "reset": reset_btn,
            }
            self._ch_widgets.append(widgets)
            self._ch_grid.addWidget(QLabel(f"C{c}"), c, 0)
            self._ch_grid.addWidget(vis, c, 1)
            self._ch_grid.addWidget(lut, c, 2)
            self._ch_grid.addWidget(inv, c, 3)
            self._ch_grid.addWidget(lo_spin, c, 4)
            self._ch_grid.addWidget(hi_spin, c, 5)
            self._ch_grid.addWidget(auto_btn, c, 6)
            self._ch_grid.addWidget(reset_btn, c, 7)

        # Active-channel + mode selectors.
        self._active_combo.blockSignals(True)
        self._active_combo.clear()
        self._active_combo.addItems([f"C{c}" for c in range(self.nC)])
        self._active_combo.setCurrentIndex(self.active_channel)
        self._active_combo.blockSignals(False)
        self._mode_combo.blockSignals(True)
        self._mode_combo.setCurrentText(self.mode)
        self._mode_combo.blockSignals(False)

        # Z / T sliders.
        self._z_slider.blockSignals(True)
        self._z_slider.setMaximum(max(0, self.nZ - 1))
        self._z_slider.setValue(self.z_idx)
        self._z_slider.blockSignals(False)
        self._z_label.setText(str(self.z_idx))
        self._set_row_visible(self._z_row, self.nZ > 1)
        self._t_slider.blockSignals(True)
        self._t_slider.setMaximum(max(0, self.nT - 1))
        self._t_slider.setValue(self.t_idx)
        self._t_slider.blockSignals(False)
        self._t_label.setText(str(self.t_idx))
        self._set_row_visible(self._t_row, self.nT > 1)

        self._updating_ui = False

    def _configure_spinbox(self, spin, frame, fmin, fmax) -> None:
        dt = frame.dtype
        width = fmax - fmin
        if np.issubdtype(dt, np.integer):
            info = np.iinfo(dt)
            spin.setRange(float(info.min), float(info.max))
            spin.setDecimals(0)
            spin.setSingleStep(1)
            return
        margin = max(width, 1.0)
        spin.setRange(fmin - margin, fmax + margin)
        if width > 0:
            try:
                precision = np.finfo(dt).precision
            except (ValueError, TypeError):
                precision = 15
            mag = max(abs(fmin), abs(fmax), 1e-30)
            cap = max(2, precision - int(np.floor(np.log10(mag))))
            decimals = min(15, max(2, 3 - int(np.floor(np.log10(width)))), cap)
        else:
            decimals = 2
        spin.setDecimals(decimals)
        spin.setSingleStep(max(width, 1.0) * 0.01)

    # ---- signal handlers ----------------------------------------------

    def _on_mode_changed(self, text: str) -> None:
        if self._updating_ui:
            return
        self.set_mode(text)

    def _on_active_changed(self, idx: int) -> None:
        if self._updating_ui or idx < 0:
            return
        self.set_active_channel(idx)

    def _on_z_changed(self, v: int) -> None:
        if self._updating_ui:
            return
        self.set_z(v)

    def _on_t_changed(self, v: int) -> None:
        if self._updating_ui:
            return
        self.set_t(v)

    def _on_visible_toggled(self, c: int) -> None:
        if self._updating_ui:
            return
        self.set_channel_visible(c, self._ch_widgets[c]["visible"].isChecked())

    def _on_lut_changed(self, c: int) -> None:
        if self._updating_ui:
            return
        w = self._ch_widgets[c]
        self.set_channel_lut(c, w["lut"].currentText(), w["invert"].isChecked())

    def _on_spin_changed(self, c: int) -> None:
        if self._updating_ui:
            return
        w = self._ch_widgets[c]
        self.set_channel_range(c, w["lo"].value(), w["hi"].value())

    def _on_auto_clicked(self, c: int) -> None:
        self.auto_contrast_channel(c)

    def _on_reset_clicked(self, c: int) -> None:
        frame = self._plane(c)
        fmin, fmax = _finite_range(frame)
        self.set_channel_range(c, fmin, fmax)

    def _on_mouse_moved(self, evt) -> None:
        if self._arr is None:
            return
        pos = evt[0]
        pt = self._vb.mapSceneToView(pos)
        xi = int(np.floor(pt.x()))
        yi = int(np.floor(pt.y()))
        if not (0 <= xi < self.nX and 0 <= yi < self.nY):
            self._status.setText("")
            return
        parts = [f"x={xi}", f"y={yi}"]
        for c in range(self.nC):
            parts.append(f"C{c}={self._plane(c)[yi, xi]}")
        self._status.setText("  ".join(parts))

    # ---- public mutators (used by verbs + UI) -------------------------

    def _sync_spin_widgets(self, c: int) -> None:
        if c >= len(self._ch_widgets):
            return
        w = self._ch_widgets[c]
        lo, hi = self.channels[c]["levels"]
        w["lo"].blockSignals(True)
        w["hi"].blockSignals(True)
        w["lo"].setValue(lo)
        w["hi"].setValue(hi)
        w["lo"].blockSignals(False)
        w["hi"].blockSignals(False)

    def set_mode(self, mode: str) -> None:
        if mode not in ("single", "composite"):
            raise ValueError(f"unknown mode: {mode!r}")
        self.mode = mode
        self._render()

    def set_active_channel(self, idx) -> None:
        self.active_channel = _clamp(idx, self.nC)
        if not self._updating_ui:
            self._active_combo.blockSignals(True)
            self._active_combo.setCurrentIndex(self.active_channel)
            self._active_combo.blockSignals(False)
        self._render()

    def set_z(self, idx) -> None:
        self.z_idx = _clamp(idx, self.nZ)
        self._z_label.setText(str(self.z_idx))
        self._render()

    def set_t(self, idx) -> None:
        self.t_idx = _clamp(idx, self.nT)
        self._t_label.setText(str(self.t_idx))
        self._render()

    def set_channel_visible(self, c, visible) -> None:
        c = _clamp(c, self.nC)
        self.channels[c]["visible"] = bool(visible)
        self._render()

    def set_channel_lut(self, c, name: str, invert: bool = False) -> None:
        c = _clamp(c, self.nC)
        if name not in _LUT_TABLES:
            raise ValueError(f"unknown LUT: {name!r}")
        self.channels[c]["lut_name"] = name
        self.channels[c]["invert"] = bool(invert)
        self._render()

    def set_channel_range(self, c, lo, hi) -> None:
        c = _clamp(c, self.nC)
        frame = self._plane(c)
        self.channels[c]["levels"] = _sane_levels(float(lo), float(hi), frame)
        self._sync_spin_widgets(c)
        self._render()

    def auto_contrast_channel(self, c, low=_AUTO_LOW, high=_AUTO_HIGH) -> None:
        c = _clamp(c, self.nC)
        frame = self._plane(c)
        self.channels[c]["levels"] = _percentile_levels(frame, low, high)
        self._sync_spin_widgets(c)
        self._render()

    def resolve_channel(self, channel) -> int:
        """None -> active channel; else clamp into range."""
        if channel is None:
            return self.active_channel
        return _clamp(channel, self.nC)

    # ---- state round-trip ---------------------------------------------

    def get_state(self) -> dict:
        return {
            "mode": self.mode,
            "active_channel": self.active_channel,
            "z": self.z_idx,
            "t": self.t_idx,
            "channels": [
                {
                    "lut": ch["lut_name"],
                    "invert": ch["invert"],
                    "levels": [ch["levels"][0], ch["levels"][1]],
                    "visible": ch["visible"],
                }
                for ch in self.channels
            ],
        }

    def apply_state(self, state: dict) -> None:
        if not isinstance(state, dict):
            return
        saved = state.get("channels") or []
        for c in range(min(self.nC, len(saved))):
            s = saved[c]
            if not isinstance(s, dict):
                continue
            frame = self._plane(c)
            lut = s.get("lut", self.channels[c]["lut_name"])
            if lut not in _LUT_TABLES:
                lut = self.channels[c]["lut_name"]
            lv = s.get("levels")
            if isinstance(lv, (list, tuple)) and len(lv) == 2:
                levels = _sane_levels(float(lv[0]), float(lv[1]), frame)
            else:
                levels = self.channels[c]["levels"]
            self.channels[c] = {
                "lut_name": lut,
                "invert": bool(s.get("invert", False)),
                "levels": levels,
                "visible": bool(s.get("visible", True)),
            }
        mode = state.get("mode")
        if mode in ("single", "composite"):
            self.mode = mode
        if "active_channel" in state:
            self.active_channel = _clamp(state["active_channel"], self.nC)
        if "z" in state:
            self.z_idx = _clamp(state["z"], self.nZ)
        if "t" in state:
            self.t_idx = _clamp(state["t"], self.nT)
        self._build_dynamic_controls()
        self._render()


# ---------------------------------------------------------------------------
# Attach helper + verb registration
# ---------------------------------------------------------------------------

def attach_image_viewer(tab, image=None, panel="left", key="viewer"):
    """Embed an ImageViewerPanel in `tab`'s split pane and wire its verbs.

    The single-line entry point for embedding the viewer in an analysis
    `build_tab`. Returns the panel so the analysis can fold its state into
    get/apply_state (`{**own, "viewer": panel.get_state()}`).
    """
    p = ImageViewerPanel()
    tab.add_panel(key, p, panel, stretch=1)
    if image is not None:
        p.set_image(image)
    _register_viewer_verbs(tab, p)
    return p


def _register_viewer_verbs(tab, panel) -> None:
    """Register composite-aware viewer verbs on `tab`.

    HARD RULE: never (re)register the `set-split`/`snapshot`/`refresh-state`
    verbs that `attach_tab` wires — `tab.register_command` overwrites by key, so
    doing so would clobber existing behaviour. The viewer verb key-set is
    disjoint from those three, so call order relative to attach_tab is
    irrelevant.
    """

    def _set_lut(lut, channel=None, invert=False):
        c = panel.resolve_channel(channel)
        panel.set_channel_lut(c, str(lut), _as_bool(invert))
        return f"lut:{c}:{lut}"

    def _set_range(min, max, channel=None):
        c = panel.resolve_channel(channel)
        panel.set_channel_range(c, float(min), float(max))
        return f"range:{c}"

    def _auto_contrast(channel=None, low=_AUTO_LOW, high=_AUTO_HIGH):
        c = panel.resolve_channel(channel)
        panel.auto_contrast_channel(c, float(low), float(high))
        return f"auto:{c}"

    def _set_channel(index):
        panel.set_active_channel(int(index))
        return f"channel:{panel.active_channel}"

    def _set_mode(mode):
        panel.set_mode(str(mode))
        return f"mode:{panel.mode}"

    def _set_z(index):
        panel.set_z(int(index))
        return f"z:{panel.z_idx}"

    def _set_t(index):
        panel.set_t(int(index))
        return f"t:{panel.t_idx}"

    def _set_visible(channel, visible):
        c = _clamp(channel, panel.nC)
        panel.set_channel_visible(c, _as_bool(visible))
        return f"visible:{c}:{panel.channels[c]['visible']}"

    def _load_image(path):
        panel.set_image(path)
        return f"loaded:{path}"

    tab.register_command("set-lut", _set_lut)
    tab.register_command("set-range", _set_range)
    tab.register_command("auto-contrast", _auto_contrast)
    tab.register_command("set-channel", _set_channel)
    tab.register_command("set-mode", _set_mode)
    tab.register_command("set-z", _set_z)
    tab.register_command("set-t", _set_t)
    tab.register_command("set-visible", _set_visible)
    tab.register_command("load-image", _load_image)
