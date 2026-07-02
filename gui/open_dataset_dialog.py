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
    QSortFilterProxyModel,
    Qt,
    QThread,
    Signal,
)
from PySide6.QtGui import QColor, QPixmap
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self._needle = ""

    def set_needle(self, text: str) -> None:
        self._needle = (text or "").lower()
        self.invalidate()

    def filterAcceptsRow(self, source_row, source_parent) -> bool:
        if not self._needle:
            return True
        model = self.sourceModel()
        m = model.meta_at(source_row)
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
        self._model = DatasetTableModel(metas, self, open_names=self._open_names)
        self._proxy = DatasetFilterProxyModel(self)
        self._proxy.setSourceModel(self._model)

        # --- layout ---
        outer = QVBoxLayout(self)
        self._filter = QLineEdit(self)
        self._filter.setPlaceholderText(tr("picker.filter.placeholder"))
        self._filter.textChanged.connect(self._proxy.set_needle)
        outer.addWidget(self._filter)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._view = QTableView(splitter)
        self._view.setModel(self._proxy)
        self._view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._view.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self._view.setSortingEnabled(True)
        # Disable the initial sort setSortingEnabled(True) triggers on column 0,
        # so the construction-time composite (MRU) order is preserved until the
        # user clicks a header.
        self._view.horizontalHeader().setSortIndicator(
            -1, Qt.SortOrder.AscendingOrder)
        self._view.horizontalHeader().setStretchLastSection(True)
        self._view.doubleClicked.connect(lambda _idx: self._accept())
        self._view.selectionModel().selectionChanged.connect(
            lambda *_: self._on_selection_changed())
        splitter.addWidget(self._view)

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
        btn_row.addWidget(self._refresh_btn)
        btn_row.addWidget(self._edit_btn)
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

        # Select first row (if any) and render detail.
        if self._proxy.rowCount() > 0:
            self._view.selectRow(0)
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
        idxs = self._view.selectionModel().selectedRows()
        if not idxs:
            return None
        src = self._proxy.mapToSource(idxs[0])
        return self._model.meta_at(src.row())

    def _on_selection_changed(self) -> None:
        m = self._current_meta()
        self._render_detail(m)
        avail = bool(m and m.available)
        if self._open_btn is not None:
            self._open_btn.setEnabled(avail)
        self._edit_btn.setEnabled(avail)
        # [更新] stays enabled even for unavailable rows (rebuild_meta is a
        # best-effort no-op there).
        self._refresh_btn.setEnabled(m is not None)

    def _clear_form(self) -> None:
        while self._form.rowCount() > 0:
            self._form.removeRow(0)

    def _render_detail(self, m: DatasetMeta | None) -> None:
        self._clear_form()
        self._thumb.clear()
        if m is None:
            return
        add = self._form.addRow

        def _txt(v, missing=None):
            return v if v else (missing or tr("picker.unknown"))

        if m.name in self._open_names:
            open_lbl = QLabel(tr("picker.badge.open"))
            open_lbl.setStyleSheet("color:#6ec1e4;font-weight:bold")
            add("", open_lbl)
        add(tr("picker.col.description"),
            QLabel(m.description or tr("picker.unwritten")))
        add(tr("picker.detail.format"), QLabel(_txt(m.format)))
        add(tr("picker.detail.host_path"),
            QLabel(m.host_path or _avail_text(m)))
        add(tr("picker.detail.other_hosts"),
            QLabel(", ".join(m.other_hosts) if m.other_hosts else "-"))
        add(tr("picker.detail.analyses"),
            QLabel(", ".join(m.analysis_names) if m.analysis_names
                   else tr("picker.unknown")))
        if m.open_tab_count is not None:
            names = ", ".join(m.open_analysis_names) if m.open_analysis_names else "-"
            add(tr("picker.detail.open_tabs"), QLabel(f"{m.open_tab_count} ({names})"))
        else:
            add(tr("picker.detail.open_tabs"), QLabel(tr("picker.unknown")))
        add(tr("picker.col.last_touched"), QLabel(_fmt_time(m.last_touched, precise=True)))
        add(tr("picker.detail.last_opened"), QLabel(_fmt_time(m.last_opened, precise=True)))
        add(tr("picker.detail.annotations"),
            QLabel(str(m.annotation_total) if m.annotation_total is not None
                   else tr("picker.unknown")))
        add(tr("picker.detail.exports"),
            QLabel(str(m.export_png_count) if m.export_png_count is not None
                   else tr("picker.unknown")))
        add(tr("picker.detail.chats"),
            QLabel(str(m.chat_session_count) if m.chat_session_count is not None
                   else tr("picker.unknown")))
        add(tr("picker.detail.disk_size"), QLabel(_fmt_size(m.disk_size_bytes)))
        add(tr("picker.detail.last_meas"), QLabel(_fmt_time(m.last_measurement)))
        if m.uncomputed:
            add(tr("picker.detail.updated_at"), QLabel(tr("picker.uncomputed")))
        else:
            add(tr("picker.detail.updated_at"), QLabel(_fmt_time(m.updated_at, precise=True)))

        # thumbnail: only if the path exists and is loadable.
        if m.thumbnail and m.host_path:
            from pathlib import Path
            p = Path(m.host_path) / m.thumbnail
            if p.exists():
                pix = QPixmap(str(p))
                if not pix.isNull():
                    self._thumb.setPixmap(
                        pix.scaledToWidth(240, Qt.TransformationMode.SmoothTransformation))

    # ----- actions -----

    def _on_refresh(self) -> None:
        # Route the manual refresh through the SAME background worker as startup
        # stale rebuilds. The HEAVY os.walk must never run on the GUI thread —
        # doing it inline here would freeze the dialog on a large synced-drive
        # dataset (Issue #50 §性能・スレッド設計; the row updates via _on_meta_built).
        m = self._current_meta()
        if m is None or not m.name:
            return
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
