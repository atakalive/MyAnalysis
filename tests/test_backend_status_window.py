"""gui.backend_status_window — 対応ドライバの導入状況ウィンドウ（offscreen）。

**実プロセスは一切起動しない。** `Popen` と preflight を差し替え、呼ばれた引数だけを検証する。
とくに「自動経路が課金しないこと」「pi auth を呼ばないこと」はここで守る。
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from llm_backend.preflight import EngineStatus  # noqa: E402


@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def parent_widget(qapp):
    from PySide6.QtWidgets import QWidget
    return QWidget()


@pytest.fixture()
def no_probe(monkeypatch):
    """ワーカーを起動させない: refresh() は何もしない。行の描画は _paint を直接呼ぶ。"""
    import gui.backend_status_window as mod
    monkeypatch.setattr(mod.BackendStatusWindow, "refresh", lambda self: None)
    return mod


def _win(no_probe, parent_widget):
    return no_probe.BackendStatusWindow(MagicMock(), parent_widget)


def _st(engine_id, **kw):
    return EngineStatus(engine_id=engine_id, **kw)


# ---- 行の構成 ----


def test_one_row_per_catalog_engine(no_probe, parent_widget):
    from llm_backend.engines import ENGINES
    w = _win(no_probe, parent_widget)
    assert set(w._rows) == {e.id for e in ENGINES}


def test_install_label_when_missing(no_probe, parent_widget):
    w = _win(no_probe, parent_widget)
    w._paint(_st("pi", prereq_state="ok", binary_state="missing",
                 install=("npm", "i", "-g", "x")))
    act = w._rows["pi"]["action"]
    assert not act.isHidden()
    assert act.text() == no_probe.tr("backend.status.btn.install")


def test_update_label_when_already_installed(no_probe, parent_widget):
    """入っている行はボタンが消えるのではなく「更新」になる（同じ npm i -g）。"""
    w = _win(no_probe, parent_widget)
    w._paint(_st("pi", prereq_state="ok", binary_state="ok", version="0.83.0",
                 install=("npm", "i", "-g", "x")))
    act = w._rows["pi"]["action"]
    assert not act.isHidden()
    assert act.text() == no_probe.tr("backend.status.btn.update")


def test_update_and_install_run_the_same_command(no_probe, parent_widget,
                                                 monkeypatch):
    w = _win(no_probe, parent_widget)
    seen = []
    monkeypatch.setattr(w, "_run_install", lambda cmd, engine_id: seen.append(list(cmd)))
    for state in ("missing", "ok"):
        w._paint(_st("pi", prereq_state="ok", binary_state=state,
                     install=("npm", "i", "-g", "x")))
        w._on_action("pi")
    assert seen == [["npm", "i", "-g", "x"], ["npm", "i", "-g", "x"]]


def test_login_is_a_separate_button_and_shows_even_when_authed(no_probe,
                                                               parent_widget):
    """アカウント切替・再ログインは認証済みでも正当な操作。"""
    w = _win(no_probe, parent_widget)
    w._paint(_st("pi", binary_state="ok", auth_state="ok", login=("pi",)))
    assert not w._rows["pi"]["login"].isHidden()
    w._paint(_st("pi", binary_state="missing", login=None))
    assert w._rows["pi"]["login"].isHidden()


def test_no_install_button_when_prereq_blocks(no_probe, parent_widget):
    """node/npm が無いのにインストールを押させない（0 を飛ばして 1 に行かない）。"""
    w = _win(no_probe, parent_widget)
    w._paint(_st("pi", prereq_state="missing", binary_state="missing", install=None))
    assert w._rows["pi"]["action"].isHidden()


def test_no_install_button_when_state_is_unknown(no_probe, parent_widget):
    """タイムアウトしただけで再インストールを勧めない。"""
    w = _win(no_probe, parent_widget)
    w._paint(_st("pi", prereq_state="ok", binary_state="unknown", install=None))
    assert w._rows["pi"]["action"].isHidden()


def test_ping_button_hidden_when_not_installed(no_probe, parent_widget):
    w = _win(no_probe, parent_widget)
    w._paint(_st("pi", binary_state="missing"))
    assert w._rows["pi"]["ping"].isHidden()
    w._paint(_st("pi", binary_state="ok"))
    assert not w._rows["pi"]["ping"].isHidden()


def test_auth_cell_lists_providers(no_probe, parent_widget):
    w = _win(no_probe, parent_widget)
    w._paint(_st("pi", binary_state="ok", auth_state="ok",
                 authed_providers=("anthropic", "openai-codex")))
    assert "anthropic, openai-codex" in w._rows["pi"]["auth"].text()


# ---- generation ガード ----


def test_stale_worker_result_is_dropped(no_probe, parent_widget):
    """再確認を連打しても古いワーカーの結果で上書きしない。"""
    w = _win(no_probe, parent_widget)
    w._generation = 5
    w._on_row(5, _st("pi", binary_state="ok", version="9.9.9"))
    assert "9.9.9" in w._rows["pi"]["install"].text()
    w._on_row(4, _st("pi", binary_state="missing", version=None))   # stale
    assert "9.9.9" in w._rows["pi"]["install"].text()


# ---- プロセス起動の形 ----


def test_bare_name_is_resolved_before_spawning(no_probe, parent_widget, monkeypatch):
    """win32 で `npm` は実行ファイルではなく `npm.cmd`。解決しないと WinError 2 で
    インストール/ログインボタンが必ず失敗する（実際に踏んだ）。"""
    from llm_backend import preflight
    monkeypatch.setattr(preflight.sys, "platform", "win32")
    monkeypatch.setattr(
        preflight, "_which",
        lambda n: r"C:\nodejs\npm.cmd" if n == "npm" else None,
    )
    assert preflight._wrap(["npm", "i", "-g", "x"]) == [
        "cmd.exe", "/c", r"C:\nodejs\npm.cmd", "i", "-g", "x",
    ]


def test_already_resolved_cmd_shim_is_still_wrapped(no_probe, parent_widget,
                                                    monkeypatch):
    from llm_backend import preflight
    monkeypatch.setattr(preflight.sys, "platform", "win32")
    monkeypatch.setattr(preflight, "_which", lambda n: n)
    assert preflight._wrap(["npm.cmd", "i"]) == ["cmd.exe", "/c", "npm.cmd", "i"]


def test_plain_executable_is_not_wrapped(no_probe, parent_widget, monkeypatch):
    from llm_backend import preflight
    monkeypatch.setattr(preflight.sys, "platform", "win32")
    monkeypatch.setattr(preflight, "_which", lambda n: r"C:\nodejs\node.exe")
    assert preflight._wrap(["node", "--version"]) == [r"C:\nodejs\node.exe", "--version"]


def test_login_opens_a_new_console_on_windows(no_probe, parent_widget, monkeypatch):
    """端末を新規コンソールで起こす。argv[0] は解決済みでなければ WinError 2 になる。"""
    import subprocess
    from llm_backend import preflight
    w = _win(no_probe, parent_widget)
    monkeypatch.setattr(no_probe.sys, "platform", "win32")
    monkeypatch.setattr(preflight.sys, "platform", "win32")
    monkeypatch.setattr(
        preflight, "_which", lambda n: r"C:\npm\pi.cmd" if n == "pi" else None
    )
    seen = {}

    def _popen(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return MagicMock()
    monkeypatch.setattr(no_probe.subprocess, "Popen", _popen)
    w._launch_login(["pi"])
    assert seen["cmd"] == ["cmd.exe", "/c", r"C:\npm\pi.cmd"]   # 裸の "pi" では起動しない
    assert seen["kw"]["creationflags"] == getattr(
        subprocess, "CREATE_NEW_CONSOLE", 0
    )


def test_login_on_posix_shows_the_command_instead_of_guessing_a_terminal(
    no_probe, parent_widget, monkeypatch
):
    w = _win(no_probe, parent_widget)
    monkeypatch.setattr(no_probe.sys, "platform", "linux")
    called = []
    monkeypatch.setattr(no_probe.subprocess, "Popen",
                        lambda *a, **k: called.append(a))
    w._launch_login(["pi"])
    assert called == []                       # 端末エミュレータを推測しない
    assert "pi" in w._log.toPlainText()


# ---- 課金・トークン回転をしない ----


def test_opening_and_refreshing_never_pings(parent_widget, monkeypatch):
    """自動経路で ping_backend を呼ぶと開くたびに ping/pong で課金される。"""
    import gui.backend_status_window as mod
    calls = []
    monkeypatch.setattr(mod, "ping_backend", lambda b: calls.append(b))
    monkeypatch.setattr(mod, "_ProbeWorker", MagicMock())
    w = mod.BackendStatusWindow(MagicMock(), parent_widget)
    w.refresh()
    assert calls == []


def test_ping_only_runs_when_the_button_is_pressed(no_probe, parent_widget,
                                                   monkeypatch):
    w = _win(no_probe, parent_widget)
    started = []
    monkeypatch.setattr(no_probe, "build_backend", lambda k, s: MagicMock())
    monkeypatch.setattr(no_probe._PingWorker, "start",
                        lambda self: started.append(self))
    w._on_ping("mock")
    assert len(started) == 1


def test_probe_never_invokes_pi_auth(monkeypatch):
    """`pi auth print-bearer-token` は共有 refresh token を回して他環境をログアウトさせる。"""
    from llm_backend import preflight
    seen: list[list[str]] = []
    monkeypatch.setattr(preflight, "_which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(preflight, "_run",
                        lambda cmd, timeout: seen.append(list(cmd)) or None)
    preflight.check_all()
    assert not any("auth" in " ".join(c) for c in seen), seen


def test_no_auto_refresh_timer(no_probe, parent_widget):
    """meeting_share の 1 秒タイマーは真似しない（毎回プロセスを起動するため）。"""
    from PySide6.QtCore import QTimer
    w = _win(no_probe, parent_widget)
    timers = [c for c in w.findChildren(QTimer) if c.isActive()]
    assert timers == []


# ---- 後始末 ----


def test_close_stops_workers(no_probe, parent_widget):
    w = _win(no_probe, parent_widget)
    stopped = []
    w._stop_all = lambda: stopped.append(True)
    w.close()
    assert stopped == [True]


# ---- モデル取得 ----


def _ok_codex(**kw):
    from llm_backend.model_catalog import ModelList
    return ModelList("codex", "ok", **kw)


@pytest.fixture()
def models_toml(tmp_path, monkeypatch):
    from llm_backend import engines
    p = tmp_path / "models.toml"
    monkeypatch.setattr(engines, "models_toml_path", lambda: p)
    return p


@pytest.fixture()
def no_start(no_probe, monkeypatch):
    """ワーカーを起動させない（start の呼び出しだけ記録する）。"""
    started = {"models": [], "ping": [], "cmd": []}
    monkeypatch.setattr(no_probe._ModelsWorker, "start",
                        lambda self: started["models"].append(self))
    monkeypatch.setattr(no_probe._PingWorker, "start",
                        lambda self: started["ping"].append(self))
    monkeypatch.setattr(no_probe._CmdWorker, "start",
                        lambda self: started["cmd"].append(self))
    return started


def _choices(p):
    import tomllib
    return tomllib.loads(p.read_text(encoding="utf-8"))["codex"]["model_choices"]


def _write_choices(p, vals):
    from llm_backend.settings_store import set_toml_keys
    set_toml_keys(p, {"codex": {"model_choices": vals}})


def _buttons(w):
    return [row[k] for row in w._rows.values() for k in ("action", "models", "ping")]


def test_models_button_visibility(no_probe, parent_widget):
    w = _win(no_probe, parent_widget)
    for eid in ("claude-vscode", "claude-cli", "pi", "codex"):
        w._paint(_st(eid, binary_state="ok"))
        assert not w._rows[eid]["models"].isHidden(), eid
        for state in ("missing", "unknown"):
            w._paint(_st(eid, binary_state=state))
            assert w._rows[eid]["models"].isHidden(), (eid, state)
    w._paint(_st("openai-http", binary_state="n/a"))
    w._paint(_st("mock", binary_state="ok"))
    assert w._rows["openai-http"]["models"].isHidden()
    assert w._rows["mock"]["models"].isHidden()


def test_models_click_appends_to_file(no_probe, no_start, parent_widget,
                                      models_toml, monkeypatch):
    _write_choices(models_toml, ["A"])
    monkeypatch.setattr(no_probe.model_catalog, "fetch_models",
                        lambda eid: _ok_codex(models=("A", "B")))
    w = _win(no_probe, parent_widget)
    w._on_models("codex")
    assert len(no_start["models"]) == 1
    w._models.run()
    assert _choices(models_toml) == ["A", "B"]
    assert "B" in w._log.toPlainText()


def test_models_click_nothing_new_leaves_file(no_probe, no_start, parent_widget,
                                              models_toml, monkeypatch):
    _write_choices(models_toml, ["A"])
    before = models_toml.read_bytes()
    monkeypatch.setattr(no_probe.model_catalog, "fetch_models",
                        lambda eid: _ok_codex(models=("A",)))
    w = _win(no_probe, parent_widget)
    w._on_models("codex")
    w._models.run()
    assert models_toml.read_bytes() == before


def test_models_result_keeps_changes_made_during_fetch(no_probe, no_start, parent_widget,
                                                       models_toml, monkeypatch):
    _write_choices(models_toml, ["A", "X"])
    monkeypatch.setattr(no_probe.model_catalog, "fetch_models",
                        lambda eid: _ok_codex(models=("A", "B")))
    w = _win(no_probe, parent_widget)
    w._on_models("codex")
    _write_choices(models_toml, ["A", "Y"])           # ダイアログ・同期を模す
    w._models.run()
    assert _choices(models_toml) == ["A", "Y", "B"]


def test_models_save_failure_is_logged(no_probe, no_start, parent_widget,
                                       models_toml, monkeypatch):
    monkeypatch.setattr(no_probe.model_catalog, "fetch_models",
                        lambda eid: _ok_codex(models=("B",)))

    def _boom(eng, ml):
        raise RuntimeError("read-only fs")
    monkeypatch.setattr(no_probe.model_catalog, "append_fetched", _boom)
    w = _win(no_probe, parent_widget)
    w._on_models("codex")
    w._models.run()
    assert "read-only fs" in w._log.toPlainText()


def test_models_result_after_close_is_dropped(no_probe, no_start, parent_widget,
                                              models_toml, monkeypatch):
    _write_choices(models_toml, ["A"])
    before = models_toml.read_bytes()
    w = _win(no_probe, parent_widget)
    gen = w._models_gen
    w.close()
    w._on_models_result(gen, _ok_codex(models=("A", "B")))
    assert models_toml.read_bytes() == before


def test_opening_or_ok_row_without_flag_does_not_fetch(parent_widget, monkeypatch):
    import gui.backend_status_window as mod
    started = []
    monkeypatch.setattr(mod._ModelsWorker, "start", lambda self: started.append(self))
    monkeypatch.setattr(mod, "_ProbeWorker", MagicMock())
    w = mod.BackendStatusWindow(MagicMock(), parent_widget)
    w._on_row(w._generation, _st("codex", binary_state="ok"))
    assert started == []


def _install(w, eid="pi"):
    w._paint(_st(eid, prereq_state="ok", binary_state="ok",
                 install=("npm", "i", "-g", "x")))
    w._on_action(eid)


def _finish_install(w, rc):
    """npm の終了を模す。実際と同じく done（今のインストールの番号）→ finished（_forget）の順。"""
    cmd = w._cmd
    w._on_install_done(w._install_token, rc)
    w._forget(cmd)


def test_auto_fetch_once_after_successful_install(no_probe, no_start, parent_widget):
    w = _win(no_probe, parent_widget)
    _install(w)
    assert len(no_start["cmd"]) == 1
    _finish_install(w, 0)
    w._on_row(w._generation, _st("codex", binary_state="ok"))     # 別エンジン
    assert no_start["models"] == []
    w._on_row(w._generation, _st("pi", binary_state="ok"))
    assert len(no_start["models"]) == 1
    assert no_start["models"][0]._engine_id == "pi"
    w._forget(w._models)
    w._on_row(w._generation, _st("pi", binary_state="ok"))
    assert len(no_start["models"]) == 1


def test_failed_install_does_not_fetch_and_unbusies(no_probe, no_start, parent_widget):
    from llm_backend.engines import engine_by_id, engine_label
    w = _win(no_probe, parent_widget)
    _install(w)
    _finish_install(w, 1)
    w._on_row(w._generation, _st("pi", binary_state="ok"))
    assert no_start["models"] == []
    assert all(b.isEnabled() for b in _buttons(w))
    w._on_probe_done(w._generation)
    skipped = no_probe.tr("backend.status.models.install_skipped",
                          name=engine_label(engine_by_id("pi")))
    assert skipped not in w._log.toPlainText()


def test_install_still_missing_skips_and_unbusies(no_probe, no_start, parent_widget):
    from llm_backend.engines import engine_by_id, engine_label
    w = _win(no_probe, parent_widget)
    _install(w)
    _finish_install(w, 0)
    w._on_row(w._generation, _st("pi", binary_state="missing"))
    assert not any(b.isEnabled() for b in _buttons(w))
    w._on_probe_done(w._generation)
    assert no_start["models"] == []
    assert all(b.isEnabled() for b in _buttons(w))
    expected = no_probe.tr("backend.status.models.install_skipped",
                           name=engine_label(engine_by_id("pi")))
    assert expected in w._log.toPlainText()


def test_busy_during_install(no_probe, no_start, parent_widget):
    w = _win(no_probe, parent_widget)
    _install(w)
    assert not any(b.isEnabled() for b in _buttons(w))
    w._paint(_st("codex", binary_state="ok"))
    assert not any(b.isEnabled() for b in _buttons(w))
    w._on_ping("mock")
    assert no_start["ping"] == []


def test_busy_during_models_and_released_on_finish(no_probe, no_start, parent_widget):
    w = _win(no_probe, parent_widget)
    w._on_models("codex")
    assert not any(b.isEnabled() for b in _buttons(w))
    w._paint(_st("codex", binary_state="ok"))
    assert not any(b.isEnabled() for b in _buttons(w))
    w._forget(w._models)
    assert all(b.isEnabled() for b in _buttons(w))


def test_busy_during_ping(no_probe, no_start, parent_widget, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(no_probe, "build_backend", lambda k, s: MagicMock())
    w = _win(no_probe, parent_widget)
    w._on_ping("mock")
    assert len(no_start["ping"]) == 1
    assert not any(b.isEnabled() for b in _buttons(w))
    w._on_models("codex")
    w._run_install(["npm", "i", "-g", "x"], engine_id="pi")
    assert no_start["models"] == [] and no_start["cmd"] == []
    w._on_ping_result(0, SimpleNamespace(ok=True, elapsed=0.1, text="pong"))
    assert all(b.isEnabled() for b in _buttons(w))


def test_models_paths_never_ping_or_build(no_probe, no_start, parent_widget,
                                          models_toml, monkeypatch):
    calls = []
    monkeypatch.setattr(no_probe, "ping_backend", lambda b: calls.append("ping"))
    monkeypatch.setattr(no_probe, "build_backend", lambda *a: calls.append("build"))
    monkeypatch.setattr(no_probe.model_catalog, "fetch_models",
                        lambda eid: _ok_codex(models=("Z",)))
    w = _win(no_probe, parent_widget)
    w._on_models("codex")
    w._models.run()
    w._forget(w._models)
    _install(w, "codex")
    _finish_install(w, 0)
    w._on_row(w._generation, _st("codex", binary_state="ok"))
    w._models.run()
    assert calls == []


def _still_running(monkeypatch, mod, worker):
    """kill / wait しても終わらないワーカーを模す（win32 の cmd.exe /c npm.cmd で子の node が
    stdout を握ったまま、など）。"""
    monkeypatch.setattr(mod.BackendStatusWindow, "_running",
                        staticmethod(lambda wk: wk is not None and wk is worker))
    monkeypatch.setattr(worker, "wait", lambda *a: False)


def test_close_keeps_a_still_running_install_busy_and_ignores_its_late_done(
        no_probe, no_start, parent_widget, monkeypatch):
    """閉じる → 旧 npm が終わらない → 再表示 → 別の操作 → 旧 done、の順。"""
    w = _win(no_probe, parent_widget)
    _install(w)
    old, old_token = w._cmd, w._install_token
    _still_running(monkeypatch, no_probe, old)
    w.close()
    assert w._cmd is old                        # 枠を残す
    w.refresh()                                 # 再表示（_open_backend_status と同じ）
    assert not any(b.isEnabled() for b in _buttons(w))
    assert not w._refresh_btn.isEnabled()
    w._on_models("codex")
    w._on_ping("mock")
    w._on_action("pi")
    assert no_start["models"] == [] and no_start["ping"] == [] and len(no_start["cmd"]) == 1
    w._on_install_done(old_token, 0)            # 閉じる前のインストールの完了
    assert w._models_after_install is None
    assert not any(b.isEnabled() for b in _buttons(w))
    monkeypatch.setattr(no_probe.BackendStatusWindow, "_running", staticmethod(lambda wk: False))
    w._forget(old)                              # 旧ワーカーがやっと終わった
    assert all(b.isEnabled() for b in _buttons(w))
    assert w._refresh_btn.isEnabled()


def test_late_done_of_an_old_install_does_not_touch_the_current_one(
        no_probe, no_start, parent_widget):
    w = _win(no_probe, parent_widget)
    _install(w)
    old_token = w._install_token
    w._on_install_done(old_token, 1)
    w._forget(w._cmd)
    _install(w, "codex")
    new_token = w._install_token
    assert new_token != old_token
    w._on_install_done(old_token, 0)            # 古い完了が遅れてもう一度届く
    assert w._install_eid == "codex" and w._install_token == new_token
    assert w._models_after_install is None
    _finish_install(w, 0)
    assert w._models_after_install == "codex"


def test_close_keeps_a_still_running_ping_busy(no_probe, no_start, parent_widget,
                                              monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(no_probe, "build_backend", lambda k, s: MagicMock())
    w = _win(no_probe, parent_widget)
    w._on_ping("mock")
    old = w._ping
    _still_running(monkeypatch, no_probe, old)
    w.close()
    assert w._ping is old
    assert not any(b.isEnabled() for b in _buttons(w))
    w._on_ping_result(0, SimpleNamespace(ok=True, elapsed=0.1, text="pong"))
    assert all(b.isEnabled() for b in _buttons(w))


def test_recheck_is_disabled_while_busy_and_while_probing(no_probe, no_start,
                                                         parent_widget):
    """再確認も --version 等でエンジンを起動するので、npm の実行中に押させない。"""
    w = _win(no_probe, parent_widget)
    assert w._refresh_btn.isEnabled()
    _install(w)
    assert not w._refresh_btn.isEnabled()
    w._paint(_st("codex", binary_state="ok"))
    assert not w._refresh_btn.isEnabled()
    _finish_install(w, 1)
    assert w._refresh_btn.isEnabled()
    w._probe = MagicMock()                      # 判定中
    w._apply_busy()
    assert not w._refresh_btn.isEnabled()
    w._forget(w._probe)
    assert w._refresh_btn.isEnabled()
