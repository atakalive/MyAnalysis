"""Backend / model selector dialog + connectivity-check worker.

View menu → 「バックエンド/モデル設定」. Pick an *engine* (利用方法) and a *model*,
optionally run a connectivity check on the candidate configuration (a minimal
``stream()`` turn on a background thread, UI non-blocking), then apply — which
rewrites the truth-source TOMLs and refreshes caches so every chat session picks
up the new backend on its next send (no restart).
"""

from __future__ import annotations

import os

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from common.i18n import tr
from llm_backend import build_backend
from llm_backend.engines import (
    ENGINES,
    apply_selection,
    candidate_settings,
    current_engine_id,
    current_model,
    current_provider,
    engine_by_id,
)
from llm_backend.ping import ping_backend


class _PingWorker(QThread):
    """Run ping_backend(self.backend) off the GUI thread and emit the result."""

    result = Signal(object)   # PingResult

    def __init__(self, backend, parent):
        super().__init__(parent)
        self.backend = backend

    def run(self) -> None:
        self.result.emit(ping_backend(self.backend))


class BackendSelectorDialog(QDialog):
    def __init__(self, main_window, parent=None):
        super().__init__(parent if parent is not None else main_window)
        self._main_window = main_window
        self.setWindowTitle(tr("backend.dialog.title"))
        self._workers: list[_PingWorker] = []
        self._ping_worker: _PingWorker | None = None
        self._ping_backend = None
        self._ping_timeout: QTimer | None = None
        self._ping_kill: QTimer | None = None

        # Baseline for engine_changed (used by both ping and apply).
        self._opened_engine_id = current_engine_id()

        layout = QVBoxLayout(self)
        self._form = QFormLayout()
        layout.addLayout(self._form)

        self._engine_combo = QComboBox(self)
        for e in ENGINES:
            self._engine_combo.addItem(tr(e.label_key), e.id)
        idx = self._engine_combo.findData(self._opened_engine_id)
        if idx >= 0:
            self._engine_combo.setCurrentIndex(idx)
        self._form.addRow(tr("backend.dialog.engine"), self._engine_combo)

        self._model_combo = QComboBox(self)
        self._model_combo.setEditable(True)
        self._form.addRow(tr("backend.dialog.model"), self._model_combo)

        self._provider_combo = QComboBox(self)
        self._provider_combo.setEditable(True)
        self._form.addRow(tr("backend.dialog.provider"), self._provider_combo)

        self._env_warning = QLabel(tr("backend.dialog.env_warning"), self)
        self._env_warning.setWordWrap(True)
        layout.addWidget(self._env_warning)
        self._env_bin_warning = QLabel(tr("backend.dialog.env_bin_warning"), self)
        self._env_bin_warning.setWordWrap(True)
        layout.addWidget(self._env_bin_warning)

        self._test_btn = QPushButton(tr("backend.dialog.test"), self)
        layout.addWidget(self._test_btn)
        self._result_label = QLabel("", self)
        self._result_label.setWordWrap(True)
        layout.addWidget(self._result_label)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            tr("backend.dialog.apply")
        )
        layout.addWidget(self._buttons)

        # initial population for the opened engine.
        self._sync_engine_widgets()

        self._engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        self._test_btn.clicked.connect(self._on_test)
        self._buttons.accepted.connect(self._on_apply)
        self._buttons.rejected.connect(self.reject)

    # ----- selection helpers -----

    def _selected_engine_id(self) -> str:
        return self._engine_combo.currentData()

    def _selected_engine(self):
        return engine_by_id(self._selected_engine_id())

    def _engine_changed(self) -> bool:
        return self._selected_engine_id() != self._opened_engine_id

    def _current_model_text(self) -> str:
        return self._model_combo.currentText().strip()

    def _current_provider_text(self, engine) -> str:
        if "provider" not in engine.fields:
            return ""
        return self._provider_combo.currentText().strip()

    def _sync_engine_widgets(self) -> None:
        engine = self._selected_engine()
        if engine is None:
            return
        self._populate_model(engine)
        has_provider = "provider" in engine.fields
        if has_provider:
            self._populate_provider(engine)
        self._form.setRowVisible(self._provider_combo, has_provider)
        self._update_warnings(engine)

    def _populate_model(self, engine) -> None:
        self._model_combo.blockSignals(True)
        self._model_combo.clear()
        cur = current_model(engine)
        items: list[str] = []
        if cur:
            items.append(cur)
        for s in engine.model_suggestions:
            if s and s not in items:
                items.append(s)
        self._model_combo.addItems(items)
        self._model_combo.setEditText(cur)
        self._model_combo.blockSignals(False)

    def _populate_provider(self, engine) -> None:
        self._provider_combo.blockSignals(True)
        self._provider_combo.clear()
        cur = current_provider(engine)
        items: list[str] = []
        if cur and cur not in items:
            items.append(cur)
        for s in engine.provider_suggestions:
            if s not in items:
                items.append(s)
        self._provider_combo.addItems(items)
        self._provider_combo.setEditText(cur)
        self._provider_combo.blockSignals(False)

    def _update_warnings(self, engine=None) -> None:
        if engine is None:
            engine = self._selected_engine()
        self._env_warning.setVisible(bool(os.environ.get("LLM_BACKEND")))
        show_bin = bool(
            engine is not None
            and engine.id == "claude-vscode"
            and os.environ.get("CLAUDE_CODE_BIN")
        )
        self._env_bin_warning.setVisible(show_bin)

    def _on_engine_changed(self, _idx: int = 0) -> None:
        self._sync_engine_widgets()

    # ----- connectivity check -----

    def _on_test(self) -> None:
        engine = self._selected_engine()
        if engine is None:
            return
        model = self._current_model_text()
        provider = self._current_provider_text(engine)
        settings = candidate_settings(
            engine, model, provider, engine_changed=self._engine_changed()
        )
        backend = build_backend(engine.backend_key, settings)
        self._test_btn.setEnabled(False)
        self._result_label.setText(tr("backend.dialog.testing"))

        worker = _PingWorker(backend, QApplication.instance())
        self._ping_worker = worker
        self._ping_backend = backend
        self._workers.append(worker)
        worker.result.connect(self._on_ping_result)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(lambda: self._forget_worker(worker))

        self._ping_timeout = QTimer(self)
        self._ping_timeout.setSingleShot(True)
        self._ping_timeout.timeout.connect(lambda: self._on_ping_timeout(backend))
        self._ping_timeout.start(60000)
        worker.start()

    def _on_ping_timeout(self, backend) -> None:
        if hasattr(backend, "cancel"):
            try:
                backend.cancel()
            except Exception:
                pass
        self._ping_kill = QTimer(self)
        self._ping_kill.setSingleShot(True)
        self._ping_kill.timeout.connect(
            lambda: backend.kill() if hasattr(backend, "kill") else None
        )
        self._ping_kill.start(2000)

    def _on_ping_result(self, result) -> None:
        self._ping_worker = None
        self._ping_backend = None
        if self._ping_timeout is not None:
            self._ping_timeout.stop()
        if self._ping_kill is not None:
            self._ping_kill.stop()
        self._test_btn.setEnabled(True)
        if result.ok:
            self._result_label.setText(
                tr(
                    "backend.dialog.test_ok",
                    elapsed=f"{result.elapsed:.1f}",
                    model=result.model,
                    text=result.text,
                )
            )
        else:
            self._result_label.setText(
                tr("backend.dialog.test_fail", error=result.error)
            )

    def _forget_worker(self, worker) -> None:
        try:
            self._workers.remove(worker)
        except ValueError:
            pass

    def _stop_ping_worker(self) -> None:
        """Synchronously wind down a live ping worker (don't rely on the timers,
        which vanish with the dialog): cancel → wait → kill → wait."""
        worker = self._ping_worker
        backend = self._ping_backend
        if worker is None:
            return
        if backend is not None and hasattr(backend, "cancel"):
            try:
                backend.cancel()
            except Exception:
                pass
        worker.requestInterruption()
        worker.wait(2000)
        if worker.isRunning() and backend is not None and hasattr(backend, "kill"):
            try:
                backend.kill()
            except Exception:
                pass
            worker.wait(2000)
        self._ping_worker = None
        self._ping_backend = None

    # ----- lifecycle -----

    def _on_apply(self) -> None:
        cw = None
        if hasattr(self._main_window, "chat_widget"):
            cw = self._main_window.chat_widget()
        if cw is not None and cw.is_busy():
            self._result_label.setText(tr("backend.dialog.busy_warning"))
            return
        engine = self._selected_engine()
        if engine is None:
            return
        model = self._current_model_text()
        provider = self._current_provider_text(engine)
        try:
            apply_selection(
                engine, model, provider, engine_changed=self._engine_changed()
            )
        except RuntimeError as e:
            QMessageBox.critical(self, tr("backend.dialog.title"), str(e))
            return
        self._stop_ping_worker()
        super().accept()

    def reject(self) -> None:
        self._stop_ping_worker()
        super().reject()

    def closeEvent(self, event) -> None:
        self._stop_ping_worker()
        super().closeEvent(event)
