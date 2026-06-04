from collections.abc import Callable

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QDockWidget,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QTabWidget,
    QWidget,
)

from gui.tab import AnalysisTab

RESERVED_WINDOW_VERBS = frozenset([
    "add-tab", "close-tab", "list-tabs", "set-active-tab",
    "show", "toggle-chat-float",
])


class ToolWindow(QMainWindow):
    tab_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MyAnalysis Tool")
        self.resize(1400, 800)

        self._tabs = QTabWidget()
        self.setCentralWidget(self._tabs)
        self._tabs.currentChanged.connect(self._on_tab_changed)
        tab_bar = self._tabs.tabBar()
        tab_bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tab_bar.customContextMenuRequested.connect(self._on_tab_context_menu)
        self.statusBar()

        self._chat_dock = QDockWidget("Chat", self)
        self._chat_dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
        )
        self._chat_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self._chat_dock.setWidget(QLabel("(chat panel: not wired yet)"))
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._chat_dock)

        self._command_handlers: dict[str, Callable[..., object]] = {}

        file_menu = self.menuBar().addMenu("ファイル(&F)")
        open_action = file_menu.addAction("解析を開く…")
        open_action.triggered.connect(self._open_analysis)
        file_menu.addSeparator()
        quit_action = file_menu.addAction("終了")
        quit_action.triggered.connect(self.close)

        view_menu = self.menuBar().addMenu("表示(&V)")
        chat_action = view_menu.addAction("チャットを切り離す/格納")
        chat_action.triggered.connect(self.toggle_chat_floating)

        help_menu = self.menuBar().addMenu("ヘルプ(&H)")
        about_action = help_menu.addAction("バージョン情報")
        about_action.triggered.connect(
            lambda: QMessageBox.about(self, "MyAnalysis Tool", "MyAnalysis Tool v0.1")
        )

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

    def tab_names(self) -> list[str]:
        return [self._tabs.widget(i).name for i in range(self._tabs.count())]

    def set_chat_widget(self, widget: QWidget) -> None:
        self._chat_dock.setWidget(widget)

    def set_chat_floating(self, floating: bool) -> None:
        self._chat_dock.setFloating(floating)

    def is_chat_floating(self) -> bool:
        return self._chat_dock.isFloating()

    def toggle_chat_floating(self) -> None:
        self._chat_dock.setFloating(not self._chat_dock.isFloating())

    def register_command(
        self, verb: str, handler: Callable[..., object]
    ) -> None:
        self._command_handlers[verb] = handler

    def dispatch_command(self, verb: str, **kwargs) -> object:
        return self._command_handlers[verb](**kwargs)

    def has_command(self, verb: str) -> bool:
        return verb in self._command_handlers

    def _open_analysis(self) -> None:
        from common.paths import analyses_root

        root = analyses_root()
        if root.is_dir():
            names = sorted(
                d.name
                for d in root.glob("*")
                if (d / "analysis.py").is_file()
            )
        else:
            names = []
        if not names:
            QMessageBox.information(self, "解析を開く", "解析がありません")
            return
        name, ok = QInputDialog.getItem(
            self, "解析を開く", "解析を選択:", names, 0, False
        )
        if not ok or not name:
            return
        if not self.has_command("add-tab"):
            QMessageBox.critical(self, "エラー", "add-tab コマンドが未登録です")
            return
        try:
            self.dispatch_command("add-tab", name=name)
            self.set_active_tab(name)
        except Exception as e:
            QMessageBox.critical(self, "解析を開けません", str(e))

    def _on_tab_context_menu(self, pos: QPoint) -> None:
        tab_bar = self._tabs.tabBar()
        index = tab_bar.tabAt(pos)
        if index < 0:  # タブ以外（空き領域）を右クリックした場合は何もしない
            return
        widget = self._tabs.widget(index)
        if widget is None:
            return
        name = widget.name  # 全タブ AnalysisTab なので .name は必ず存在
        menu = QMenu(self)
        close_action = menu.addAction("タブを閉じる")
        close_action.triggered.connect(lambda: self.close_tab(name))
        menu.exec(tab_bar.mapToGlobal(pos))

    def _on_tab_changed(self, idx: int) -> None:
        tab = self._tabs.widget(idx)
        if tab is not None:
            self.statusBar().showMessage(f"Active: {tab.name}")
        self.tab_changed.emit(idx)
