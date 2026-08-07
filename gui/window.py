import logging
from collections.abc import Callable

from PySide6.QtCore import QPoint, Qt, QThread, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from common import i18n
from common.i18n import tr
from gui.floating_window import FloatingTabWindow
from gui.tab import AnalysisTab
from gui.tabbar import MultiRowTabBar

_log = logging.getLogger(__name__)


# フロート中タブの位置スタブ（無効タブ）ラベルに付ける識別記号。通常タブと一目で
# 区別するため名前の前に置く（バーが狭くて右省略されても記号は残る）。#56
_FLOAT_STUB_MARK = "↗"


def _float_stub_label(name: str) -> str:
    return f"{_FLOAT_STUB_MARK} {name}"


class _FloatStub(QWidget):
    """Inert placeholder holding a floated tab's name + tab-bar slot (#56).

    While a tab lives in its FloatingTabWindow, a disabled stub keeps its
    original position and label in the bar so re-docking can restore the exact
    slot (the stub is a real tab, so it moves with any reordering). It is a
    *pure UI marker*: filtered out of the group's find/names/widgets enumeration
    (the real tab is enumerated via ``_floated``), never selectable
    (``setTabEnabled(False)``), and destroyed on re-dock / close.
    """

    is_float_stub = True

    def __init__(self, name: str):
        super().__init__()
        self.name = name


def _is_stub(w: QWidget | None) -> bool:
    return bool(getattr(w, "is_float_stub", False))


class _DatasetGroup(QWidget):
    """One page of the dataset stack: a QTabWidget holding one dataset's tabs.

    ``name`` is the dataset this group represents (``None`` for the dataset-less
    group that holds the ``(empty)`` placeholder / inferred viewers). Tab IDs are
    unique *within a group*, so two datasets may each hold a same-named tab.
    """

    def __init__(self, name: str | None, parent=None):
        super().__init__(parent)
        self.name = name
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setTabBar(MultiRowTabBar())   # 多段（N段）タブ：横スクロール廃止
        self.tabs.setMovable(True)
        layout.addWidget(self.tabs)
        # フロート中の解析タブ（親を切り離した独立窓に居るが、概念上はこのグループの
        # メンバー。列挙メソッドは self.tabs のページと _floated の和を返す）。#56
        self._floated: dict[str, AnalysisTab] = {}
        # フロート中タブの「元位置」を保持する無効スタブ（name → _FloatStub）。
        # バー上の位置・名前だけを持ち、列挙では除外される（#56 位置復元）。
        self._float_stubs: dict[str, _FloatStub] = {}

    def find(self, name: str) -> tuple[int, QWidget | None]:
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if _is_stub(w):
                continue  # 位置スタブは列挙対象外（実タブは _floated 側で数える）
            if getattr(w, "name", None) == name:
                return i, w
        floated = getattr(self, "_floated", {})
        if name in floated:
            return -1, floated[name]
        return -1, None

    def names(self) -> list[str]:
        return (
            [self.tabs.widget(i).name for i in range(self.tabs.count())
             if not _is_stub(self.tabs.widget(i))]
            + [t.name for t in getattr(self, "_floated", {}).values()]
        )

    def widgets(self) -> list[QWidget]:
        return (
            [self.tabs.widget(i) for i in range(self.tabs.count())
             if not _is_stub(self.tabs.widget(i))]
            + list(getattr(self, "_floated", {}).values())
        )


class DatasetSwitcher(MultiRowTabBar):
    """Top-level bar: one tab per open dataset. Selecting one switches the stack.

    Reuses ``MultiRowTabBar`` verbatim with tab dragging enabled — datasets are
    reorderable by drag, and the resulting order is synced into ``_groups`` and
    persisted to last_window.json (Issue #59). The drag path is gated on
    ``isMovable()``.
    """

    def __init__(self, parent=None):
        super().__init__(parent, compact_width_hint=True)
        self.setMovable(True)


class ToolWindow(QMainWindow):
    tab_changed = Signal(int)
    dataset_changed = Signal(object)  # str | None
    open_datasets_changed = Signal()  # open-dataset set / active membership changed
    write_failed = Signal(dict)       # common.paths の書込検証失敗 (Issue #96)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Object names let saveState()/restoreState() match docks across a
        # blue-green rebuild (Tier 3) and a restart (Tier 4).
        self.setObjectName("ToolWindow")
        self.setWindowTitle("MyAnalysis Tool")
        self.resize(1400, 800)
        self._current_dataset: str | None = None

        # Two-layer central widget: the DatasetSwitcher (top) selects a page in
        # the QStackedWidget, each page a _DatasetGroup with its own tab bar.
        # `_groups` is the workspace registry (display order — reflects DS-tab
        # drag reorder, persisted to last_window.json, Issue #59) and the single
        # source of truth for "which datasets are open" — NOT the tabs, since a
        # dataset may be open with zero tabs (no-session / restored:0).
        self._groups: dict[str | None, _DatasetGroup] = {}
        self._suppress_switch = False

        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self._switcher = DatasetSwitcher()
        self._switcher.currentChanged.connect(self._on_switcher_changed)
        self._switcher.tabMoved.connect(self._on_dataset_tab_moved)
        self._switcher.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._switcher.customContextMenuRequested.connect(self._on_dataset_context_menu)
        v.addWidget(self._switcher)
        self._stack = QStackedWidget()
        v.addWidget(self._stack, 1)
        self.setCentralWidget(central)
        self._update_switcher_visibility()
        self.statusBar()
        self._install_write_failure_sink()

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
        self._meta_workers: list[QThread] = []

        # Strong refs keep floated windows alive (anti-GC). Keyed by the canonical
        # (grp.name, tab_name). _shutting_down suppresses re-docking during
        # app teardown / Tier reload. #56
        self._float_windows: dict[tuple[str | None, str], FloatingTabWindow] = {}
        self._shutting_down = False

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
        self._restore_session_action = self._file_menu.addAction(
            tr("menu.file.restore_session"))
        self._restore_session_action.triggered.connect(self._restore_last_session)
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

        # 設定 sits between 表示 and ヘルプ; the 開発 menu is appended later by
        # devtools.install_hotreload(), so it stays rightmost.
        self._settings_menu = self.menuBar().addMenu(tr("menu.settings"))
        self._language_menu = self._settings_menu.addMenu(tr("menu.settings.language"))
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

        self._tool_display_menu = self._settings_menu.addMenu(tr("menu.settings.tool_display"))
        self._tool_display_group = QActionGroup(self)
        self._tool_display_group.setExclusive(True)
        self._tool_display_actions: dict[str, QAction] = {
            "full": self._tool_display_menu.addAction(tr("menu.settings.tool_display.full")),
            "compact": self._tool_display_menu.addAction(
                tr("menu.settings.tool_display.compact")),
            "hidden": self._tool_display_menu.addAction(
                tr("menu.settings.tool_display.hidden")),
        }
        for mode, act in self._tool_display_actions.items():
            act.setCheckable(True)
            act.triggered.connect(
                lambda _checked=False, m=mode: self._set_tool_display_default(m)
            )
            self._tool_display_group.addAction(act)

        self._settings_menu.addSeparator()

        self._provider_prompt_action = self._settings_menu.addAction(
            tr("menu.settings.provider_prompt")
        )
        self._provider_prompt_action.setCheckable(True)
        from gui.chat import _effective_use_provider_prompt
        self._provider_prompt_action.setChecked(_effective_use_provider_prompt())
        self._provider_prompt_action.triggered.connect(
            lambda checked=False: self._set_use_provider_prompt(checked)
        )

        self._backend_selector_action = self._settings_menu.addAction(
            tr("menu.settings.backend_selector")
        )
        self._backend_selector_action.triggered.connect(self._open_backend_selector)

        self._backend_status_action = self._settings_menu.addAction(
            tr("menu.settings.backend_status")
        )
        self._backend_status_action.triggered.connect(self._open_backend_status)

        self._help_menu = self.menuBar().addMenu(tr("menu.help"))
        self._about_action = self._help_menu.addAction(tr("menu.help.about"))
        self._about_action.triggered.connect(
            lambda: QMessageBox.about(self, "MyAnalysis Tool", "MyAnalysis Tool v0.1")
        )

    def retranslate(self) -> None:
        self._file_menu.setTitle(tr("menu.file"))
        self._open_dataset_action.setText(tr("menu.file.open_dataset"))
        self._restore_session_action.setText(tr("menu.file.restore_session"))
        self._register_action.setText(tr("menu.file.register"))
        self._save_session_action.setText(tr("menu.file.save_session"))
        self._save_quit_action.setText(tr("menu.file.save_quit"))
        self._quit_action.setText(tr("menu.file.quit"))
        self._view_menu.setTitle(tr("menu.view"))
        self._chat_action.setText(tr("menu.view.toggle_chat"))
        self._meeting_share_action.setText(tr("menu.view.meeting_share"))
        self._settings_menu.setTitle(tr("menu.settings"))
        self._language_menu.setTitle(tr("menu.settings.language"))
        self._tool_display_menu.setTitle(tr("menu.settings.tool_display"))
        self._tool_display_actions["full"].setText(tr("menu.settings.tool_display.full"))
        self._tool_display_actions["compact"].setText(tr("menu.settings.tool_display.compact"))
        self._tool_display_actions["hidden"].setText(tr("menu.settings.tool_display.hidden"))
        self._provider_prompt_action.setText(tr("menu.settings.provider_prompt"))
        self._backend_selector_action.setText(tr("menu.settings.backend_selector"))
        self._backend_status_action.setText(tr("menu.settings.backend_status"))
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

    def _open_backend_status(self) -> None:
        """Open the non-modal backend-status window.

        Non-modal on purpose: an npm install takes ~20s and the login flow happens in
        a separate terminal, so the window has to stay usable throughout.
        """
        win = getattr(self, "_backend_status_window", None)
        if win is None:
            from gui.backend_status_window import BackendStatusWindow
            win = BackendStatusWindow(self)
            self._backend_status_window = win
        else:
            win.refresh()
        win.show()
        win.raise_()
        win.activateWindow()

    def _set_tool_display_default(self, mode: str) -> None:
        if self._chat_widget is not None \
                and hasattr(self._chat_widget, "set_tool_display_default"):
            self._chat_widget.set_tool_display_default(mode)

    def _set_use_provider_prompt(self, value: bool) -> None:
        if self._chat_widget is not None \
                and hasattr(self._chat_widget, "set_use_provider_system_prompt"):
            self._chat_widget.set_use_provider_system_prompt(value)

    def _open_backend_selector(self) -> None:
        from gui.backend_selector_dialog import BackendSelectorDialog
        dlg = BackendSelectorDialog(self, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        w = self.chat_widget()
        if w is None:
            return
        applied = w.apply_backend_change()
        if applied:
            self.statusBar().showMessage(tr("backend.applied"), 5000)
        else:
            self.statusBar().showMessage(tr("backend.applied_config_only"), 5000)

    # ------------------------------------------------------------------ #
    # Dataset groups (top-level "open datasets" layer)                   #
    # ------------------------------------------------------------------ #

    def _current_group(self) -> _DatasetGroup | None:
        w = self._stack.currentWidget()
        return w if isinstance(w, _DatasetGroup) else None

    def _update_switcher_visibility(self) -> None:
        # Hide the switcher when ≤1 dataset is open (single-dataset look).
        self._switcher.setVisible(self._switcher.count() > 1)

    # ---- 書込失敗の可視化（Issue #96） ----------------------------------- #

    def _install_write_failure_sink(self) -> None:
        """common.paths の書込検証失敗をステータスバーに出す。

        MountWriteError は `except OSError` を握っている既存経路に呑まれ得るので、
        **例外とは別の経路**で必ずユーザーに見せる。sink は任意のスレッド
        （メタ再構築ワーカー等）から呼ばれるため、Signal 経由で GUI スレッドへ渡す。
        """
        from common import paths as common_paths

        self.write_failed.connect(self._on_write_failed)
        common_paths.set_write_failure_sink(self.write_failed.emit)

    def _on_write_failed(self, payload: dict) -> None:
        import os.path
        name = os.path.basename(str(payload.get("path", ""))) or "?"
        logging.error("write verification failed: %s", payload)
        self.statusBar().showMessage(tr("status.write_failed", name=name), 30000)

    def _ensure_group(self, ds: str | None) -> _DatasetGroup:
        """Return the group for *ds*, creating (and registering) it if absent.

        Creating the first REAL dataset group retires the None/(empty) group so a
        zero-tab real group and the (empty) placeholder never coexist (invariant).
        """
        grp = self._groups.get(ds)
        if grp is not None:
            return grp
        if ds is not None and None in self._groups:
            # Retire the None group ONLY when it just holds the (empty)
            # placeholder (or nothing) — a None group carrying real dataset-less
            # viewers must survive alongside the new real group.
            none_names = self._groups[None].names()
            if not none_names or none_names == ["(empty)"]:
                self._remove_group(None)
        grp = _DatasetGroup(ds)
        self._groups[ds] = grp
        self._stack.addWidget(grp)
        grp.tabs.currentChanged.connect(
            lambda _i, g=grp: self._on_tab_changed(g)
        )
        tab_bar = grp.tabs.tabBar()
        tab_bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tab_bar.customContextMenuRequested.connect(
            lambda pos, g=grp: self._on_tab_context_menu(g, pos)
        )
        tab_bar.tabMoved.connect(lambda *_: self.mark_session_dirty())
        tab_bar.set_detachable(True)
        tab_bar.tabDetachRequested.connect(
            lambda name, gp, g=grp: self._on_tab_detach(g, name, gp)
        )
        if ds is not None:
            self._suppress_switch = True
            try:
                idx = self._switcher.addTab(self._format_switcher_label(ds, False))
                self._switcher.setTabData(idx, ds)
            finally:
                self._suppress_switch = False
            self._update_switcher_visibility()
        self.open_datasets_changed.emit()
        return grp

    def _remove_group(self, ds: str | None) -> None:
        grp = self._groups.pop(ds, None)
        if grp is None:
            return
        if ds is not None:
            for i in range(self._switcher.count()):
                if self._switcher.tabData(i) == ds:
                    self._suppress_switch = True
                    try:
                        self._switcher.removeTab(i)
                    finally:
                        self._suppress_switch = False
                    break
            self._update_switcher_visibility()
        self._stack.removeWidget(grp)
        grp.deleteLater()

    def _on_switcher_changed(self, idx: int) -> None:
        if self._suppress_switch or idx < 0:
            return
        ds = self._switcher.tabData(idx)
        self._select_dataset_group(ds)

    def _on_dataset_tab_moved(self, *_) -> None:
        """DS タブのドラッグ並べ替えをワークスペースレジストリ (_groups) に同期する。

        _groups の挿入順が DS 順序の真実ソース（open_dataset_names →
        last_window.json）なので、スイッチャーの視覚順に合わせて再構築する。
        解析タブ並べ替え（window.py:357）と同じく mark_session_dirty() のみで、
        即時のファイル書き込みは行わない（active.json は次に _write_active が
        走ったときに新順序へ更新される）。

        switcher に現れないキー（None グループ＝(empty) プレースホルダや
        dataset-less な inferred viewer。tabData が None のもの含む）は末尾の
        leftover ループで必ず保持する。これを落とすと None グループが _groups
        から消え、その viewer タブが到達不能になる（データ消失）。
        """
        order = [self._switcher.tabData(i) for i in range(self._switcher.count())]
        new_groups = {
            ds: self._groups[ds]
            for ds in order
            if ds in self._groups and ds is not None
        }
        for k, g in self._groups.items():
            if k not in new_groups:
                new_groups[k] = g
        self._groups = new_groups
        self.mark_session_dirty()

    def _select_dataset_group(self, ds: str | None) -> None:
        """Bring dataset *ds*'s group to the front and sync current/chat/status.

        Single switch path (switcher click, restore, reload, set_active_dataset).
        Resolves by name key — the switcher index and stack page index diverge
        because the None group has no switcher tab.
        """
        grp = self._groups.get(ds)
        if grp is None:
            return
        self._suppress_switch = True
        try:
            self._stack.setCurrentWidget(grp)
            if ds is not None:
                for i in range(self._switcher.count()):
                    if self._switcher.tabData(i) == ds:
                        self._switcher.setCurrentIndex(i)
                        break
        finally:
            self._suppress_switch = False
        self._set_current_dataset(ds)
        # Chat push runs unconditionally (even when ds is unchanged) so a
        # same-dataset re-select still drives the chat's adoption machinery.
        if self._chat_widget is not None:
            self._chat_widget.set_current_dataset(ds)
        self._refresh_switcher_badges()
        self._update_status_for_active()

    def _update_status_for_active(self) -> None:
        tab = self.active_tab()
        if tab is not None:
            self.statusBar().showMessage(tr("status.active", name=tab.name))

    def refresh_dataset_badges(self) -> None:
        """Public hook for the chat widget to refresh switcher ● on turn state
        change (a turn starting/ending in a hidden dataset; Issue #51 P2-2)."""
        self._refresh_switcher_badges()

    def _refresh_switcher_badges(self) -> None:
        """Prefix a ● to any switcher tab whose dataset has an in-flight chat turn."""
        cw = self._chat_widget
        for i in range(self._switcher.count()):
            ds = self._switcher.tabData(i)
            busy = False
            if cw is not None and hasattr(cw, "dataset_busy"):
                try:
                    busy = cw.dataset_busy(ds)
                except Exception:
                    busy = False
            self._switcher.setTabText(i, self._format_switcher_label(ds, busy))

    @staticmethod
    def _format_switcher_label(ds: str, busy: bool) -> str:
        """DS 切替タブの表示ラベル。◆ で解析タブと区別、● は in-flight チャット。"""
        return ("● " if busy else "") + "◆ " + str(ds)

    def open_datasets(self) -> list["_DatasetGroup"]:
        """The open real-dataset groups (excludes the None/(empty) group), in display order (reflects drag reorder, persisted to last_window.json)."""
        return [g for ds, g in self._groups.items() if ds is not None]

    def open_dataset_names(self) -> list[str]:
        """Names of open datasets (workspace membership) in display order (reflects drag reorder, persisted to last_window.json).

        Derived from the group registry — NOT from tabs — so a dataset opened
        with zero tabs (no-session / restored:0) still counts as open.
        """
        return [ds for ds in self._groups if ds is not None]

    def open_dataset_keys(self) -> list[str]:
        """Wire keys of ALL open dataset groups in display order, including the
        None/(empty) group as "" (Issue #78 meeting relay). Unlike
        open_dataset_names() (which drops the None group), this coerces None → ""
        and keeps it, so a dataset-less group is still addressable by guests."""
        return ["" if ds is None else ds for ds in self._groups]

    def set_active_dataset(self, name: str) -> bool:
        """Bring dataset *name* to the front. False if it is not open."""
        if name not in self.open_dataset_names():
            return False
        # Select the group FIRST (works even for a zero-tab dataset), then focus
        # an intra-group tab if one exists.
        self._select_dataset_group(name)
        grp = self._groups.get(name)
        if grp is not None and grp.tabs.count() > 0 and grp.tabs.currentIndex() < 0:
            for i in range(grp.tabs.count()):  # 位置スタブは飛ばして最初の実タブへ（#56）
                if not _is_stub(grp.tabs.widget(i)):
                    grp.tabs.setCurrentIndex(i)
                    break
        self.open_datasets_changed.emit()
        return True

    def close_dataset(self, name: str) -> int:
        """Close dataset *name*'s group. Returns closed-tab count (>=0), or -1
        (abort sentinel) if flushing its layout failed.

        Order: flush this dataset's current layout to session.json → forget it in
        session._touched (so save_all can't clobber it with empty tabs) → close
        its tabs under suppressed dirty → drop the group. close ≠ forget:
        session.json / chat are never deleted.
        """
        if name is None or name not in self._groups:
            raise LookupError(f"dataset {name!r} is not open")
        from llm_bridge import session
        grp = self._groups[name]
        # 1. flush the current layout (save_dataset skips an all-tabs-closed
        #    dataset — it must not persist an empty layout).
        try:
            ok = session.save_dataset(self, name)
        except Exception:
            ok = False
        if not ok:
            try:
                QMessageBox.warning(
                    self,
                    tr("err.save.title"),
                    tr("err.close_dataset_save_failed", dataset=name),
                )
            except Exception:
                pass
            return -1
        # 2. forget so the eventual save_all won't overwrite the flushed layout.
        session.forget_dataset(name)
        # 3. close its tabs without raising a spurious "unsaved" dirty flag.
        #    widgets() は実タブ（docked + floated）のみで位置スタブを除外するので、
        #    フロート中タブを二重計上しない正しい閉じ数になる。#56
        n = len(grp.widgets())
        self.set_suppress_dirty(True)
        try:
            # フロート破棄（両台帳。_floated は name キー、_float_windows は (ds,name) キー）
            floated = getattr(grp, "_floated", {})
            for fname in list(floated):
                win = getattr(self, "_float_windows", {}).pop((grp.name, fname), None)
                if win is not None:
                    win.deleteLater()
                tab = floated.pop(fname)
                tab.deleteLater()
            # 残りのバー内タブ（実タブ＋位置スタブ）を破棄。スタブも deleteLater される。
            while grp.tabs.count() > 0:
                w = grp.tabs.widget(0)
                grp.tabs.removeTab(0)
                w.deleteLater()
            was_current = self._current_group() is grp
            self._remove_group(name)
            # 4. re-anchor current if we just closed it.
            if self._current_dataset == name or was_current:
                remaining = self.open_dataset_names()
                if remaining:
                    self._select_dataset_group(remaining[0])
                elif None in self._groups:
                    # A dataset-less group (inferred viewers) survives.
                    self._select_dataset_group(None)
                else:
                    self._reseed_empty_group()
        finally:
            self.set_suppress_dirty(False)
        self.open_datasets_changed.emit()
        return n

    def _reseed_empty_group(self) -> None:
        """Recreate the None/(empty) placeholder after the last dataset closed."""
        from tool import build_placeholder_tab
        tab, _sp, _ah = build_placeholder_tab()
        self.add_tab(tab)          # → _ensure_group(None)
        self._select_dataset_group(None)

    def add_tab(self, tab: AnalysisTab) -> None:
        spec = getattr(tab, "session_spec", None)
        ds = spec.get("dataset") if isinstance(spec, dict) else None
        grp = self._ensure_group(ds)
        _, existing = grp.find(tab.name)
        if existing is not None:
            raise KeyError(f"tab name {tab.name!r} already exists")
        i = grp.tabs.addTab(tab, tab.name)
        grp.tabs.tabBar().setTabData(i, tab.name)   # tear-off identity（#56）
        # 分割 splitter の手動ドラッグ（sizes 変更）を dirty 化＝分割ジオメトリ変更を
        # 保存対象にする。add_tab は同名再追加で上の KeyError に当たり同一タブに 2 度
        # 来ないので 1 接続（再ドックは _dock_tab が insertTab 直呼びで add_tab を通らない）。
        # mark_session_dirty は引数なし・splitterMoved は (pos, index) を渡すため lambda で捨てる。
        # setSizes() は splitterMoved を発火しない＋復元中は suppress_dirty=True で二重に安全。
        sp = getattr(tab, "_splitter", None)
        if sp is not None:
            sp.splitterMoved.connect(lambda *a: self.mark_session_dirty())
        self.mark_session_dirty()

    def float_tab(
        self, name: str, dataset: str | None = None, at: QPoint | None = None
    ) -> bool:
        """Pop *name* out into a standalone FloatingTabWindow (Issue #56).

        The tab stays a member of its group (moves to grp._floated), so it still
        appears in tabs()/tab_names()/find_tab() and is saved/relayed normally.
        Returns False for an absent or placeholder tab (the single gate).
        """
        if not hasattr(self, "_float_windows"):
            self._float_windows = {}
        self._shutting_down = False  # 新規フロート＝teardown 中ではない（latch 自己回復）
        grp = tab = None
        idx = -1
        for g in self._groups_to_search(dataset):
            i, w = g.find(name)
            if w is not None:
                grp, idx, tab = g, i, w
                break
        if tab is None or getattr(tab, "is_placeholder", False):
            return False
        key_ds = grp.name  # 正準キーは解決後の grp.name（呼び出し引数 dataset は使わない）
        if idx < 0:  # find が (-1, tab) を返した＝既にフロート中 → 前面化のみ
            win = getattr(self, "_float_windows", {}).get((key_ds, name))
            if win:
                win.show()
                win.raise_()
                win.activateWindow()
            return True
        if not hasattr(grp, "_floated"):
            grp._floated = {}
        if not hasattr(grp, "_float_stubs"):
            grp._float_stubs = {}
        size = tab.size()
        # 台帳を Qt tabs 操作より先に更新: removeTab は同期的に currentChanged を
        # 発火し _on_tab_changed が再入する。_floated 先行更新で単一メンバーシップ維持。
        grp._floated[tab.name] = tab
        grp.tabs.removeTab(idx)
        tab.setParent(None)
        # 元 index に無効スタブを差し込み、タブ名と位置を保持する（再ドックで元位置へ
        # 復元。スタブは実タブと共にバー内で並べ替わるので位置は頑健に追随する）。
        # ラベルは ↗ 記号付きにして通常タブと視覚的に区別する（tabData は素の name の
        # まま＝同一性は不変）。#56
        stub = _FloatStub(name)
        si = grp.tabs.insertTab(idx, stub, _float_stub_label(name))
        grp.tabs.tabBar().setTabData(si, name)
        grp.tabs.setTabEnabled(si, False)  # 選択不可（名前と位置だけを保持）
        grp._float_stubs[name] = stub
        win = FloatingTabWindow(self, tab, key_ds, name, size)
        if at is not None:
            win.move(at)
        win.show()
        win.raise_()
        win.activateWindow()
        self._float_windows[(key_ds, name)] = win
        self.mark_session_dirty()  # docked タブ集合が変わる（再ドックは元位置へ復元）#56
        return True

    def _on_tab_detach(
        self, grp: "_DatasetGroup", name: str, gpos: QPoint
    ) -> None:
        # tabDetachRequested 受け口（引数は index でなく name）。全経路が float_tab を通る。
        self.float_tab(name, dataset=grp.name, at=gpos)

    def _dock_tab(self, ds: str | None, name: str) -> None:
        """Re-dock a floated tab back into its group at its original slot. Idempotent (#56)."""
        grp = self._groups.get(ds)
        win = getattr(self, "_float_windows", {}).pop((ds, name), None)
        tab = getattr(grp, "_floated", {}).pop(name, None) if grp else None
        if tab is not None:
            # 位置スタブがバーに残っていれば、その現在位置へ実タブを差し込んで元の
            # タブ順序を復元する（フロート中に他タブが増減/並べ替えされてもスタブが
            # 一緒に動くので位置は正しく追随する）。スタブが無ければ末尾へフォールバック。#56
            stub = getattr(grp, "_float_stubs", {}).pop(name, None)
            insert_at = grp.tabs.count()
            if stub is not None:
                si = grp.tabs.indexOf(stub)
                if si >= 0:
                    insert_at = si
                    grp.tabs.removeTab(si)
                stub.deleteLater()
            tab.setParent(None)
            i = grp.tabs.insertTab(insert_at, tab, tab.name)
            grp.tabs.tabBar().setTabData(i, tab.name)
            self.set_active_tab(name, dataset=ds)
        if win is not None:
            win.deleteLater()  # win.setParent(None) は呼ばない（自窓 closeEvent 中の再親付け回避）

    def _close_all_floats(self) -> None:
        """Re-dock every floated tab before teardown/reload/dataset-close."""
        self._shutting_down = True
        for (ds, name) in list(getattr(self, "_float_windows", {})):
            self._dock_tab(ds, name)

    def close_tab(self, name: str, dataset: str | None = None) -> bool:
        """Close a tab. With *dataset* given, only that group is searched;
        otherwise the active group wins, then the first match across groups.
        プレースホルダタブ（is_placeholder=True）は close しない（#54, ゼロタブ窓防止）。
        最後の実タブ除去でウィンドウが空になる場合は再アンカーする（#55, 副作用）:
        None グループが空になり実データセットが開いていればそれへアクティブを切替＋空 None
        グループを除去、実データセットも無ければ (empty) を再生成する。実データセットグループは
        ゼロタブでも有効な開き状態として維持する。"""
        for grp in self._groups_to_search(dataset):
            idx, widget = grp.find(name)
            if widget is not None:
                if getattr(widget, "is_placeholder", False):
                    return False  # プレースホルダは閉じない（システムスタブ）
                floated = getattr(grp, "_floated", {})
                if idx < 0 or name in floated:   # フロート中タブ（#56）
                    win = getattr(self, "_float_windows", {}).pop((grp.name, name), None)
                    if win is not None:
                        win.deleteLater()
                    floated.pop(name, None)
                    # 位置スタブもバーから除去して破棄（#56）
                    stub = getattr(grp, "_float_stubs", {}).pop(name, None)
                    if stub is not None:
                        si = grp.tabs.indexOf(stub)
                        if si >= 0:
                            grp.tabs.removeTab(si)
                        stub.deleteLater()
                else:
                    grp.tabs.removeTab(idx)
                widget.deleteLater()
                self.mark_session_dirty()
                self._reanchor_after_close_tab(grp)  # #55: ゼロタブ空ウィンドウ防止
                return True
        return False

    def _reanchor_after_close_tab(self, grp: "_DatasetGroup") -> None:
        """close_tab がタブを1つ除去した後、ウィンドウがゼロタブ空状態に落ちない
        よう再アンカーする（#55）。close_dataset のステップ4（`# 4. re-anchor
        current` 以下）と同型のロジック。

        - *grp* にまだタブが残る → 何もしない。
        - *grp* が実データセットグループ（grp.name is not None）で空になった
          → 何もしない。ゼロタブの実データセットは有効な開き状態であり（switcher に
          残り register も効く。#51 test_open_dataset_zero_tabs_comes_to_front と同じ
          扱い）、ここで (empty) を再生成すると _ensure_group の不変条件
          「zero-tab real group と (empty) placeholder は共存しない」を破る。
        - *grp* が None グループ（grp.name is None）で空になった:
            - 実データセットが1つ以上開いている（open_dataset_names() が非空）
              → 意味を失った空の None グループを除去し、None グループが前面だった
              場合のみ先頭の実データセットへ再アンカーする。(empty) は再生成しない
              （上記の不変条件を維持）。
            - 実データセットが無い → _reseed_empty_group() で (empty) を再生成し、
              完全な空ウィンドウを防ぐ。
        """
        if grp.tabs.count() > 0 or getattr(grp, "_floated", {}):
            return
        if grp.name is not None:
            return
        was_current = self._current_group() is grp
        remaining = self.open_dataset_names()
        if remaining:
            self._remove_group(None)
            if was_current:
                self._select_dataset_group(remaining[0])
        else:
            self._reseed_empty_group()

    def _groups_to_search(self, dataset: str | None) -> list["_DatasetGroup"]:
        if dataset is not None:
            grp = self._groups.get(dataset)
            return [grp] if grp is not None else []
        cur = self._current_group()
        ordered = [cur] if cur is not None else []
        ordered += [g for g in self._groups.values() if g is not cur]
        return ordered

    def active_tab(self) -> AnalysisTab | None:
        grp = self._current_group()
        if grp is None:
            return None
        idx = grp.tabs.currentIndex()
        if idx < 0:
            return None
        w = grp.tabs.widget(idx)
        return None if _is_stub(w) else w  # 位置スタブが current になる退化ケースを除外（#56）

    def snapshot_active_thumbnail(self, dataset: str) -> str | None:
        """Re-grab *dataset*'s live active tab into its current_view.png and return
        the absolute PNG path (or None).

        Used by the Open-dataset picker's 更新 button so the thumbnail reflects the
        on-screen view rather than a stale/absent persisted snapshot (a metadata
        rebuild re-reads current_view.png but never recaptures it). Returns None
        when the dataset isn't open, its active tab can't snapshot (e.g. a
        non-analysis tab with no writer), or nothing was written. GUI-thread only
        — QWidget.grab() can't run off the main thread."""
        grp = self._groups.get(dataset)
        if grp is None:
            return None
        idx = grp.tabs.currentIndex()
        tab = grp.tabs.widget(idx) if idx >= 0 else None
        name = getattr(tab, "name", None)
        if tab is None or not name or not hasattr(tab, "take_snapshot"):
            return None
        from llm_bridge import snapshots
        try:
            tab.take_snapshot()          # no-op if this tab has no snapshot writer
            p = snapshots.path(dataset, name)
            return str(p) if p.exists() else None
        except Exception:
            return None

    def set_active_tab(self, name: str, dataset: str | None = None) -> bool:
        target_group = None
        target_idx = -1
        for grp in self._groups_to_search(dataset):
            idx, w = grp.find(name)
            if w is not None:
                target_group, target_idx = grp, idx
                break
        if target_group is None:
            return False
        if target_idx < 0:  # フロート中 → 前面グループは変えず窓を前面化（#56）
            win = getattr(self, "_float_windows", {}).get((target_group.name, name))
            if win is not None:
                win.show()
                win.raise_()
                win.activateWindow()
            return True
        # Always select (idempotent) so current_dataset / chat sync even when the
        # group is already the front page — the front stack widget and
        # current_dataset can otherwise diverge (e.g. right after add_tab).
        self._select_dataset_group(target_group.name)
        target_group.tabs.setCurrentIndex(target_idx)
        return True

    def find_tab(self, name: str, dataset: str | None = None):
        """Resolve a tab by (name, dataset) WITHOUT focusing it. Generic over tab
        kinds (analysis / figure / viewer).

        0 matches → None; 1 → that tab; multiple → current_dataset's if present,
        else raise LookupError naming the candidate datasets.
        """
        if dataset is not None:
            grp = self._groups.get(dataset)
            if grp is None:
                return None
            _, w = grp.find(name)
            return w
        matches: list[tuple[str | None, QWidget]] = []
        for ds, grp in self._groups.items():
            _, w = grp.find(name)
            if w is not None:
                matches.append((ds, w))
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0][1]
        for ds, w in matches:
            if ds == self._current_dataset:
                return w
        cands = [ds for ds, _ in matches]
        raise LookupError(
            f"tab {name!r} exists in multiple datasets {cands!r}; "
            f"pass dataset= to disambiguate"
        )

    def tab_names(self) -> list[str]:
        out: list[str] = []
        for grp in self._groups.values():
            out.extend(grp.names())
        return out

    def tabs(self) -> list[AnalysisTab]:
        out: list[AnalysisTab] = []
        for grp in self._groups.values():
            out.extend(grp.widgets())
        return out

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
        # Push the initial dataset (the explicitly-selected current dataset).
        if hasattr(widget, "set_current_dataset"):
            widget.set_current_dataset(self._current_dataset)
        # Sync the Settings-menu tool-display radio to the widget's current default.
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
        return self._current_dataset

    def notify_chat_dataset(self) -> None:
        if self._chat_widget is not None:
            self._chat_widget.set_current_dataset(self._current_dataset)

    def _active_tab_dataset(self) -> str | None:
        tab = self.active_tab()
        if tab is None:
            return None
        spec = getattr(tab, "session_spec", None)
        return spec.get("dataset") if isinstance(spec, dict) else None

    @property
    def current_dataset(self) -> str | None:
        """The explicitly-selected dataset (the front group in the switcher).

        No longer sticky-follows the active tab: it changes only when a dataset
        group is selected (switcher click, open-dataset, set_active_dataset,
        restore/reload). The invariant "the active tab lives in the current
        group" holds, so readers of current_dataset stay consistent.
        """
        return self._current_dataset

    def _set_current_dataset(self, ds: str | None) -> None:
        if ds != self._current_dataset:
            self._current_dataset = ds
            self.dataset_changed.emit(ds)

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
            res = self.dispatch_command("open-dataset", name=name)
        except Exception as e:
            QMessageBox.critical(self, tr("err.open_dataset.title"), str(e))
            return
        if isinstance(res, str) and res.startswith("error:"):
            _log.warning("open-dataset returned %r for %r", res, name)
            QMessageBox.warning(
                self,
                tr("err.open_dataset.title"),
                tr("err.open_dataset.body", dataset=name),
            )
        elif isinstance(res, str) and res.startswith("unreadable-session:"):
            _log.warning("open-dataset returned %r for %r", res, name)
            QMessageBox.warning(
                self,
                tr("err.session_unreadable.title"),
                tr("err.session_unreadable.body", dataset=name),
            )

    def _restore_last_session(self) -> None:
        """Restore the last saved workspace (which datasets were open + active).

        ADDITIVE: already-open datasets/tabs are never closed or overwritten;
        open-dataset just focuses an already-open dataset. Missing file / empty /
        non-list payload → an info message only. Datasets that fail to open are
        skipped, logged, and surfaced in a single warning; the rest still restore.
        Datasets whose session.json is corrupt (`unreadable-session:`) DO open
        (tabs only are missing) and get their own warning.
        """
        from llm_bridge import session
        state = session.read_last_window()
        datasets = state.get("datasets") if isinstance(state, dict) else None
        if not isinstance(datasets, list) or not datasets:
            QMessageBox.information(
                self, tr("dlg.restore_session.title"), tr("dlg.restore_session.empty")
            )
            return
        if not self.has_command("open-dataset"):
            QMessageBox.critical(self, tr("err.generic.title"), tr("err.no_open_dataset"))
            return
        opened: list[str] = []
        failed: list[str] = []
        unreadable: list[str] = []
        for ds in datasets:
            try:
                res = self.dispatch_command("open-dataset", name=ds)
            except Exception:
                _log.exception("restore: open-dataset raised for %r", ds)
                failed.append(str(ds))
                continue
            if isinstance(res, str) and res.startswith("error:"):
                _log.warning("restore: open-dataset returned %r for %r", res, ds)
                failed.append(str(ds))
                continue
            if isinstance(res, str) and res.startswith("unreadable-session:"):
                # DS 自体は開いている（タブだけ未復元）ので opened にも載せる。
                _log.warning("restore: open-dataset returned %r for %r", res, ds)
                unreadable.append(str(ds))
            opened.append(ds)
        active = state.get("active") if isinstance(state, dict) else None
        target = active if active in opened else (opened[0] if opened else None)
        if target is not None:
            try:
                self.set_active_dataset(target)
            except LookupError:
                pass
        if failed:
            QMessageBox.warning(
                self,
                tr("err.restore_partial.title"),
                tr("err.restore_partial.body", datasets="\n".join(failed)),
            )
        if unreadable:
            QMessageBox.warning(
                self,
                tr("err.session_unreadable.title"),
                tr("err.session_unreadable.body", dataset="\n".join(unreadable)),
            )

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
            self._close_all_floats()
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
            self._close_all_floats()
            event.accept()
        elif reply == QMessageBox.StandardButton.No:
            self._stop_meeting_relay()
            self._close_all_floats()
            event.accept()
        else:
            event.ignore()

    def _on_tab_context_menu(self, grp: "_DatasetGroup", pos: QPoint) -> None:
        tab_bar = grp.tabs.tabBar()
        index = tab_bar.tabAt(pos)
        if index < 0:  # タブ以外（空き領域）を右クリックした場合は何もしない
            return
        widget = grp.tabs.widget(index)
        if widget is None or _is_stub(widget):
            return  # 位置スタブ（フロート中タブの元位置ホルダ）はメニューを出さない（#56）
        menu = QMenu(self)
        self._populate_tab_menu(menu, widget, grp.name)
        if not menu.actions():  # プレースホルダ等、項目が無ければ空ポップアップを出さない
            menu.deleteLater()
            return
        menu.exec(tab_bar.mapToGlobal(pos))

    def _on_dataset_context_menu(self, pos: QPoint) -> None:
        index = self._switcher.tabAt(pos)
        if index < 0:                       # タブ以外（空き領域）は無視
            return
        ds = self._switcher.tabData(index)
        if ds is None:                      # 念のため（switcher に None タブは無い）
            return
        menu = QMenu(self)
        self._populate_dataset_menu(menu, ds)
        if not menu.actions():              # 念のための空メニュー抑止（雛形と対称）
            menu.deleteLater()
            return
        menu.exec(self._switcher.mapToGlobal(pos))

    def _populate_dataset_menu(self, menu: QMenu, ds: str) -> None:
        """DS 切替タブ右クリックメニューを構築（exec はしない＝ヘッドレステスト可能）。"""
        close_action = menu.addAction(tr("menu.dataset.close"))
        close_action.triggered.connect(
            lambda: self.close_dataset(ds) if ds in self._groups else None
        )

    def _populate_tab_menu(
        self, menu: QMenu, widget: AnalysisTab, ds: str | None
    ) -> None:
        """タブ右クリックメニューを *menu* に構築する（exec はしない）。

        プレースホルダタブ（is_placeholder=True）はシステムスタブでありユーザー content
        ではないため、メニュー項目を一切出さない（コピー / コメントは無意味、「タブを
        閉じる」はゼロタブ空ウィンドウを生む — #54）。実体タブ（解析 / demo / inferred
        viewer）は従来どおり全項目を出す。exec を分離しているのでヘッドレステスト可能。
        """
        name = widget.name  # 全タブ AnalysisTab なので .name は必ず存在
        # is_placeholder は実 AnalysisTab では常に存在（__init__ で無条件初期化）。
        # duck-typed テスト widget / headless fake は非プレースホルダ扱い（fail-open）。
        if not getattr(widget, "is_placeholder", False):
            copy_action = menu.addAction(tr("menu.tab.copy_name"))
            copy_action.triggered.connect(lambda: QApplication.clipboard().setText(name))
            comment_action = menu.addAction(tr("menu.tab.comment"))
            comment_action.triggered.connect(
                lambda: self._comment_on_tab(name, dataset=ds)
            )
            float_action = menu.addAction(tr("menu.tab.float"))
            float_action.triggered.connect(lambda: self.float_tab(name, dataset=ds))
            menu.addSeparator()
            close_action = menu.addAction(tr("menu.tab.close"))
            close_action.triggered.connect(lambda: self.close_tab(name, dataset=ds))

    def _comment_on_tab(self, name: str, dataset: str | None = None) -> None:
        chat = self.chat_widget()
        if chat is None:
            return
        # マルチDS時はタブ名だけでは一意にならない（#51: タブ ID はグループ内一意）
        # ので、CLI verb が逐語で受け取れる dataset= 表記で修飾する。
        if dataset is not None and len(self.open_dataset_names()) > 1:
            prefix = tr("menu.tab.comment_prefix_ds", name=name, dataset=dataset)
        else:
            prefix = tr("menu.tab.comment_prefix", name=name)
        draft = chat.input_draft()
        if not draft.startswith(prefix):
            chat.set_input_draft(prefix + draft)
        chat.focus_input()

    def _register_dataset(self) -> None:
        import socket

        import config
        from common.paths import validate_identifier_name

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

        QMessageBox.information(
            self, tr("dlg.register_done.title"), tr("register.done", name=name)
        )

    def _on_tab_changed(self, grp: "_DatasetGroup") -> None:
        # Only the visible group's intra-tab change drives status/active.json;
        # a background group's tab churn (e.g. restore adding tabs) must not.
        if grp is not self._current_group():
            return
        tab = self.active_tab()
        if tab is not None:
            self.statusBar().showMessage(tr("status.active", name=tab.name))
        self.tab_changed.emit(grp.tabs.currentIndex())
        self.mark_session_dirty()
        self._refresh_switcher_badges()
