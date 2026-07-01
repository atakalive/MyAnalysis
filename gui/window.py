from collections.abc import Callable

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QDialog,
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

from common import i18n
from common.i18n import tr
from gui.tab import AnalysisTab
from gui.tabbar import MultiRowTabBar

RESERVED_WINDOW_VERBS = frozenset([
    "add-tab", "close-tab", "list-tabs", "open-dataset",
    "set-active-tab", "show", "toggle-chat-float",
])


class ToolWindow(QMainWindow):
    tab_changed = Signal(int)
    dataset_changed = Signal(object)  # str | None

    def __init__(self, parent=None):
        super().__init__(parent)
        # Object names let saveState()/restoreState() match docks across a
        # blue-green rebuild (Tier 3) and a restart (Tier 4).
        self.setObjectName("ToolWindow")
        self.setWindowTitle("MyAnalysis Tool")
        self.resize(1400, 800)
        self._current_dataset: str | None = None

        self._tabs = QTabWidget()
        self._tabs.setTabBar(MultiRowTabBar())   # 多段（N段）タブ：横スクロール廃止
        self._tabs.setMovable(True)
        self.setCentralWidget(self._tabs)
        self._tabs.currentChanged.connect(self._on_tab_changed)
        tab_bar = self._tabs.tabBar()
        tab_bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tab_bar.customContextMenuRequested.connect(self._on_tab_context_menu)
        tab_bar.tabMoved.connect(lambda *_: self.mark_session_dirty())
        self.statusBar()

        self._chat_dock = QDockWidget(tr("dock.chat"), self)
        self._chat_dock.setObjectName("ChatDock")
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
        self._chat_widget = None

        self._command_handlers: dict[str, Callable[..., object]] = {}

        # Long-lived registry keeping dataset-picker meta-build workers alive
        # while they run (belt-and-braces with their QApplication Qt parent).
        self._meta_workers: list = []

        self._session_dirty = False
        self._suppress_dirty = False
        self._session_saver: Callable[[], object] | None = None

        # Retranslate hooks let menus built outside this class (e.g. the 開発
        # menu installed by devtools.qt_integration) re-translate on language
        # switch without window.py importing devtools (keeps devtools→gui).
        self._retranslate_hooks: list[Callable[[], None]] = []

        self._file_menu = self.menuBar().addMenu(tr("menu.file"))
        self._register_action = self._file_menu.addAction(tr("menu.file.register"))
        self._register_action.triggered.connect(self._register_dataset)
        self._open_dataset_action = self._file_menu.addAction(tr("menu.file.open_dataset"))
        self._open_dataset_action.triggered.connect(self._open_dataset)
        self._file_menu.addSeparator()
        self._save_session_action = self._file_menu.addAction(tr("menu.file.save_session"))
        self._save_session_action.triggered.connect(self._save_session)
        self._save_quit_action = self._file_menu.addAction(tr("menu.file.save_quit"))
        self._save_quit_action.triggered.connect(self._save_and_quit)
        self._file_menu.addSeparator()
        self._quit_action = self._file_menu.addAction(tr("menu.file.quit"))
        self._quit_action.triggered.connect(self.close)

        self._view_menu = self.menuBar().addMenu(tr("menu.view"))
        self._chat_action = self._view_menu.addAction(tr("menu.view.toggle_chat"))
        self._chat_action.triggered.connect(self.toggle_chat_floating)
        self._meeting_share_action = self._view_menu.addAction(tr("menu.view.meeting_share"))
        self._meeting_share_action.triggered.connect(self._open_meeting_share)

        self._language_menu = self._view_menu.addMenu(tr("menu.view.language"))
        self._language_group = QActionGroup(self)
        self._language_group.setExclusive(True)
        self._language_actions: dict[str, object] = {}
        for code in i18n.available_languages():
            act = self._language_menu.addAction(i18n.language_display_name(code))
            act.setCheckable(True)
            act.setChecked(code == i18n.current_language())
            act.triggered.connect(lambda _checked=False, c=code: self._on_set_language(c))
            self._language_group.addAction(act)
            self._language_actions[code] = act

        self._tool_display_menu = self._view_menu.addMenu(tr("menu.view.tool_display"))
        self._tool_display_group = QActionGroup(self)
        self._tool_display_group.setExclusive(True)
        self._tool_display_actions: dict[str, QAction] = {
            "full": self._tool_display_menu.addAction(tr("menu.view.tool_display.full")),
            "compact": self._tool_display_menu.addAction(tr("menu.view.tool_display.compact")),
            "hidden": self._tool_display_menu.addAction(tr("menu.view.tool_display.hidden")),
        }
        for mode, act in self._tool_display_actions.items():
            act.setCheckable(True)
            act.triggered.connect(
                lambda _checked=False, m=mode: self._set_tool_display_default(m)
            )
            self._tool_display_group.addAction(act)

        self._help_menu = self.menuBar().addMenu(tr("menu.help"))
        self._about_action = self._help_menu.addAction(tr("menu.help.about"))
        self._about_action.triggered.connect(
            lambda: QMessageBox.about(self, "MyAnalysis Tool", "MyAnalysis Tool v0.1")
        )

    def retranslate(self) -> None:
        self._file_menu.setTitle(tr("menu.file"))
        self._open_dataset_action.setText(tr("menu.file.open_dataset"))
        self._register_action.setText(tr("menu.file.register"))
        self._save_session_action.setText(tr("menu.file.save_session"))
        self._save_quit_action.setText(tr("menu.file.save_quit"))
        self._quit_action.setText(tr("menu.file.quit"))
        self._view_menu.setTitle(tr("menu.view"))
        self._chat_action.setText(tr("menu.view.toggle_chat"))
        self._meeting_share_action.setText(tr("menu.view.meeting_share"))
        self._language_menu.setTitle(tr("menu.view.language"))
        self._tool_display_menu.setTitle(tr("menu.view.tool_display"))
        self._tool_display_actions["full"].setText(tr("menu.view.tool_display.full"))
        self._tool_display_actions["compact"].setText(tr("menu.view.tool_display.compact"))
        self._tool_display_actions["hidden"].setText(tr("menu.view.tool_display.hidden"))
        self._help_menu.setTitle(tr("menu.help"))
        self._about_action.setText(tr("menu.help.about"))
        self._chat_dock.setWindowTitle(tr("dock.chat"))
        cur = i18n.current_language()
        for code, act in self._language_actions.items():
            act.setChecked(code == cur)   # autonym は言語非依存なので setText 不要
        cw = self.chat_widget()
        if cw is not None and hasattr(cw, "retranslate"):
            cw.retranslate()
        for hook in self._retranslate_hooks:
            try:
                hook()
            except Exception:  # never-raise: a broken hook must not block others
                pass

    def register_retranslate_hook(self, fn: Callable[[], None]) -> None:
        """Register a callback invoked at the end of retranslate().

        Used by menus built outside ToolWindow (e.g. the devtools 開発 menu) so
        they re-translate on language switch.
        """
        self._retranslate_hooks.append(fn)

    def _on_set_language(self, code: str) -> None:
        i18n.set_language(code)
        self.retranslate()

    def _open_meeting_share(self) -> None:
        """Open the non-modal meeting-share window (Issue #42)."""
        relay = getattr(self, "_meeting_relay", None)
        if relay is None:
            return
        win = getattr(self, "_meeting_share_window", None)
        if win is None:
            from gui.meeting_share import MeetingShareWindow
            win = MeetingShareWindow(self, relay)
            self._meeting_share_window = win
        win.show()
        win.raise_()
        win.activateWindow()

    def _set_tool_display_default(self, mode: str) -> None:
        if self._chat_widget is not None \
                and hasattr(self._chat_widget, "set_tool_display_default"):
            self._chat_widget.set_tool_display_default(mode)

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

    def mark_chat_dirty(self) -> None:
        """Mark dirty for a chat state change, bypassing suppress_dirty.

        Chat adoption / turn completion / rename / delete are real state
        changes, not the restore-echo that suppress_dirty is meant to ignore,
        so they must dirty the session even inside open_dataset's restore loop.
        """
        self._session_dirty = True

    def clear_session_dirty(self) -> None:
        self._session_dirty = False

    def is_session_dirty(self) -> bool:
        return self._session_dirty

    def set_suppress_dirty(self, b: bool) -> None:
        self._suppress_dirty = b

    def set_chat_widget(self, widget: QWidget) -> None:
        self._chat_dock.setWidget(widget)
        self._chat_widget = widget
        if hasattr(widget, "bind_window"):
            widget.bind_window(self)
        # Push the initial dataset (the active tab's dataset, if any).
        if hasattr(widget, "set_current_dataset"):
            widget.set_current_dataset(self._active_tab_dataset())
        # Sync the View-menu tool-display radio to the widget's current default.
        if hasattr(widget, "tool_display_default"):
            cur = widget.tool_display_default()
            act = self._tool_display_actions.get(cur)
            if act is not None:
                act.setChecked(True)

    def chat_widget(self):
        return self._chat_widget

    def chat_sessions(self) -> list:
        if self._chat_widget is None:
            return []
        return self._chat_widget.sessions_for_persistence()

    def chat_deleted_sessions(self) -> list:
        if self._chat_widget is None:
            return []
        return self._chat_widget.deleted_sessions()

    def chat_clear_deleted(self, applied) -> None:
        if self._chat_widget is not None:
            self._chat_widget.clear_deleted(applied)

    def current_chat_dataset(self) -> str | None:
        return self._active_tab_dataset()

    def notify_chat_dataset(self) -> None:
        self._sync_current_from_active_tab()
        if self._chat_widget is not None:
            self._chat_widget.set_current_dataset(self._active_tab_dataset())

    def _active_tab_dataset(self) -> str | None:
        tab = self.active_tab()
        if tab is None:
            return None
        spec = getattr(tab, "session_spec", None)
        return spec.get("dataset") if isinstance(spec, dict) else None

    @property
    def current_dataset(self) -> str | None:
        """The currently open dataset (sticky — survives dataset-less tab switches)."""
        return self._current_dataset

    def _set_current_dataset(self, ds: str | None) -> None:
        if ds != self._current_dataset:
            self._current_dataset = ds
            self.dataset_changed.emit(ds)

    def _sync_current_from_active_tab(self) -> None:
        ds = self._active_tab_dataset()
        if ds is not None:
            self._set_current_dataset(ds)

    def note_current_dataset(self, name: str | None) -> None:
        """Set the current dataset and push to the chat widget.

        Unlike ``_set_current_dataset``, the chat push runs even when *name*
        equals the current value — a same-dataset re-open must still trigger
        the chat's adoption machinery.

        Not to be confused with ``llm_bridge.session.note_dataset``, which
        records a dataset in the save-target set (_touched) but does not
        touch the window or chat widget.
        """
        self._set_current_dataset(name)
        if self._chat_widget is not None:
            self._chat_widget.set_current_dataset(name)

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

    def _open_dataset(self) -> None:
        import config
        try:
            config.reload_datasets()
        except Exception:
            pass  # best-effort; proceed with current DATASETS
        names = sorted(config.DATASETS.keys())
        if not names:
            QMessageBox.information(
                self, tr("dlg.open_dataset.title"), tr("dlg.open_dataset.empty")
            )
            return
        from gui.open_dataset_dialog import OpenDatasetDialog
        dlg = OpenDatasetDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.selected_dataset()
        if not name:
            return
        if not self.has_command("open-dataset"):
            QMessageBox.critical(self, tr("err.generic.title"), tr("err.no_open_dataset"))
            return
        try:
            self.dispatch_command("open-dataset", name=name)
        except Exception as e:
            QMessageBox.critical(self, tr("err.open_dataset.title"), str(e))

    def _save_session(self) -> None:
        if self._session_saver is None:
            QMessageBox.information(
                self, tr("dlg.save_session.title"), tr("err.no_saver")
            )
            return
        try:
            result = self._session_saver()
        except Exception as e:
            QMessageBox.critical(self, tr("err.save.title"), str(e))
            return
        saved, failed = result
        if failed:
            QMessageBox.warning(
                self,
                tr("err.save_partial.title"),
                tr("err.save_partial.body", datasets="\n".join(failed)),
            )

    def _save_and_quit(self) -> None:
        if self._session_saver is None:
            QMessageBox.critical(self, tr("err.save.title"), tr("err.no_saver"))
            return
        try:
            result = self._session_saver()
        except Exception as e:
            QMessageBox.critical(self, tr("err.save.title"), str(e))
            return
        saved, failed = result
        if failed:
            QMessageBox.warning(
                self,
                tr("err.save_partial.title"),
                tr("err.save_partial.body", datasets="\n".join(failed)),
            )
            return
        self.close()

    def _stop_meeting_relay(self) -> None:
        """Stop the meeting relay (idempotent). Called only on closeEvent accept
        paths — not on ignore/Cancel/save-failure, where the window stays open."""
        relay = getattr(self, "_meeting_relay", None)
        if relay is not None:
            relay.stop()

    def closeEvent(self, event) -> None:
        if not self._session_dirty or self._session_saver is None:
            self._stop_meeting_relay()
            event.accept()
            return
        reply = QMessageBox.question(
            self,
            tr("dlg.unsaved.title"),
            tr("dlg.unsaved.body"),
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                result = self._session_saver()
            except Exception as e:
                QMessageBox.critical(self, tr("err.save.title"), str(e))
                event.ignore()
                return
            saved, failed = result
            if failed:
                QMessageBox.warning(
                    self,
                    tr("err.save_partial.title"),
                    tr("err.save_partial.body", datasets="\n".join(failed)),
                )
                event.ignore()
                return
            self._stop_meeting_relay()
            event.accept()
        elif reply == QMessageBox.StandardButton.No:
            self._stop_meeting_relay()
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
        close_action = menu.addAction(tr("menu.tab.close"))
        close_action.triggered.connect(lambda: self.close_tab(name))
        menu.exec(tab_bar.mapToGlobal(pos))

    def _register_dataset(self) -> None:
        import socket

        import config
        from common.paths import validate_identifier_name
        from newanalysis.__main__ import create_analysis

        name, ok = QInputDialog.getText(
            self, tr("dlg.register.title"), tr("dlg.register.label")
        )
        if not ok or not name:
            return
        try:
            validate_identifier_name(name, check_reserved=False)
        except ValueError as e:
            QMessageBox.critical(self, tr("err.register.title"), str(e))
            return

        path = QFileDialog.getExistingDirectory(self, tr("dlg.register_dir.title"))
        if not path:
            return

        try:
            config.register_dataset(name, path)
        except Exception as e:
            QMessageBox.critical(self, tr("err.register.title"), str(e))
            return

        # 成功時のみメモリ poke: 直後に解析タブを追加しても get_dataset_dir が成功する。
        host = socket.gethostname().upper()
        config.DATASETS.setdefault(name, {})[host] = path

        # Sticky dataset + chat push. GUI 登録はセッション復元しない — ユーザは
        # File → データセットを開く… で明示的に復元できる。
        self.note_current_dataset(name)

        reply = QMessageBox.question(
            self, tr("dlg.create_template.title"), tr("dlg.create_template.body")
        )
        if reply == QMessageBox.StandardButton.Yes:
            analysis_name, ok = QInputDialog.getText(
                self, tr("dlg.create_template.title"),
                tr("dlg.create_template.label"), text=name
            )
            if ok and analysis_name:
                try:
                    create_analysis(analysis_name, dataset=name)
                except (ValueError, FileExistsError, KeyError, RuntimeError) as e:
                    QMessageBox.warning(
                        self,
                        tr("err.template_failed.title"),
                        tr("register.template_failed", name=name, error=e),
                    )
                    return
                try:
                    from llm_bridge import dataset_meta
                    dataset_meta.rebuild_meta(name, heavy=False)
                except Exception:
                    pass
                QMessageBox.information(
                    self,
                    tr("dlg.register_done.title"),
                    tr("register.done_with_template",
                       name=name, analysis=analysis_name),
                )
                return

        QMessageBox.information(
            self, tr("dlg.register_done.title"), tr("register.done", name=name)
        )

    def _on_tab_changed(self, idx: int) -> None:
        tab = self._tabs.widget(idx)
        if tab is not None:
            self.statusBar().showMessage(tr("status.active", name=tab.name))
        self.tab_changed.emit(idx)
        self.mark_session_dirty()
        self._sync_current_from_active_tab()
        if self._chat_widget is not None:
            self._chat_widget.set_current_dataset(self._active_tab_dataset())
