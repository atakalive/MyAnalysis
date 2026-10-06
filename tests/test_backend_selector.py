"""Tests for gui.backend_selector_dialog (offscreen)."""
import os
from unittest.mock import MagicMock

import pytest

from common.i18n import tr

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


def _make_dialog(monkeypatch, parent_widget, *, busy=False, search_sel=("", "", "")):
    """busy はチャット応答中の状況を模す（ダイアログはもう busy を見ないので、
    test_apply_proceeds_while_busy の regression guard としてのみ意味を持つ）。

    AI 検索用モデル（[chat_search]）は必ず差し替える — 実 config.toml を読み書きしない。
    search_sel は (engine id, model, provider)。engine id "" = 全体設定に従う。
    書込の呼び出しは dlg._search_spy に記録される。"""
    import gui.backend_selector_dialog as mod
    from llm_backend.engines import engine_by_id

    eid, smodel, sprov = search_sel
    eng = engine_by_id(eid) if eid else None
    monkeypatch.setattr(mod, "chat_search_selection",
                        lambda: (eng, smodel, sprov) if eng else (None, "", ""))
    spy = MagicMock()
    monkeypatch.setattr(mod, "apply_chat_search_selection", spy)
    main_window = MagicMock()
    cw = MagicMock()
    cw.is_busy.return_value = busy
    main_window.chat_widget.return_value = cw
    dlg = mod.BackendSelectorDialog(main_window, parent_widget)
    dlg._search_spy = spy
    return dlg, main_window


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


def test_apply_proceeds_while_busy(monkeypatch, parent_widget):
    """応答中でも適用は拒否しない（regression guard）: 進行中ターンは自分の
    backend 参照で完走し、新設定は次の送信から効く。"""
    import gui.backend_selector_dialog as mod

    _patch_config(monkeypatch)
    spy = MagicMock()
    monkeypatch.setattr(mod, "apply_selection", spy)
    dlg, _ = _make_dialog(monkeypatch, parent_widget, busy=True)
    dlg._on_apply()
    spy.assert_called_once()
    from PySide6.QtWidgets import QDialog

    assert dlg.result() == QDialog.DialogCode.Accepted


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
    # While a ping is in flight, selection + Apply are locked so a
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
    # apply_selection can raise OSError (IO) too — it must surface
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
# choice-list Add/Remove controls                                              #
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
    """After Remove, the removed value must not still be listed (it only stays typed)."""
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


# --------------------------------------------------------------------------- #
# SessionEngineDialog — チャットセッションごとのエンジン上書き                 #
# --------------------------------------------------------------------------- #

def _session_dlg(monkeypatch, parent_widget, *, engine=None, model=None,
                 provider=None, busy_ids=()):
    import gui.backend_selector_dialog as mod
    from llm_bridge import chat_store

    _patch_config(monkeypatch, engine_id="claude-vscode", model="opus")
    sess = chat_store.new_session("mock", "sys")
    sess.engine, sess.engine_model, sess.engine_provider = engine, model, provider

    chat = MagicMock()
    chat._turns = {i: object() for i in busy_ids}
    applied = []
    chat._set_session_engine.side_effect = (
        lambda *a, **k: applied.append((a, k))
    )
    dlg = mod.SessionEngineDialog(MagicMock(), chat, sess, parent_widget)
    return dlg, sess, chat, applied


def test_session_dialog_opens_on_the_session_engine(monkeypatch, parent_widget):
    dlg, _, _, _ = _session_dlg(monkeypatch, parent_widget, engine="pi")
    assert dlg._engine_combo.currentData() == "pi"
    assert not dlg._follow_default.isChecked()


def test_session_dialog_opens_on_global_when_no_override(monkeypatch, parent_widget):
    dlg, _, _, _ = _session_dlg(monkeypatch, parent_widget)
    assert dlg._engine_combo.currentData() == "claude-vscode"
    assert dlg._follow_default.isChecked()


def test_follow_default_disables_inputs_but_not_apply(monkeypatch, parent_widget):
    from PySide6.QtWidgets import QDialogButtonBox
    dlg, _, _, _ = _session_dlg(monkeypatch, parent_widget)
    assert not dlg._engine_combo.isEnabled()
    # 「既定に従う」を適用（=上書き解除）できないと詰むので OK は生きている
    assert dlg._buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    dlg._follow_default.setChecked(False)
    assert dlg._engine_combo.isEnabled()


def test_ping_lock_and_follow_default_are_independent(monkeypatch, parent_widget):
    """一本化すると ping 中にチェックを外した瞬間コンボが再有効化され、
    ロックが防いでいる stale-result レースが復活する。"""
    dlg, _, _, _ = _session_dlg(monkeypatch, parent_widget, engine="pi")
    dlg._set_selection_enabled(False)             # ping 中
    dlg._follow_default.setChecked(True)
    dlg._follow_default.setChecked(False)         # 外しても ping 中は解錠しない
    assert not dlg._engine_combo.isEnabled()
    dlg._set_selection_enabled(True)
    assert dlg._engine_combo.isEnabled()


def test_apply_writes_the_session_not_the_tomls(monkeypatch, parent_widget):
    import gui.backend_selector_dialog as mod
    called = []
    monkeypatch.setattr(mod, "apply_selection",
                        lambda *a, **k: called.append(a))
    dlg, sess, chat, applied = _session_dlg(
        monkeypatch, parent_widget, engine="pi", model="qwen3-coder")
    dlg._on_apply()
    assert called == []                            # TOML は書かない
    assert applied and applied[0][0][1] == "pi"    # セッションへ書く


def test_apply_with_follow_default_clears_the_override(monkeypatch, parent_widget):
    dlg, sess, chat, applied = _session_dlg(monkeypatch, parent_widget, engine="pi")
    dlg._follow_default.setChecked(True)
    dlg._on_apply()
    assert applied and applied[0][0][1] is None


def test_busy_never_blocks_session_apply(monkeypatch, parent_widget):
    """応答中を理由に適用を拒否しない: 他タブ busy でも自タブ busy でも通る
    （自タブの進行中応答は旧エンジンで完走し、次の送信から新設定）。"""
    dlg, sess, _, applied = _session_dlg(monkeypatch, parent_widget, engine="pi")
    dlg._chat._turns = {"some-other-session": object()}
    dlg._on_apply()
    assert applied                                 # 他タブ busy → 適用できる

    dlg2, sess2, _, applied2 = _session_dlg(monkeypatch, parent_widget, engine="pi")
    dlg2._chat._turns = {sess2.id: object()}
    dlg2._on_apply()
    assert applied2                                # 自分が busy でも適用できる


def test_env_backend_warning_hidden_in_session_mode(monkeypatch, parent_widget):
    """「LLM_BACKEND が優先」はセッション上書きでは嘘（build_backend を直接呼ぶので
    get_backend の env 優先順位を通らない）。"""
    monkeypatch.setenv("LLM_BACKEND", "mock")
    dlg, _, _, _ = _session_dlg(monkeypatch, parent_widget, engine="pi")
    assert dlg._env_warning.isHidden()


def test_probe_uses_session_settings(monkeypatch, parent_widget):
    """疎通確認が実際に走るバイナリと違うものを叩いては意味がない。"""
    import gui.backend_selector_dialog as mod
    seen = []
    monkeypatch.setattr(mod, "session_settings",
                        lambda e, m, p: seen.append((e.id, m, p)) or {"model": m})
    dlg, _, _, _ = _session_dlg(monkeypatch, parent_widget, engine="pi")
    dlg._probe_settings(dlg._selected_engine(), "qwen3-coder", "llama.cpp")
    assert seen == [("pi", "qwen3-coder", "llama.cpp")]


def test_seed_uses_session_value_only_for_its_own_engine(monkeypatch, parent_widget):
    """別エンジンに切り替えたとき、そのセッションの model を持ち込まない
    （claude のモデル名が pi のコンボに出てしまう）。"""
    dlg, _, _, _ = _session_dlg(
        monkeypatch, parent_widget, engine="pi", model="qwen3-coder")
    from llm_backend.engines import engine_by_id
    assert dlg._seed_value(engine_by_id("pi"), "model") == "qwen3-coder"
    assert dlg._seed_value(engine_by_id("claude-vscode"), "model") == "opus"



# --------------------------------------------------------------------------- #
# AI 検索用モデル（Issue #108）                                                  #
# --------------------------------------------------------------------------- #

def test_search_group_only_in_global_dialog(monkeypatch, parent_widget):
    dlg, _ = _make_dialog(monkeypatch, parent_widget)
    assert dlg._search_follow is not None
    sdlg, _, _, _ = _session_dlg(monkeypatch, parent_widget)
    assert not hasattr(sdlg, "_search_follow")


def test_search_follow_default_on_open(monkeypatch, parent_widget):
    _patch_config(monkeypatch, engine_id="codex", model="gpt")
    dlg, _ = _make_dialog(monkeypatch, parent_widget)
    assert dlg._search_follow.isChecked()
    assert dlg._search_engine_combo.currentData() == "codex"
    assert not dlg._search_engine_combo.isEnabled()
    assert not dlg._search_model_combo.isEnabled()


def test_search_override_on_open(monkeypatch, parent_widget):
    _patch_config(monkeypatch, engine_id="claude-vscode", model="opus")
    dlg, _ = _make_dialog(monkeypatch, parent_widget, search_sel=("codex", "gpt-x", ""))
    assert not dlg._search_follow.isChecked()
    assert dlg._search_engine_combo.currentData() == "codex"
    assert dlg._search_model_combo.currentText() == "gpt-x"
    assert dlg._search_engine_combo.isEnabled()
    dlg._search_follow.setChecked(True)
    assert not dlg._search_engine_combo.isEnabled()
    assert not dlg._search_model_combo.isEnabled()
    dlg._search_follow.setChecked(False)
    assert dlg._search_model_combo.isEnabled()


@pytest.mark.parametrize("follow", [True, False])
def test_search_locked_during_ping(monkeypatch, parent_widget, follow):
    import gui.backend_selector_dialog as mod

    _patch_config(monkeypatch, engine_id="mock")
    dlg, _ = _make_dialog(monkeypatch, parent_widget,
                          search_sel=("", "", "") if follow else ("pi", "m", ""))
    monkeypatch.setattr(mod, "build_backend", lambda *a, **k: MagicMock())
    monkeypatch.setattr(mod._PingWorker, "start", lambda self: None)
    dlg._on_test()
    assert not dlg._search_follow.isEnabled()
    for c in (dlg._search_engine_combo, dlg._search_model_combo, dlg._search_provider_combo):
        assert not c.isEnabled()


def test_search_engine_switch_rows(monkeypatch, parent_widget):
    _patch_config(monkeypatch, engine_id="claude-vscode")
    dlg, _ = _make_dialog(monkeypatch, parent_widget, search_sel=("pi", "", ""))
    form = dlg._search_form
    assert form.isRowVisible(dlg._search_model_combo)
    assert form.isRowVisible(dlg._search_provider_combo)
    dlg._search_engine_combo.setCurrentIndex(dlg._search_engine_combo.findData("mock"))
    assert not form.isRowVisible(dlg._search_model_combo)
    assert not form.isRowVisible(dlg._search_provider_combo)
    dlg._search_engine_combo.setCurrentIndex(dlg._search_engine_combo.findData("codex"))
    assert form.isRowVisible(dlg._search_model_combo)
    assert not form.isRowVisible(dlg._search_provider_combo)


def test_apply_writes_search_selection_after_main(monkeypatch, parent_widget):
    import gui.backend_selector_dialog as mod

    _patch_config(monkeypatch, engine_id="claude-vscode", model="opus")
    order = []
    monkeypatch.setattr(mod, "apply_selection", lambda *a, **k: order.append("main"))
    dlg, _ = _make_dialog(monkeypatch, parent_widget, search_sel=("pi", "m1", "llama.cpp"))
    dlg._search_spy.side_effect = lambda *a: order.append("search")
    dlg._on_apply()
    assert order == ["main", "search"]
    eng, model, provider = dlg._search_spy.call_args.args
    assert eng.id == "pi" and model == "m1" and provider == "llama.cpp"


def test_apply_follow_writes_empty_search_selection(monkeypatch, parent_widget):
    import gui.backend_selector_dialog as mod

    _patch_config(monkeypatch)
    monkeypatch.setattr(mod, "apply_selection", MagicMock())
    dlg, _ = _make_dialog(monkeypatch, parent_widget)
    dlg._on_apply()
    dlg._search_spy.assert_called_once_with(None, "", "")


def test_search_apply_error_warns_partial_and_closes(monkeypatch, parent_widget):
    """主の設定は書けたのに AI 検索用モデルの保存だけ失敗 → 部分適用だと警告して閉じる。

    開いたままにすると、ディスクは新しい設定なのにチャットは古いバックエンドのまま
    （apply_backend_change は accept の後で呼ばれる）になる。"""
    import gui.backend_selector_dialog as mod
    from PySide6.QtWidgets import QDialog

    _patch_config(monkeypatch)
    main = MagicMock()
    monkeypatch.setattr(mod, "apply_selection", main)
    warned, critical = [], []
    monkeypatch.setattr(mod.QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    monkeypatch.setattr(mod.QMessageBox, "critical", lambda *a, **k: critical.append(a[2]))
    dlg, _ = _make_dialog(monkeypatch, parent_widget)
    dlg._search_spy.side_effect = RuntimeError("bad toml")
    dlg._on_apply()
    main.assert_called_once()
    assert warned == [tr("backend.dialog.search_apply_failed", error="bad toml")]
    assert critical == []
    assert dlg.result() == QDialog.DialogCode.Accepted


def test_main_apply_error_skips_search_and_stays_open(monkeypatch, parent_widget):
    import gui.backend_selector_dialog as mod
    from PySide6.QtWidgets import QDialog

    _patch_config(monkeypatch)
    monkeypatch.setattr(mod, "apply_selection", MagicMock(side_effect=OSError("denied")))
    critical = []
    monkeypatch.setattr(mod.QMessageBox, "critical", lambda *a, **k: critical.append(a[2]))
    dlg, _ = _make_dialog(monkeypatch, parent_widget)
    dlg._on_apply()
    assert critical == ["denied"]
    dlg._search_spy.assert_not_called()
    assert dlg.result() != QDialog.DialogCode.Accepted
