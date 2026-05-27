from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget


_PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
            "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]


class SelectorPanel(QWidget):
    selectionChanged = Signal(str)

    def __init__(self, label: str, options: list[str], parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        self._label = QLabel(label)
        self._combo = QComboBox()
        self._combo.addItems(options)
        layout.addWidget(self._label)
        layout.addWidget(self._combo)
        layout.addStretch(1)
        self._combo.currentTextChanged.connect(self.selectionChanged.emit)

    def set_options(self, options: list[str]) -> None:
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItems(options)
        self._combo.blockSignals(False)

    def current(self) -> str:
        return self._combo.currentText()


class TrajectoryPanel(QWidget):
    pointClicked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        self._plot = pg.PlotWidget()
        layout.addWidget(self._plot)
        self._scatter = pg.ScatterPlotItem()
        self._plot.addItem(self._scatter)
        self._scatter.sigClicked.connect(self._on_clicked)
        self._markers: list[pg.InfiniteLine] = []

    def _on_clicked(self, scatter_item, points, ev) -> None:
        if len(points) == 0:
            return
        idx = int(points[0].data())
        self.pointClicked.emit(idx)

    def set_data(
        self,
        x: np.ndarray,
        y: np.ndarray,
        color_by: list[str] | None = None,
        highlights: list[int] | None = None,
    ) -> None:
        if len(x) != len(y):
            raise ValueError(f"len(x) != len(y): {len(x)} vs {len(y)}")
        N = len(x)
        if color_by is not None and len(color_by) != N:
            raise ValueError(
                f"len(color_by) != len(x): {len(color_by)} vs {N}"
            )
        if highlights is not None:
            for i in highlights:
                if not isinstance(i, int):
                    raise ValueError(f"highlight index must be int, got {type(i).__name__}: {i}")
                if not (0 <= i < N):
                    raise ValueError(f"highlight index out of range: {i}")

        kwargs = dict(x=x, y=y, data=np.arange(N))

        if color_by is not None:
            cat_map: dict[str, str] = {}
            for cat in color_by:
                if cat not in cat_map:
                    cat_map[cat] = _PALETTE[len(cat_map) % len(_PALETTE)]
            kwargs["brush"] = [pg.mkBrush(cat_map[c]) for c in color_by]
        else:
            kwargs["brush"] = pg.mkBrush(_PALETTE[0])

        if highlights is not None:
            hl_set = set(highlights)
            default_pen = pg.mkPen(None)
            highlight_pen = pg.mkPen(color="#ffffff", width=2)
            kwargs["pen"] = [
                highlight_pen if i in hl_set else default_pen for i in range(N)
            ]
        else:
            kwargs["pen"] = pg.mkPen(None)

        self._scatter.setData(**kwargs)

    def add_marker(self, x: float, color: str = "#ff0000", label: str = "") -> None:
        line = pg.InfiniteLine(pos=x, angle=90, label=label, pen=pg.mkPen(color))
        self._plot.addItem(line)
        self._markers.append(line)

    def clear_markers(self) -> None:
        for line in self._markers:
            self._plot.removeItem(line)
        self._markers.clear()


class ImagePanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        self._view = pg.ImageView()
        layout.addWidget(self._view)
        self._notes: list[pg.TextItem] = []

    def set_image(self, image: np.ndarray | Path | str) -> None:
        if isinstance(image, np.ndarray):
            self._view.setImage(image)
        elif isinstance(image, (Path, str)):
            try:
                from PIL import Image as PILImage
            except ImportError as e:
                raise ImportError(
                    "Pillow is required to load images from a path. Run: pip install Pillow"
                ) from e
            arr = np.asarray(PILImage.open(image))
            self._view.setImage(arr)
        else:
            raise TypeError(f"unsupported image type: {type(image)!r}")

    def add_note(self, text: str, x: float = 0, y: float = 0) -> None:
        item = pg.TextItem(text)
        item.setPos(x, y)
        self._view.getView().addItem(item)
        self._notes.append(item)

    def clear_notes(self) -> None:
        view = self._view.getView()
        for item in self._notes:
            view.removeItem(item)
        self._notes.clear()
