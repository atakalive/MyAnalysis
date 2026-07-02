"""Single-tab pop-out window (Issue #56).

A floated analysis tab lives in one top-level ``FloatingTabWindow``. Float is a
non-persisted view state: the tab remains a member of its dataset group (via
``_DatasetGroup._floated``), so it still shows up in ``window.tabs()`` and is
saved/relayed/manifested as a normal docked tab. Closing the window re-docks.

Import direction is one-way: ``gui/window.py`` imports this module, never the
reverse. ``main`` is duck-typed and ``AnalysisTab`` is only imported under
TYPE_CHECKING to avoid an import cycle.

Note: this file contains NO user-facing translatable literals — the window
title is composed from identifier data (analysis name / dataset), which is not
translated (same treatment as the dataset-switcher labels). If CJK/translated
strings are added here in the future, wrap them with ``tr()`` and add this file
to the i18n ``IN_FILES`` list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QVBoxLayout, QWidget

if TYPE_CHECKING:
    from gui.tab import AnalysisTab


class FloatingTabWindow(QWidget):
    def __init__(self, main, tab: "AnalysisTab", dataset, name, size):
        super().__init__()
        self._main = main
        self._dataset = dataset
        self._name = name
        self.setObjectName("FloatingTabWindow")
        self.setWindowFlag(Qt.WindowType.Window)
        self.setWindowTitle(f"{name} — {dataset}" if dataset else name)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(tab)
        self.resize(
            size.width() if size.width() > 0 else 900,
            size.height() if size.height() > 0 else 700,
        )

    def closeEvent(self, event):
        main = self._main
        if getattr(main, "_shutting_down", False):
            event.accept()
            return
        dock = getattr(main, "_dock_tab", None)
        if callable(dock):
            event.ignore()  # 破棄せずタブを戻すだけ
            dock(self._dataset, self._name)
        else:
            event.accept()  # headless/fake 親 → 素通し（テスト耐性）
