from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDockWidget,
    QLabel,
    QMainWindow,
    QTabWidget,
    QWidget,
)

from gui.tab import AnalysisTab

RESERVED_WINDOW_VERBS = frozenset([
    "add-tab", "close-tab", "set-active-tab",
    "toggle-chat-float", "toggle-chat-visible",
])


class ToolWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MyAnalysis Tool")
        self.resize(1400, 800)

        self._tabs = QTabWidget()
        self.setCentralWidget(self._tabs)
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self.statusBar()

        self._chat_dock = QDockWidget("Chat", self)
        self._chat_dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
        )
        self._chat_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self._chat_dock.setWidget(QLabel("(chat panel: not wired yet)"))
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._chat_dock)

        self._command_handlers: dict[str, Callable[..., object]] = {}

    def add_tab(self, tab: AnalysisTab) -> None:
        for i in range(self._tabs.count()):
            if self._tabs.widget(i).name == tab.name:
                raise KeyError(f"tab name {tab.name!r} already exists")
        self._tabs.addTab(tab, tab.name)

    def close_tab(self, name: str) -> bool:
        for i in range(self._tabs.count()):
            if self._tabs.widget(i).name == name:
                widget = self._tabs.widget(i)
                self._tabs.removeTab(i)
                widget.deleteLater()
                return True
        return False

    def active_tab(self) -> AnalysisTab | None:
        idx = self._tabs.currentIndex()
        return self._tabs.widget(idx) if idx >= 0 else None

    def set_active_tab(self, name: str) -> bool:
        for i in range(self._tabs.count()):
            if self._tabs.widget(i).name == name:
                self._tabs.setCurrentIndex(i)
                return True
        return False

    def set_chat_widget(self, widget: QWidget) -> None:
        self._chat_dock.setWidget(widget)

    def set_chat_floating(self, floating: bool) -> None:
        self._chat_dock.setFloating(floating)

    def set_chat_visible(self, visible: bool) -> None:
        self._chat_dock.setVisible(visible)

    def is_chat_floating(self) -> bool:
        return self._chat_dock.isFloating()

    def is_chat_visible(self) -> bool:
        return self._chat_dock.isVisible()

    def toggle_chat_floating(self) -> None:
        self._chat_dock.setFloating(not self._chat_dock.isFloating())

    def toggle_chat_visible(self) -> None:
        self._chat_dock.setVisible(not self._chat_dock.isVisible())

    def register_command(
        self, verb: str, handler: Callable[..., object]
    ) -> None:
        self._command_handlers[verb] = handler

    def dispatch_command(self, verb: str, **kwargs) -> object:
        return self._command_handlers[verb](**kwargs)

    def _on_tab_changed(self, idx: int) -> None:
        tab = self._tabs.widget(idx)
        if tab is not None:
            self.statusBar().showMessage(f"Active: {tab.name}")
