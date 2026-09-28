"""Dataset picker dialog: sortable table + detail pane + filter.

Replaces the old name-only QInputDialog.getItem for File → "Open dataset". Reads
each dataset's materialized `<dataset_dir>/meta.json` (via llm_bridge.dataset_meta)
so it stays fast over a synced drive, and rebuilds stale/heavy metrics in a Qt
background thread.
"""
from __future__ import annotations

import time

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSignalBlocker,
    QSortFilterProxyModel,
    Qt,
    QThread,
    Signal,
)
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from common.i18n import tr
from llm_bridge import dataset_meta
from llm_bridge.dataset_meta import HEAVY_FIELDS, DatasetMeta

# Column layout.
COL_NAME = 0
COL_DESC = 1
COL_COUNT = 2
COL_TOUCHED = 3
COL_AVAIL = 4
_NCOLS = 5

_NEG_INF = float("-inf")


def _fmt_time(epoch, precise: bool = False) -> str:
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return tr("picker.unknown")
    fmt = "%Y-%m-%d %H:%M:%S" if precise else "%Y-%m-%d %H:%M"
    try:
        return time.strftime(fmt, time.localtime(epoch))
    except (ValueError, OSError):
        return tr("picker.unknown")


def _fmt_size(nbytes) -> str:
    if not isinstance(nbytes, (int, float)) or isinstance(nbytes, bool):
        return tr("picker.unknown")
    size = float(nbytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _avail_text(meta) -> str:
    if meta.available:
        return tr("picker.avail.ok")
    return {
        "no-host": tr("picker.avail.no_host"),
        "missing": tr("picker.avail.missing"),
        "bad-config": tr("picker.avail.bad_config"),
    }.get(meta.unavailable_reason, tr("picker.avail.bad_config"))


class DatasetTableModel(QAbstractTableModel):
    """Holds a list[DatasetMeta]; DisplayRole = formatted, UserRole = sort value."""

    def __init__(self, metas, parent=None, open_names=()):
        super().__init__(parent)
        self._metas = list(metas)
        self._open_names = set(open_names)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._metas)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else _NCOLS

    def meta_at(self, row: int) -> DatasetMeta | None:
        if 0 <= row < len(self._metas):
            return self._metas[row]
        return None

    def update_meta(self, meta: DatasetMeta) -> None:
        """Replace the row whose name matches meta.name (by name correlation)."""
        for i, m in enumerate(self._metas):
            if m.name == meta.name:
                self._metas[i] = meta
                top = self.index(i, 0)
                bot = self.index(i, _NCOLS - 1)
                self.dataChanged.emit(top, bot)
                return

    def remove_meta(self, name: str) -> None:
        """Drop the row whose name matches (by name correlation)."""
        for i, m in enumerate(self._metas):
            if m.name == name:
                self.beginRemoveRows(QModelIndex(), i, i)
                del self._metas[i]
                self.endRemoveRows()
                return

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation != Qt.Orientation.Horizontal \
                or role != Qt.ItemDataRole.DisplayRole:
            return None
        return {
            COL_NAME: tr("picker.col.name"),
            COL_DESC: tr("picker.col.description"),
            COL_COUNT: tr("picker.col.analysis_count"),
            COL_TOUCHED: tr("picker.col.last_touched"),
            COL_AVAIL: tr("picker.col.available"),
        }.get(section)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        m = self._metas[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_NAME:
                # Mark datasets already open in this window (Issue #51 B1b).
                if m.name in self._open_names:
                    return f"● {m.name or ''}"
                return m.name or ""
            if col == COL_DESC:
                return m.description or tr("picker.unwritten")
            if col == COL_COUNT:
                return str(m.analysis_count) if m.analysis_count is not None \
                    else tr("picker.unknown")
            if col == COL_TOUCHED:
                return _fmt_time(m.last_touched)
            if col == COL_AVAIL:
                return _avail_text(m)
        elif role == Qt.ItemDataRole.UserRole:
            if col == COL_NAME:
                return m.name or ""
            if col == COL_DESC:
                return (m.description or "").lower()
            if col == COL_COUNT:
                return m.analysis_count if m.analysis_count is not None else -1
            if col == COL_TOUCHED:
                return m.last_touched if m.last_touched is not None else _NEG_INF
            if col == COL_AVAIL:
                return 1 if m.available else 0
        elif role == Qt.ItemDataRole.ForegroundRole:
            if not m.available:
                return QColor(Qt.GlobalColor.gray)
        return None


class DatasetFilterProxyModel(QSortFilterProxyModel):
    """Filters across name + description; sorts by the clicked column's UserRole."""

    def __init__(self, parent=None, completed: bool | None = None):
        super().__init__(parent)
        self._needle = ""
        self._completed = completed   # None = completed フィルタなし(既存互換)

    def set_needle(self, text: str) -> None:
        self._needle = (text or "").lower()
        self.invalidate()

    def filterAcceptsRow(self, source_row, source_parent) -> bool:
        m = self.sourceModel().meta_at(source_row)
        # completed セクション振り分け: needle が空でも必ず評価する。
        # _completed is None(既定 proxy・後方互換)のときは振り分けをスキップ。
        # m is None(範囲外/stale)は completed=False 扱い → 完了=False の proxy だけ
        # accept し、両 proxy が accept して二重表示になるのを防ぐ。
        completed_val = getattr(m, "completed", False) if m is not None else False
        if self._completed is not None and bool(completed_val) != self._completed:
            return False
        if not self._needle:
            return True
        if m is None:
            return True
        hay = f"{m.name or ''}\n{m.description or ''}".lower()
        return self._needle in hay

    def lessThan(self, left, right) -> bool:
        # Compare the clicked column's UserRole raw values. None-safe: the model
        # substitutes float("-inf") for missing last_touched, so ascending sort
        # pushes never-touched rows to the top (descending → bottom).
        lv = left.data(Qt.ItemDataRole.UserRole)
        rv = right.data(Qt.ItemDataRole.UserRole)
        try:
            return lv < rv
        except TypeError:
            return str(lv) < str(rv)


class _MetaBuildWorker(QThread):
    """Background heavy meta rebuild for a set of dataset names."""

    built = Signal(str, object)   # (name, DatasetMeta) — name is the correlation key

    def __init__(self, names, parent):
        super().__init__(parent)
        self._names = list(names)

    def run(self) -> None:
        try:
            for n in self._names:
                if self.isInterruptionRequested():
                    return
                try:
                    dataset_meta.rebuild_meta(
                        n, heavy=True, should_stop=self.isInterruptionRequested)
                    self.built.emit(n, dataset_meta.load_one(n))
                except Exception:
                    continue
        except Exception:
            return


class OpenDatasetDialog(QDialog):
    """Sortable dataset picker. Constructed with the main window so meta workers
    can be registered in its long-lived `_meta_workers` list."""

    def __init__(self, main_window, parent=None):
        super().__init__(parent if parent is not None else main_window)
        self._main_window = main_window
        self.setWindowTitle(tr("picker.title"))
        self.resize(900, 500)

        import config
        try:
            config.reload_datasets()
        except Exception:
            pass  # best-effort; proceed with current DATASETS

        metas = dataset_meta.load_for_picker(sorted(config.DATASETS))
        # Default composite sort: MRU desc → last_touched desc → name asc.
        metas.sort(key=lambda m: (
            -(m.last_opened if m.last_opened is not None else _NEG_INF),
            -(m.last_touched or 0.0),
            m.name or "",
        ))

        open_names = getattr(main_window, "open_dataset_names", lambda: [])()
        self._open_names = set(open_names)
        # dataset name → absolute current_view.png path from a live 更新 re-grab.
        # Takes precedence over the persisted meta.thumbnail so the pane shows the
        # freshly captured on-screen view (see _on_refresh / _render_detail).
        self._live_thumbs: dict[str, str] = {}
        self._model = DatasetTableModel(metas, self, open_names=self._open_names)
        self._proxy = DatasetFilterProxyModel(self, completed=False)
        self._proxy.setSourceModel(self._model)
        self._completed_proxy = DatasetFilterProxyModel(self, completed=True)
        self._completed_proxy.setSourceModel(self._model)

        # --- layout ---
        outer = QVBoxLayout(self)
        self._filter = QLineEdit(self)
        self._filter.setPlaceholderText(tr("picker.filter.placeholder"))
        self._filter.textChanged.connect(self._on_filter_changed)
        outer.addWidget(self._filter)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # Left side: normal list (top) + collapsible completed section (bottom).
        left = QWidget(splitter)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self._view = QTableView(left)
        self._completed_toggle = QToolButton(left)
        self._completed_toggle.setCheckable(True)
        self._completed_toggle.setChecked(False)   # collapsed by default
        self._completed_toggle.setAutoRaise(True)
        self._completed_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self._completed_toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._completed_toggle.toggled.connect(self._on_completed_toggle)
        self._completed_view = QTableView(left)
        self._completed_view.setVisible(False)
        left_layout.addWidget(self._view, 1)
        left_layout.addWidget(self._completed_toggle)
        left_layout.addWidget(self._completed_view, 1)
        self._configure_view(self._view, self._proxy)
        self._configure_view(self._completed_view, self._completed_proxy)
        self._active_view = self._view
        splitter.addWidget(left)

        # Keep the completed header/section in sync with filtered counts.
        for proxy in (self._proxy, self._completed_proxy):
            proxy.rowsInserted.connect(lambda *_: self._sync_completed_section())
            proxy.rowsRemoved.connect(lambda *_: self._sync_completed_section())
            proxy.modelReset.connect(lambda *_: self._sync_completed_section())
            proxy.layoutChanged.connect(lambda *_: self._sync_completed_section())

        # detail pane
        detail = QWidget(splitter)
        detail_layout = QVBoxLayout(detail)
        self._form = QFormLayout()
        detail_layout.addLayout(self._form)
        self._thumb = QLabel(detail)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        detail_layout.addWidget(self._thumb)
        btn_row = QHBoxLayout()
        self._refresh_btn = QPushButton(tr("picker.btn.refresh"), detail)
        self._refresh_btn.clicked.connect(self._on_refresh)
        self._edit_btn = QPushButton(tr("picker.btn.edit_desc"), detail)
        self._edit_btn.clicked.connect(self._on_edit_desc)
        self._complete_btn = QPushButton(tr("picker.btn.mark_completed"), detail)
        self._complete_btn.clicked.connect(self._on_toggle_completed)
        self._delete_btn = QPushButton(tr("picker.btn.delete"), detail)
        self._delete_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(self._refresh_btn)
        btn_row.addWidget(self._edit_btn)
        btn_row.addWidget(self._complete_btn)
        btn_row.addWidget(self._delete_btn)
        detail_layout.addLayout(btn_row)
        detail_layout.addStretch(1)
        splitter.addWidget(detail)
        # Left table keeps its width on window resize; only the right detail
        # pane absorbs the delta. Handle stays draggable for manual adjustment.
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([540, 360])  # sensible initial split (~3:2 at 900px)
        outer.addWidget(splitter, 1)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Open
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self._buttons.accepted.connect(self._accept)
        self._buttons.rejected.connect(self.reject)
        outer.addWidget(self._buttons)
        self._open_btn = self._buttons.button(QDialogButtonBox.StandardButton.Open)

        # Select first row (if any) and render detail. Degenerate case: no
        # normal rows but completed rows exist → auto-expand and select there so
        # the dialog isn't blank.
        self._sync_completed_section()
        if self._proxy.rowCount() > 0:
            self._active_view = self._view
            self._view.selectRow(0)
        elif self._completed_proxy.rowCount() > 0:
            self._completed_toggle.setChecked(True)
            self._active_view = self._completed_view
            self._completed_view.selectRow(0)
        self._on_selection_changed()

        # Background heavy rebuild for stale rows or rows missing any HEAVY field.
        # All HEAVY scans (startup stale rows AND the [更新] button) run on the
        # background worker — never on the GUI thread — so a large synced-drive
        # os.walk can't freeze the dialog.
        self._workers: list[_MetaBuildWorker] = []
        stale_names = [
            m.name for m in metas
            if m.name and (m.uncomputed or any(
                getattr(m, f) is None for f in HEAVY_FIELDS))
        ]
        if stale_names:
            self._start_worker(stale_names)

    # ----- views / completed section -----

    def _configure_view(self, view, proxy) -> None:
        view.setModel(proxy)
        view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        view.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        view.setSortingEnabled(True)
        # Disable the initial sort setSortingEnabled(True) triggers on column 0,
        # so the construction-time composite (MRU) order is preserved until the
        # user clicks a header.
        view.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        view.horizontalHeader().setStretchLastSection(True)
        view.doubleClicked.connect(lambda _idx: self._accept())
        view.selectionModel().selectionChanged.connect(
            lambda *_: self._on_view_selection(view))

    def _on_filter_changed(self, text: str) -> None:
        self._proxy.set_needle(text)
        self._completed_proxy.set_needle(text)
        self._sync_completed_section()
        # needle が完了済みのみにマッチしたときダイアログが空白に見えるのを防ぐ:
        # 上 proxy が 0 行かつ完了 proxy が >0 行なら完了セクションを開いて選択を乗せる。
        if self._proxy.rowCount() == 0 and self._completed_proxy.rowCount() > 0:
            self._completed_toggle.setChecked(True)
            self._active_view = self._completed_view
            self._completed_view.selectRow(0)
        # 現在の選択がフィルタで落ちたら _current_meta()==None → ボタン無効化。
        self._on_selection_changed()

    def _sync_completed_section(self) -> None:
        n = self._completed_proxy.rowCount()   # needle 適用後カウント
        self._completed_toggle.setText(tr("picker.completed.header", n=n))
        # n==0 のときヘッダと view の両方を非表示(ヘッダだけ隠すと展開中に最後の
        # 完了 DS を解除したとき空テーブルが残る)。展開状態は非表示中も保持し、
        # n>0 復帰時に再現。
        self._completed_toggle.setVisible(n > 0)
        self._completed_view.setVisible(n > 0 and self._completed_toggle.isChecked())

    def _on_completed_toggle(self, checked: bool) -> None:
        self._completed_toggle.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
        if not checked and self._active_view is self._completed_view:
            # 畳む: 隠れる完了行が current に残らないよう選択を通常リストへ退避
            with QSignalBlocker(self._completed_view.selectionModel()):
                self._completed_view.clearSelection()
            self._active_view = self._view
            if self._proxy.rowCount() > 0:
                self._view.selectRow(0)           # → _on_view_selection → _on_selection_changed
            else:
                with QSignalBlocker(self._view.selectionModel()):
                    self._view.clearSelection()
                self._on_selection_changed()      # 選択無し → Open/編集/完了ボタンを無効化
        self._sync_completed_section()

    def _on_view_selection(self, view) -> None:
        if not view.selectionModel().selectedRows():
            return                              # clearSelection 由来の空通知は無視
        self._active_view = view
        other = self._completed_view if view is self._view else self._view
        with QSignalBlocker(other.selectionModel()):
            other.clearSelection()
        self._on_selection_changed()

    # ----- worker -----

    def _start_worker(self, names: list[str]) -> None:
        # Qt parent = QApplication.instance() (a guaranteed long-lived QObject).
        # NOT self.window() — a QDialog is its own top-level, so window() returns
        # the dialog (short-lived) and would risk destroy-while-running.
        worker = _MetaBuildWorker(names, QApplication.instance())
        self._workers.append(worker)
        workers = getattr(self._main_window, "_meta_workers", None)
        if workers is not None:
            workers.append(worker)
        worker.built.connect(self._on_meta_built)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(lambda: self._forget_worker(worker))
        worker.start()

    def _forget_worker(self, worker) -> None:
        try:
            self._workers.remove(worker)
        except ValueError:
            pass
        workers = getattr(self._main_window, "_meta_workers", None)
        if workers is not None:
            try:
                workers.remove(worker)
            except ValueError:
                pass

    def _on_meta_built(self, name: str, meta: DatasetMeta) -> None:
        self._model.update_meta(meta)
        cur = self._current_meta()
        if cur is not None and cur.name == name:
            self._render_detail(meta)

    # ----- selection / detail -----

    def _current_meta(self) -> DatasetMeta | None:
        view = self._active_view
        idxs = view.selectionModel().selectedRows()
        if not idxs:
            return None
        src = view.model().mapToSource(idxs[0])
        return self._model.meta_at(src.row())

    def _on_selection_changed(self) -> None:
        m = self._current_meta()
        self._render_detail(m)
        avail = bool(m and m.available)
        if self._open_btn is not None:
            self._open_btn.setEnabled(avail)
        self._edit_btn.setEnabled(avail)
        # Manual organize flag: label reflects the current state; gated like
        # [概要を編集] (unavailable rows can't be toggled).
        self._complete_btn.setText(
            tr("picker.btn.unmark_completed")
            if (m and getattr(m, "completed", False))
            else tr("picker.btn.mark_completed"))
        self._complete_btn.setEnabled(avail)
        # [更新] stays enabled even for unavailable rows (rebuild_meta is a
        # best-effort no-op there). So does [登録を削除] — a row with no path on
        # this host or a vanished directory is exactly what one wants to remove.
        self._refresh_btn.setEnabled(m is not None)
        self._delete_btn.setEnabled(m is not None)

    def _clear_form(self) -> None:
        while self._form.rowCount() > 0:
            self._form.removeRow(0)

    def _render_detail(self, m: DatasetMeta | None) -> None:
        self._clear_form()
        self._thumb.clear()
        if m is None:
            return
        add = self._form.addRow

        def _lbl(text):
            # Word-wrap every detail field so a long value (概要文・host path・解析名
            # 列挙 etc.) wraps inside the detail pane instead of forcing the dialog's
            # minimum width past resize(900, 500) and blowing the window wide.
            lbl = QLabel(text)
            lbl.setWordWrap(True)
            return lbl

        def _txt(v, missing=None):
            return v if v else (missing or tr("picker.unknown"))

        if m.name in self._open_names:
            open_lbl = QLabel(tr("picker.badge.open"))
            open_lbl.setStyleSheet("color:#6ec1e4;font-weight:bold")
            add("", open_lbl)
        if getattr(m, "completed", False):
            completed_lbl = QLabel(tr("picker.badge.completed"))
            completed_lbl.setStyleSheet("color:#6ec1e4;font-weight:bold")
            add("", completed_lbl)
        add(tr("picker.col.description"),
            _lbl(m.description or tr("picker.unwritten")))
        add(tr("picker.detail.format"), _lbl(_txt(m.format)))
        add(tr("picker.detail.host_path"),
            _lbl(m.host_path or _avail_text(m)))
        add(tr("picker.detail.other_hosts"),
            _lbl(", ".join(m.other_hosts) if m.other_hosts else "-"))
        add(tr("picker.detail.analyses"),
            _lbl(", ".join(m.analysis_names) if m.analysis_names
                 else tr("picker.unknown")))
        if m.open_tab_count is not None:
            names = ", ".join(m.open_analysis_names) if m.open_analysis_names else "-"
            add(tr("picker.detail.open_tabs"), _lbl(f"{m.open_tab_count} ({names})"))
        else:
            add(tr("picker.detail.open_tabs"), _lbl(tr("picker.unknown")))
        add(tr("picker.col.last_touched"), _lbl(_fmt_time(m.last_touched, precise=True)))
        add(tr("picker.detail.last_opened"), _lbl(_fmt_time(m.last_opened, precise=True)))
        add(tr("picker.detail.annotations"),
            _lbl(str(m.annotation_total) if m.annotation_total is not None
                 else tr("picker.unknown")))
        add(tr("picker.detail.exports"),
            _lbl(str(m.export_png_count) if m.export_png_count is not None
                 else tr("picker.unknown")))
        add(tr("picker.detail.chats"),
            _lbl(str(m.chat_session_count) if m.chat_session_count is not None
                 else tr("picker.unknown")))
        add(tr("picker.detail.disk_size"), _lbl(_fmt_size(m.disk_size_bytes)))
        add(tr("picker.detail.last_meas"), _lbl(_fmt_time(m.last_measurement)))
        if m.uncomputed:
            add(tr("picker.detail.updated_at"), _lbl(tr("picker.uncomputed")))
        else:
            add(tr("picker.detail.updated_at"), _lbl(_fmt_time(m.updated_at, precise=True)))

        # thumbnail: a live re-grab (更新 on an open dataset) takes precedence over
        # the persisted snapshot so the pane shows the current on-screen view;
        # otherwise fall back to the dataset's stored current_view.png.
        from pathlib import Path
        thumb_path = None
        live = self._live_thumbs.get(m.name)
        if live and Path(live).exists():
            thumb_path = Path(live)
        elif m.thumbnail and m.host_path:
            cand = Path(m.host_path) / m.thumbnail
            if cand.exists():
                thumb_path = cand
        if thumb_path is not None:
            # QImage reads straight from disk; QPixmap(str) goes through
            # QPixmapCache, so a just-regrabbed file is always shown fresh here.
            img = QImage(str(thumb_path))
            if not img.isNull():
                self._thumb.setPixmap(QPixmap.fromImage(img).scaledToWidth(
                    240, Qt.TransformationMode.SmoothTransformation))

    # ----- actions -----

    def _on_refresh(self) -> None:
        # Route the manual refresh through the SAME background worker as startup
        # stale rebuilds. The HEAVY os.walk must never run on the GUI thread —
        # doing it inline here would freeze the dialog on a large synced-drive
        # dataset (Issue #50 §性能・スレッド設計; the row updates via _on_meta_built).
        m = self._current_meta()
        if m is None or not m.name:
            return
        # If the dataset is open, re-grab its live active tab so the thumbnail
        # shows the current on-screen view. The metadata rebuild below only
        # re-reads the persisted current_view.png (possibly stale or absent) and
        # never recaptures it. GUI-thread only; no-op if the dataset isn't open or
        # its active tab isn't a snapshot-capable analysis (e.g. a non-image tab).
        grab = getattr(self._main_window, "snapshot_active_thumbnail", None)
        if grab is not None:
            try:
                png = grab(m.name)
            except Exception:
                png = None
            if png:
                self._live_thumbs[m.name] = png
                self._render_detail(m)   # reflect the fresh grab immediately
        self._start_worker([m.name])

    def _on_edit_desc(self) -> None:
        m = self._current_meta()
        if m is None or not m.name or not m.available:
            return
        text, ok = QInputDialog.getMultiLineText(
            self, tr("picker.desc.dialog.title"), tr("picker.desc.dialog.label"),
            m.description or "")
        if not ok:
            return
        try:
            dataset_meta.patch_description(m.name, text)
        except (KeyError, RuntimeError, OSError, ValueError):
            QMessageBox.warning(
                self, tr("picker.desc.dialog.title"), tr("picker.desc.edit_failed"))
            return
        fresh = dataset_meta.load_one(m.name)
        self._model.update_meta(fresh)
        self._render_detail(fresh)

    def _on_toggle_completed(self) -> None:
        m = self._current_meta()
        if m is None or not m.name or not m.available:
            return
        name = m.name
        current = bool(getattr(m, "completed", False))
        prev_row = None
        if self._active_view is self._view:
            idxs = self._view.selectionModel().selectedRows()
            if idxs:
                prev_row = idxs[0].row()
        try:
            dataset_meta.patch_completed(name, not current)
        except (KeyError, RuntimeError, OSError, ValueError):
            QMessageBox.warning(
                self, self.windowTitle(), tr("picker.completed.save_failed"))
            return
        fresh = dataset_meta.load_one(name)
        self._model.update_meta(fresh)
        # update_meta's dataChanged has no roles so dynamicSortFilter re-evaluates
        # both proxies; invalidate explicitly to avoid re-entrancy/ordering skew.
        # (invalidate() is the file's idiom — see set_needle — and re-applies both
        # filter and sort, whereas invalidateFilter re-runs only the filter; the
        # extra sort re-apply is harmless here and keeps section order consistent.)
        self._proxy.invalidate()
        self._completed_proxy.invalidate()
        self._sync_completed_section()

        if current:
            # 解除(→上へ): 移動先の行を name で探して通常リストで選択。
            row = self._find_proxy_row(self._proxy, name)
            if row is not None:
                self._active_view = self._view
                with QSignalBlocker(self._completed_view.selectionModel()):
                    self._completed_view.clearSelection()
                self._view.selectRow(row)
                self._view.scrollTo(self._proxy.index(row, 0))
            else:
                self._clear_all_selection()
        else:
            # 完了化(→下へ): セクション展開中ならそこで選択。collapsed 中は
            # auto-expand しない — 上リストの最寄り行を選択する。
            if self._completed_toggle.isChecked():
                row = self._find_proxy_row(self._completed_proxy, name)
                if row is not None:
                    self._active_view = self._completed_view
                    with QSignalBlocker(self._view.selectionModel()):
                        self._view.clearSelection()
                    self._completed_view.selectRow(row)
                    self._completed_view.scrollTo(
                        self._completed_proxy.index(row, 0))
                else:
                    self._clear_all_selection()
            else:
                base = prev_row if prev_row is not None else 0
                new_row = min(base, self._proxy.rowCount() - 1)
                if new_row >= 0:
                    self._active_view = self._view
                    with QSignalBlocker(self._completed_view.selectionModel()):
                        self._completed_view.clearSelection()
                    self._view.selectRow(new_row)
                else:
                    self._clear_all_selection()

    def _on_delete(self) -> None:
        # 登録簿（datasets.local.json）からエントリを外すだけ。DS フォルダには触れない。
        m = self._current_meta()
        if m is None or not m.name:
            return
        name = m.name
        is_open = name in self._open_names
        body = tr("picker.delete.confirm", name=name)
        if is_open:
            body += "\n\n" + tr("picker.delete.confirm_open")
        try:
            import config_share
            synced = config_share.is_configured()
        except Exception:
            synced = False
        if synced:
            body += "\n\n" + tr("picker.delete.confirm_sync")
        answer = QMessageBox.question(
            self, tr("picker.delete.title"), body,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return

        # 開いている DS は先に閉じる（セッションを flush してからグループを外す）。
        close = getattr(self._main_window, "close_dataset", None)
        if is_open and close is not None:
            try:
                n = close(name)
            except LookupError:
                n = 0                       # 既に閉じられていた
            if n < 0:
                return                      # flush 失敗 — close_dataset が警告済み

        import config
        try:
            config.unregister_dataset(name)
        except KeyError:
            pass                            # 並行 sync 等で既に登録簿から消えている
        except Exception as e:
            QMessageBox.warning(
                self, tr("picker.delete.title"), tr("picker.delete.failed", error=str(e)))
            return
        config.DATASETS.pop(name, None)
        self._open_names.discard(name)
        self._live_thumbs.pop(name, None)

        view = self._active_view
        idxs = view.selectionModel().selectedRows()
        prev_row = idxs[0].row() if idxs else 0
        self._model.remove_meta(name)
        # 同じリストの同じ位置（末尾なら一つ上）を選び直す。完了リストが空になったら
        # 通常リストの先頭へ。どちらも無ければ選択を外してボタンを無効化する。
        n = view.model().rowCount()
        if n > 0:
            view.selectRow(min(prev_row, n - 1))
        elif view is self._completed_view and self._proxy.rowCount() > 0:
            self._active_view = self._view
            self._view.selectRow(0)
        else:
            self._clear_all_selection()
            return
        self._on_selection_changed()   # selectRow が変化を通知しない場合も詳細を更新

    def _find_proxy_row(self, proxy, name: str) -> int | None:
        for r in range(proxy.rowCount()):
            src = proxy.mapToSource(proxy.index(r, 0))
            m = self._model.meta_at(src.row())
            if m is not None and m.name == name:
                return r
        return None

    def _clear_all_selection(self) -> None:
        with QSignalBlocker(self._view.selectionModel()):
            self._view.clearSelection()
        with QSignalBlocker(self._completed_view.selectionModel()):
            self._completed_view.clearSelection()
        self._active_view = self._view
        self._on_selection_changed()

    def selected_dataset(self) -> str | None:
        m = self._current_meta()
        return m.name if m is not None else None

    def _accept(self) -> None:
        m = self._current_meta()
        if m is None or not m.available:
            return
        self.accept()

    # ----- lifecycle: interrupt workers but never destroy them (Qt parent owns them) -----

    def _interrupt_workers(self) -> None:
        for worker in list(self._workers):
            worker.requestInterruption()

    def reject(self) -> None:
        self._interrupt_workers()
        super().reject()

    def accept(self) -> None:
        self._interrupt_workers()
        super().accept()

    def closeEvent(self, event) -> None:
        self._interrupt_workers()
        super().closeEvent(event)
