"""Backend / model selector dialog + connectivity-check worker.

設定 menu → 「バックエンド/モデル設定」. Pick an *engine* (利用方法) and a *model*,
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
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from common.i18n import tr
from llm_backend import build_backend
from llm_backend.engines import (
    ENGINES,
    apply_selection,
    candidate_settings,
    combo_choices,
    current_engine_id,
    current_model,
    current_provider,
    engine_by_id,
    save_choices,
    session_settings,
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
        self._ping_unlocked = True

        # Baseline for engine_changed (used by both ping and apply).
        self._opened_engine_id = self._baseline_engine_id()

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
        self._model_row, self._model_btns = self._make_choice_row(
            self._model_combo, "model"
        )
        self._form.addRow(tr("backend.dialog.model"), self._model_row)

        self._provider_combo = QComboBox(self)
        self._provider_combo.setEditable(True)
        self._provider_row, self._provider_btns = self._make_choice_row(
            self._provider_combo, "provider"
        )
        self._form.addRow(tr("backend.dialog.provider"), self._provider_row)

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

    # ----- subclass hooks -----
    #
    # SessionEngineDialog (per-chat-session override) reuses this whole dialog —
    # combos, the ＋/－ choice lists, and the ping worker's cancel→wait→kill
    # shutdown — and only swaps these six seams. They are overridden, not branched
    # on a `session=` flag, so each method keeps a single coherent contract.

    def _baseline_engine_id(self) -> str:
        """Engine the dialog opens on (also the engine_changed baseline)."""
        return current_engine_id()

    def _seed_value(self, engine, field: str) -> str:
        """Initial text for `field` — the value this dialog is editing."""
        return current_model(engine) if field == "model" else current_provider(engine)

    def _probe_settings(self, engine, model: str, provider: str) -> dict:
        """Settings the connectivity check should build a backend from."""
        return candidate_settings(
            engine, model, provider, engine_changed=self._engine_changed()
        )

    def _check_busy(self) -> bool:
        """True → refuse to apply (something is mid-turn)."""
        cw = None
        if hasattr(self._main_window, "chat_widget"):
            cw = self._main_window.chat_widget()
        return cw is not None and cw.is_busy()

    def _do_apply(self, engine, model: str, provider: str) -> None:
        """Persist the selection. May raise RuntimeError/OSError."""
        apply_selection(
            engine, model, provider, engine_changed=self._engine_changed()
        )

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

    # ----- choice-list rows (combo + add/remove) -----

    def _make_choice_row(self, combo, field: str) -> tuple[QWidget, list[QPushButton]]:
        """Wrap ``combo`` with ＋/－ buttons that edit the persisted choice list.

        The row's field widget is the returned container, so row visibility is
        toggled on *it*; ``_sync_engine_widgets`` additionally sets the combo's own
        visibility so ``isHidden()`` still reflects the row state.
        """
        row = QWidget(self)
        box = QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(combo, 1)
        buttons: list[QPushButton] = []
        for label_key, tip_key, slot in (
            ("backend.dialog.choice_add", "backend.dialog.choice_add_tip", True),
            ("backend.dialog.choice_remove", "backend.dialog.choice_remove_tip", False),
        ):
            btn = QPushButton(tr(label_key), row)
            btn.setToolTip(tr(tip_key))
            btn.setFixedWidth(28)
            btn.setAutoDefault(False)   # else Enter in the combo would fire it
            btn.clicked.connect(
                lambda _checked=False, f=field, add=slot: self._on_edit_choices(f, add)
            )
            box.addWidget(btn)
            buttons.append(btn)
        return row, buttons

    def _combo_for(self, field: str):
        return self._model_combo if field == "model" else self._provider_combo

    def _on_edit_choices(self, field: str, add: bool) -> None:
        """＋: add the typed value to the list. －: remove it. Persisted at once.

        This edits the *dropdown contents*, which is independent of the selection
        being applied — so it is saved immediately rather than waiting for 適用.
        """
        engine = self._selected_engine()
        if engine is None or not engine.settings_key:
            return
        combo = self._combo_for(field)
        value = combo.currentText().strip()
        if not value:
            return
        current = list(combo_choices(engine, field))
        if add:
            if value in current:
                return
            current.append(value)
        else:
            if value not in current:
                return
            current.remove(value)
        try:
            save_choices(engine, field, current)
        except Exception as e:
            self._result_label.setText(tr("backend.dialog.choice_failed", error=str(e)))
            return
        self._populate_choices(engine, field, keep_text=True)

    def _sync_engine_widgets(self) -> None:
        engine = self._selected_engine()
        if engine is None:
            return
        self._populate_choices(engine, "model")
        has_provider = "provider" in engine.fields
        if has_provider:
            self._populate_choices(engine, "provider")
        self._form.setRowVisible(self._provider_row, has_provider)
        # Keep the combo's own hidden-state in sync with the row it lives in.
        self._provider_combo.setVisible(has_provider)
        for btn in self._provider_btns:
            btn.setVisible(has_provider)
        self._update_warnings(engine)

    def _populate_choices(self, engine, field: str, *, keep_text: bool = False) -> None:
        """Rebuild ``field``'s dropdown from the choice list.

        ``keep_text`` retains what the user typed (used after ＋/－, which must not
        reset the selection back to the persisted one). It also suppresses the
        "current value first" rule: right after a －, the removed value is still in
        the edit box, and listing it would make the removal look like it failed.
        Opening the dialog does prepend the configured value, so a model that is
        active but absent from the list is never invisible.
        """
        combo = self._combo_for(field)
        if keep_text:
            text = combo.currentText().strip()
            items: list[str] = []
        else:
            text = self._seed_value(engine, field)
            items = [text] if text else []
        for s in combo_choices(engine, field):
            if s and s not in items:
                items.append(s)
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(items)
        combo.setEditText(text)
        combo.blockSignals(False)

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

    def _set_selection_enabled(self, enabled: bool) -> None:
        """Lock/unlock the selection inputs + Apply while a ping is in flight so a
        connectivity result can never be shown against a *different* selection
        (e.g. a slow claude OK landing after the user switched to mock)."""
        self._ping_unlocked = enabled
        self._refresh_enabled()

    def _editable_by_mode(self) -> bool:
        """Second, independent gate on the inputs (SessionEngineDialog's
        「全体設定に従う」). Kept separate from the ping lock and AND-ed below —
        collapsing them into one flag would let un-checking the box mid-ping
        re-enable the combos and reintroduce the stale-result race the lock exists
        to prevent."""
        return True

    def _refresh_enabled(self) -> None:
        on = self._ping_unlocked and self._editable_by_mode()
        self._engine_combo.setEnabled(on)
        self._model_combo.setEnabled(on)
        self._provider_combo.setEnabled(on)
        for btn in (*self._model_btns, *self._provider_btns):
            btn.setEnabled(on)
        # Apply follows the ping lock only: in follow-default mode the inputs are
        # greyed out but applying (= clearing the override) must stay possible.
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            self._ping_unlocked
        )

    # ----- connectivity check -----

    def _on_test(self) -> None:
        engine = self._selected_engine()
        if engine is None:
            return
        model = self._current_model_text()
        provider = self._current_provider_text(engine)
        settings = self._probe_settings(engine, model, provider)
        backend = build_backend(engine.backend_key, settings)
        self._test_btn.setEnabled(False)
        self._set_selection_enabled(False)
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
        self._set_selection_enabled(True)
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
        if self._check_busy():
            self._result_label.setText(tr("backend.dialog.busy_warning"))
            return
        engine = self._selected_engine()
        if engine is None:
            return
        model = self._current_model_text()
        provider = self._current_provider_text(engine)
        try:
            self._do_apply(engine, model, provider)
        except (RuntimeError, OSError) as e:
            # RuntimeError = set_toml_keys validation / rollback double-fault;
            # OSError (incl. PermissionError) = raw IO on either write. Both must
            # surface to the user, not become an uncaught Qt slot exception.
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


class SessionEngineDialog(BackendSelectorDialog):
    """Per-chat-session engine override — the same dialog, six seams swapped.

    Subclass rather than a ``session=`` flag: every method whose behaviour differs
    has a single coherent contract this way, instead of six ``if self._session``
    branches inside docstrings that assert global semantics. And not a dialog built
    from scratch, because the ping worker's cancel → wait → kill shutdown is ~70
    lines that must not be forked.

    Applying writes the ChatSession, not the TOMLs. The ＋/－ dropdown lists still
    write models.toml — those are shared candidate lists, and sharing them between
    the global dialog and every session is the point (note they persist immediately,
    so they survive Cancel; same as the global dialog).
    """

    def __init__(self, main_window, chat_widget, session, parent=None):
        self._chat = chat_widget
        self._session = session
        super().__init__(main_window, parent)
        self.setWindowTitle(tr("backend.dialog.session_title"))

        self._follow_default = QCheckBox(tr("backend.dialog.follow_default"), self)
        self._follow_default.setChecked(
            not (getattr(session, "engine", None) or "").strip()
        )
        self._follow_default.toggled.connect(self._on_follow_toggled)
        self._form.insertRow(0, "", self._follow_default)
        self._refresh_enabled()

    # ----- hooks -----

    def _baseline_engine_id(self) -> str:
        """Open on the session's own engine when it has one, else the global."""
        eid = (getattr(self._session, "engine", None) or "").strip()
        return eid if engine_by_id(eid) is not None else current_engine_id()

    def _seed_value(self, engine, field: str) -> str:
        """Session value if this engine IS the session's, else that engine's default.

        Carrying the session's model over to a different engine would seed e.g.
        "claude-opus-5" into a pi combo, so it only applies to the matching engine.
        """
        if (getattr(self._session, "engine", None) or "").strip() == engine.id:
            key = "engine_model" if field == "model" else "engine_provider"
            val = (getattr(self._session, key, None) or "").strip()
            if val:
                return val
        return super()._seed_value(engine, field)

    def _probe_settings(self, engine, model: str, provider: str) -> dict:
        """Probe exactly what the session will run — engine_changed is meaningless
        here (see engines.session_settings), and pinging a different binary than the
        session uses would make the check worthless."""
        return session_settings(engine, model, provider)

    def _check_busy(self) -> bool:
        """Only THIS session blocks. The widget-wide is_busy() would refuse to edit
        tab A's engine merely because tab B is streaming."""
        turns = getattr(self._chat, "_turns", None)
        return bool(turns and self._session.id in turns)

    def _do_apply(self, engine, model: str, provider: str) -> None:
        if self._follow_default.isChecked():
            self._chat._set_session_engine(self._session, None)
        else:
            self._chat._set_session_engine(self._session, engine.id, model, provider)

    def _update_warnings(self, engine=None) -> None:
        if engine is None:
            engine = self._selected_engine()
        # LLM_BACKEND does NOT win for a session override: _build_session_backend
        # calls build_backend(engine.backend_key, ...) directly, bypassing
        # get_backend()'s env precedence. Showing that warning here would be a lie.
        self._env_warning.setVisible(False)
        # CLAUDE_CODE_BIN, conversely, matters MORE per-session: a session pinned to
        # claude-vscode gets bin="" and _discover_binary then honours that env var.
        self._env_bin_warning.setVisible(bool(
            engine is not None and engine.id == "claude-vscode"
            and os.environ.get("CLAUDE_CODE_BIN")
        ))

    # ----- follow-default gate -----

    def _editable_by_mode(self) -> bool:
        return not self._follow_default.isChecked()

    def _on_follow_toggled(self, _checked: bool = False) -> None:
        self._refresh_enabled()

    def _sync_engine_widgets(self) -> None:
        super()._sync_engine_widgets()
        engine = self._selected_engine()
        # mock has fields=() — the base only hides the provider row, so without this
        # the header would advertise a model the engine ignores.
        self._form.setRowVisible(
            self._model_row, engine is not None and "model" in engine.fields
        )
