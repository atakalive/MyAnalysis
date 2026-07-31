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


# --------------------------------------------------------------------------- #
# choice-list ＋/－ controls                                                    #
# --------------------------------------------------------------------------- #

def _patch_choices(monkeypatch, initial=("a", "b")):
    """Stub combo_choices/save_choices with an in-memory store."""
    import gui.backend_selector_dialog as mod

    store = {"model": list(initial), "provider": list(initial)}
    monkeypatch.setattr(mod, "combo_choices", lambda e, f: tuple(store[f]))
    monkeypatch.setattr(
        mod, "save_choices", lambda e, f, vals: store.__setitem__(f, list(vals))
    )
    return store


def test_add_appends_typed_value_and_persists(monkeypatch, parent_widget):
    _patch_config(monkeypatch, engine_id="pi", model="a", provider="")
    store = _patch_choices(monkeypatch)
    dlg, _ = _make_dialog(monkeypatch, parent_widget)
    dlg._model_combo.setEditText("qwen3-coder")
    dlg._on_edit_choices("model", True)
    assert store["model"] == ["a", "b", "qwen3-coder"]
    # the typed value survives the repopulate and stays selected
    assert dlg._model_combo.currentText() == "qwen3-coder"


def test_remove_drops_typed_value_and_persists(monkeypatch, parent_widget):
    _patch_config(monkeypatch, engine_id="pi", model="a", provider="")
    store = _patch_choices(monkeypatch)
    dlg, _ = _make_dialog(monkeypatch, parent_widget)
    dlg._model_combo.setEditText("b")
    dlg._on_edit_choices("model", False)
    assert store["model"] == ["a"]


def test_add_is_idempotent_and_blank_is_ignored(monkeypatch, parent_widget):
    _patch_config(monkeypatch, engine_id="pi", model="a", provider="")
    store = _patch_choices(monkeypatch)
    dlg, _ = _make_dialog(monkeypatch, parent_widget)
    dlg._model_combo.setEditText("b")          # already present
    dlg._on_edit_choices("model", True)
    assert store["model"] == ["a", "b"]
    dlg._model_combo.setEditText("   ")        # blank → no-op
    dlg._on_edit_choices("model", True)
    assert store["model"] == ["a", "b"]


def test_remove_unknown_value_is_noop(monkeypatch, parent_widget):
    _patch_config(monkeypatch, engine_id="pi", model="a", provider="")
    store = _patch_choices(monkeypatch)
    dlg, _ = _make_dialog(monkeypatch, parent_widget)
    dlg._model_combo.setEditText("never-added")
    dlg._on_edit_choices("model", False)
    assert store["model"] == ["a", "b"]


def test_provider_choices_are_independent_of_model(monkeypatch, parent_widget):
    _patch_config(monkeypatch, engine_id="pi", model="a", provider="")
    store = _patch_choices(monkeypatch)
    dlg, _ = _make_dialog(monkeypatch, parent_widget)
    dlg._provider_combo.setEditText("llama.cpp")
    dlg._on_edit_choices("provider", True)
    assert store["provider"] == ["a", "b", "llama.cpp"]
    assert store["model"] == ["a", "b"]


def test_save_failure_is_surfaced_not_raised(monkeypatch, parent_widget):
    import gui.backend_selector_dialog as mod

    _patch_config(monkeypatch, engine_id="pi", model="a", provider="")
    _patch_choices(monkeypatch)

    def _boom(e, f, vals):
        raise RuntimeError("models.toml is read-only")

    monkeypatch.setattr(mod, "save_choices", _boom)
    dlg, _ = _make_dialog(monkeypatch, parent_widget)
    dlg._model_combo.setEditText("x")
    dlg._on_edit_choices("model", True)        # must not propagate
    assert "read-only" in dlg._result_label.text()


def test_choice_buttons_locked_during_ping(monkeypatch, parent_widget):
    _patch_config(monkeypatch, engine_id="pi", model="a", provider="")
    _patch_choices(monkeypatch)
    dlg, _ = _make_dialog(monkeypatch, parent_widget)
    dlg._set_selection_enabled(False)
    assert all(not b.isEnabled() for b in (*dlg._model_btns, *dlg._provider_btns))
    dlg._set_selection_enabled(True)
    assert all(b.isEnabled() for b in (*dlg._model_btns, *dlg._provider_btns))


def test_removed_value_leaves_the_dropdown_immediately(monkeypatch, parent_widget):
    """After －, the removed value must not still be listed (it only stays typed)."""
    _patch_config(monkeypatch, engine_id="pi", model="a", provider="")
    _patch_choices(monkeypatch, initial=("a", "b"))
    dlg, _ = _make_dialog(monkeypatch, parent_widget)
    dlg._model_combo.setEditText("b")
    dlg._on_edit_choices("model", False)
    items = [dlg._model_combo.itemText(i) for i in range(dlg._model_combo.count())]
    assert items == ["a"]
    assert dlg._model_combo.currentText() == "b"   # still in the edit box


def test_added_value_is_listed_once(monkeypatch, parent_widget):
    _patch_config(monkeypatch, engine_id="pi", model="a", provider="")
    _patch_choices(monkeypatch, initial=("a", "b"))
    dlg, _ = _make_dialog(monkeypatch, parent_widget)
    dlg._model_combo.setEditText("c")
    dlg._on_edit_choices("model", True)
    items = [dlg._model_combo.itemText(i) for i in range(dlg._model_combo.count())]
    assert items == ["a", "b", "c"]


def test_configured_value_is_listed_first_on_open(monkeypatch, parent_widget):
    """A configured model absent from the list must still be visible."""
    _patch_config(monkeypatch, engine_id="pi", model="not-in-list", provider="")
    _patch_choices(monkeypatch, initial=("a", "b"))
    dlg, _ = _make_dialog(monkeypatch, parent_widget)
    items = [dlg._model_combo.itemText(i) for i in range(dlg._model_combo.count())]
    assert items == ["not-in-list", "a", "b"]
