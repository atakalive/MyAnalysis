import math
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QSplitter, QVBoxLayout, QWidget

RESERVED_TAB_VERBS = frozenset(["set-split", "add-panel", "remove-panel"])


class AnalysisTab(QWidget):
    """1 解析あたりのタブ内容。汎用器なので、パネル構成は外から組み立てる。"""

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.name = name
        self._panels: dict[str, QWidget] = {}
        self._state_provider: Callable[[], dict] | None = None
        self._snapshot_writer: Callable[["AnalysisTab"], None] | None = None
        self._command_handlers: dict[str, Callable[..., object]] = {}

        outer = QVBoxLayout(self)
        self._top_row = QHBoxLayout()
        outer.addLayout(self._top_row)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(self._splitter, stretch=1)

        self._left_container = QWidget()
        self._left_layout = QVBoxLayout(self._left_container)
        self._right_container = QWidget()
        self._right_layout = QVBoxLayout(self._right_container)
        self._splitter.addWidget(self._left_container)
        self._splitter.addWidget(self._right_container)

    def add_panel(
        self, key: str, widget: QWidget, position: str, stretch: int = 0
    ) -> None:
        if key in self._panels:
            raise KeyError(f"panel key {key!r} already registered")
        if position == "top":
            self._top_row.addWidget(widget)
        elif position == "left":
            self._left_layout.addWidget(widget, stretch=stretch)
        elif position == "right":
            self._right_layout.addWidget(widget, stretch=stretch)
        else:
            raise ValueError(f"unknown position: {position!r}")
        self._panels[key] = widget

    def remove_panel(self, key: str) -> None:
        """Remove and destroy a panel. Caller must disconnect signals before calling."""
        widget = self._panels.pop(key)
        widget.setParent(None)
        widget.deleteLater()

    def panel(self, key: str) -> QWidget:
        return self._panels[key]

    def set_split_ratio(self, left: float, right: float) -> None:
        if not (math.isfinite(left) and math.isfinite(right)):
            raise ValueError(f"left and right must be finite: got {left}, {right}")
        if left <= 0 or right <= 0:
            raise ValueError(f"left and right must be positive: got {left}, {right}")
        total = 1000
        l = int(total * left / (left + right))
        self._splitter.setSizes([l, total - l])

    def connect_state(self, provider: Callable[[], dict] | None) -> None:
        self._state_provider = provider

    def current_state(self) -> dict | None:
        return self._state_provider() if self._state_provider else None

    def connect_snapshot_writer(
        self, writer: Callable[["AnalysisTab"], None] | None
    ) -> None:
        self._snapshot_writer = writer

    def register_command(
        self, verb: str, handler: Callable[..., object]
    ) -> None:
        self._command_handlers[verb] = handler

    def dispatch_command(self, verb: str, **kwargs) -> object:
        return self._command_handlers[verb](**kwargs)

    def has_command(self, verb: str) -> bool:
        return verb in self._command_handlers

    def take_snapshot(self) -> None:
        if self._snapshot_writer:
            self._snapshot_writer(self)

    def apply_annotations(self, ann: dict) -> None:
        pass
