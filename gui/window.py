from collections.abc import Callable

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
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
    "add-tab", "close-tab", "list-tabs", "open-dataset",
    "set-active-tab", "show", "toggle-chat-float",
])


class ToolWindow(QMainWindow):
    tab_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MyAnalysis Tool")
        self.resize(1400, 800)

        self._tabs = QTabWidget()
        self._tabs.setMovable(True)
        self.setCentralWidget(self._tabs)
        self._tabs.currentChanged.connect(self._on_tab_changed)
        tab_bar = self._tabs.tabBar()
        tab_bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tab_bar.customContextMenuRequested.connect(self._on_tab_context_menu)
        tab_bar.tabMoved.connect(lambda *_: self.mark_session_dirty())
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

        self._session_dirty = False
        self._suppress_dirty = False
        self._session_saver: Callable[[], object] | None = None

        file_menu = self.menuBar().addMenu("ファイル(&F)")
        open_action = file_menu.addAction("解析を開く…")
        open_action.triggered.connect(self._open_analysis)
        open_dataset_action = file_menu.addAction("データセットを開く…")
        open_dataset_action.triggered.connect(self._open_dataset)
        register_action = file_menu.addAction("データセット登録…")
        register_action.triggered.connect(self._register_dataset)
        file_menu.addSeparator()
        save_session_action = file_menu.addAction("セッションを保存")
        save_session_action.triggered.connect(self._save_session)
        save_quit_action = file_menu.addAction("保存して終了")
        save_quit_action.triggered.connect(self._save_and_quit)
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
        self.mark_session_dirty()

    def close_tab(self, name: str) -> bool:
        for i in range(self._tabs.count()):
            if self._tabs.widget(i).name == name:
                widget = self._tabs.widget(i)
                self._tabs.removeTab(i)
                widget.deleteLater()
                self.mark_session_dirty()
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

    def tabs(self) -> list[AnalysisTab]:
        return [self._tabs.widget(i) for i in range(self._tabs.count())]

    def set_session_saver(self, fn: Callable[[], object] | None) -> None:
        self._session_saver = fn

    def mark_session_dirty(self) -> None:
        if self._suppress_dirty:
            return
        self._session_dirty = True

    def clear_session_dirty(self) -> None:
        self._session_dirty = False

    def is_session_dirty(self) -> bool:
        return self._session_dirty

    def set_suppress_dirty(self, b: bool) -> None:
        self._suppress_dirty = b

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

    def _open_dataset(self) -> None:
        import config
        names = sorted(config.DATASETS.keys())
        if not names:
            QMessageBox.information(self, "データセットを開く", "データセットが登録されていません")
            return
        name, ok = QInputDialog.getItem(
            self, "データセットを開く", "データセットを選択:", names, 0, False
        )
        if not ok or not name:
            return
        if not self.has_command("open-dataset"):
            QMessageBox.critical(self, "エラー", "open-dataset コマンドが未登録です")
            return
        try:
            self.dispatch_command("open-dataset", name=name)
        except Exception as e:
            QMessageBox.critical(self, "データセットを開けません", str(e))

    def _save_session(self) -> None:
        if self._session_saver is None:
            QMessageBox.information(self, "セッションを保存", "保存機構が未配線です")
            return
        try:
            result = self._session_saver()
        except Exception as e:
            QMessageBox.critical(self, "保存エラー", str(e))
            return
        saved, failed = result
        if failed:
            QMessageBox.warning(
                self,
                "一部のデータセットを保存できません",
                "保存に失敗したデータセット:\n" + "\n".join(failed),
            )

    def _save_and_quit(self) -> None:
        if self._session_saver is None:
            QMessageBox.critical(self, "保存エラー", "保存機構が未配線です")
            return
        try:
            result = self._session_saver()
        except Exception as e:
            QMessageBox.critical(self, "保存エラー", str(e))
            return
        saved, failed = result
        if failed:
            QMessageBox.warning(
                self,
                "一部のデータセットを保存できません",
                "保存に失敗したデータセット:\n" + "\n".join(failed),
            )
            return
        self.close()

    def closeEvent(self, event) -> None:
        if not self._session_dirty or self._session_saver is None:
            event.accept()
            return
        reply = QMessageBox.question(
            self,
            "セッション未保存",
            "セッションを保存しますか？",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                result = self._session_saver()
            except Exception as e:
                QMessageBox.critical(self, "保存エラー", str(e))
                event.ignore()
                return
            saved, failed = result
            if failed:
                QMessageBox.warning(
                    self,
                    "一部のデータセットを保存できません",
                    "保存に失敗したデータセット:\n" + "\n".join(failed),
                )
                event.ignore()
                return
            event.accept()
        elif reply == QMessageBox.StandardButton.No:
            event.accept()
        else:
            event.ignore()

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

    def _register_dataset(self) -> None:
        import socket

        import config
        from common.paths import validate_identifier_name
        from newanalysis.__main__ import create_analysis

        name, ok = QInputDialog.getText(self, "データセット登録", "データセット名:")
        if not ok or not name:
            return
        try:
            validate_identifier_name(name, check_reserved=False)
        except ValueError as e:
            QMessageBox.critical(self, "登録エラー", str(e))
            return

        path = QFileDialog.getExistingDirectory(self, "データセットディレクトリを選択")
        if not path:
            return

        try:
            config.register_dataset(name, path)
        except Exception as e:
            QMessageBox.critical(self, "登録エラー", str(e))
            return

        # 成功時のみメモリ poke: 直後に「解析を開く」しても get_dataset_dir が成功する。
        host = socket.gethostname().upper()
        config.DATASETS.setdefault(name, {})[host] = path

        reply = QMessageBox.question(
            self, "解析雛形の作成", "解析雛形も作成しますか?"
        )
        if reply == QMessageBox.StandardButton.Yes:
            analysis_name, ok = QInputDialog.getText(
                self, "解析雛形の作成", "解析名:", text=name
            )
            if ok and analysis_name:
                try:
                    create_analysis(analysis_name, dataset=name)
                except (ValueError, FileExistsError) as e:
                    QMessageBox.warning(
                        self,
                        "解析雛形の作成に失敗",
                        f"データセット '{name}' の登録は完了しました。\n"
                        f"解析雛形の作成に失敗しました: {e}",
                    )
                    return
                QMessageBox.information(
                    self,
                    "登録完了",
                    f"データセット '{name}' を登録し、"
                    f"解析雛形 '{analysis_name}' を作成しました。",
                )
                return

        QMessageBox.information(
            self, "登録完了", f"データセット '{name}' を登録しました。"
        )

    def _on_tab_changed(self, idx: int) -> None:
        tab = self._tabs.widget(idx)
        if tab is not None:
            self.statusBar().showMessage(f"Active: {tab.name}")
        self.tab_changed.emit(idx)
        self.mark_session_dirty()
