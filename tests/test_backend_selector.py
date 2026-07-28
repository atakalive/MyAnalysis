"""Tests for gui.backend_selector_dialog (offscreen)."""
import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def parent_widget(qapp):
    from PySide6.QtWidgets import QWidget

    return QWidget()


def _patch_config(monkeypatch, *, engine_id="claude-vscode", model="opus", provider=""):
    import gui.backend_selector_dialog as mod

    monkeypatch.setattr(mod, "current_engine_id", lambda: engine_id)
    monkeypatch.setattr(mod, "current_model", lambda e: model)
    monkeypatch.setattr(mod, "current_provider", lambda e: provider)


def _make_dialog(monkeypatch, parent_widget, *, busy=False):
    import gui.backend_selector_dialog as mod

    main_window = MagicMock()
    cw = MagicMock()
    cw.is_busy.return_value = busy
    main_window.chat_widget.return_value = cw
    return mod.BackendSelectorDialog(main_window, parent_widget), main_window


def test_initial_selection_reflects_config(monkeypatch, parent_widget):
    _patch_config(monkeypatch, engine_id="pi", provider="openai-codex")
    dlg, _ = _make_dialog(monkeypatch, parent_widget)
    assert dlg._engine_combo.currentData() == "pi"
    assert not dlg._provider_combo.isHidden()      # pi has a provider field


def test_engine_switch_toggles_provider_and_repopulates(monkeypatch, parent_widget):
    _patch_config(monkeypatch, engine_id="claude-vscode")
    dlg, _ = _make_dialog(monkeypatch, parent_widget)
    assert dlg._provider_combo.isHidden()          # claude has no provider field
    # switch to pi (has provider)
    idx = dlg._engine_combo.findData("pi")
    dlg._engine_combo.setCurrentIndex(idx)
    assert not dlg._provider_combo.isHidden()


def test_apply_calls_apply_selection_and_accepts(monkeypatch, parent_widget):
    import gui.backend_selector_dialog as mod

    _patch_config(monkeypatch, engine_id="claude-vscode", model="opus")
    spy = MagicMock()
    monkeypatch.setattr(mod, "apply_selection", spy)
    dlg, _ = _make_dialog(monkeypatch, parent_widget, busy=False)
    dlg._on_apply()
    spy.assert_called_once()
    args, kwargs = spy.call_args
    assert args[0].id == "claude-vscode"
    assert args[1] == "opus"
    assert kwargs["engine_changed"] is False
    from PySide6.QtWidgets import QDialog

    assert dlg.result() == QDialog.DialogCode.Accepted


def test_apply_engine_changed_true(monkeypatch, parent_widget):
    import gui.backend_selector_dialog as mod

    _patch_config(monkeypatch, engine_id="claude-vscode", model="opus")
    spy = MagicMock()
    monkeypatch.setattr(mod, "apply_selection", spy)
    dlg, _ = _make_dialog(monkeypatch, parent_widget, busy=False)
    dlg._engine_combo.setCurrentIndex(dlg._engine_combo.findData("claude-cli"))
    dlg._on_apply()
    assert spy.call_args.kwargs["engine_changed"] is True


def test_apply_blocked_when_busy(monkeypatch, parent_widget):
    import gui.backend_selector_dialog as mod

    _patch_config(monkeypatch)
    spy = MagicMock()
    monkeypatch.setattr(mod, "apply_selection", spy)
    dlg, _ = _make_dialog(monkeypatch, parent_widget, busy=True)
    dlg._on_apply()
    spy.assert_not_called()
    from PySide6.QtWidgets import QDialog

    assert dlg.result() != QDialog.DialogCode.Accepted   # stays open


def test_ping_result_updates_label(monkeypatch, parent_widget):
    from llm_backend.ping import PingResult

    _patch_config(monkeypatch)
    dlg, _ = _make_dialog(monkeypatch, parent_widget)
    dlg._on_ping_result(PingResult(ok=True, elapsed=1.23, model="m", text="pong"))
    assert "OK" in dlg._result_label.text() or "pong" in dlg._result_label.text()
    dlg._on_ping_result(PingResult(ok=False, elapsed=0.1, error="boom"))
    assert "boom" in dlg._result_label.text()


def test_reject_stops_live_worker(monkeypatch, parent_widget):
    _patch_config(monkeypatch)
    dlg, _ = _make_dialog(monkeypatch, parent_widget)
    worker = MagicMock()
    worker.isRunning.return_value = True
    backend = MagicMock()
    dlg._ping_worker = worker
    dlg._ping_backend = backend
    dlg.reject()
    backend.cancel.assert_called_once()
    worker.wait.assert_called()
    backend.kill.assert_called()          # still running → escalates to kill


def test_ping_locks_selection_until_result(monkeypatch, parent_widget):
    # reviewer code P1: while a ping is in flight, selection + Apply are locked so a
    # result can never be shown against a different selection.
    import gui.backend_selector_dialog as mod
    from PySide6.QtWidgets import QDialogButtonBox

    _patch_config(monkeypatch, engine_id="mock")
    dlg, _ = _make_dialog(monkeypatch, parent_widget)
    monkeypatch.setattr(mod, "build_backend", lambda *a, **k: MagicMock())
    monkeypatch.setattr(mod._PingWorker, "start", lambda self: None)   # don't spawn
    ok = dlg._buttons.button(QDialogButtonBox.StandardButton.Ok)

    dlg._on_test()
    assert not dlg._engine_combo.isEnabled()
    assert not dlg._model_combo.isEnabled()
    assert not ok.isEnabled()

    from llm_backend.ping import PingResult
    dlg._on_ping_result(PingResult(ok=True, elapsed=0.1, model="m", text="pong"))
    assert dlg._engine_combo.isEnabled()
    assert dlg._model_combo.isEnabled()
    assert ok.isEnabled()


def test_apply_io_error_shows_message(monkeypatch, parent_widget):
    # reviewer code P2: apply_selection can raise OSError (IO) too — it must surface
    # as a message, not an uncaught Qt slot exception.
    import gui.backend_selector_dialog as mod
    from PySide6.QtWidgets import QDialog

    _patch_config(monkeypatch, engine_id="claude-vscode", model="opus")

    def _boom(*a, **k):
        raise OSError("disk full")

    shown = {}
    monkeypatch.setattr(mod, "apply_selection", _boom)
    monkeypatch.setattr(
        mod.QMessageBox, "critical", lambda *a, **k: shown.setdefault("called", True)
    )
    dlg, _ = _make_dialog(monkeypatch, parent_widget, busy=False)
    dlg._on_apply()
    assert shown.get("called")
    assert dlg.result() != QDialog.DialogCode.Accepted   # stays open
