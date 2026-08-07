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


def test_install_button_only_when_binary_is_missing(no_probe, parent_widget):
    w = _win(no_probe, parent_widget)
    w._paint(_st("pi", prereq_state="ok", binary_state="missing",
                 install=("npm", "i", "-g", "x")))
    act = w._rows["pi"]["action"]
    assert not act.isHidden()
    assert act.text() == no_probe.tr("backend.status.btn.install")


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


def test_install_wraps_cmd_shims_for_windows(no_probe, parent_widget, monkeypatch):
    """win32 の npm.cmd は CreateProcess で直接起動できない → cmd.exe /c 経由。"""
    from llm_backend import preflight
    monkeypatch.setattr(preflight.sys, "platform", "win32")
    assert preflight._wrap(["npm.cmd", "i"]) == ["cmd.exe", "/c", "npm.cmd", "i"]
    assert preflight._wrap(["npm", "i"]) == ["npm", "i"]


def test_login_opens_a_new_console_on_windows(no_probe, parent_widget, monkeypatch):
    import subprocess
    w = _win(no_probe, parent_widget)
    monkeypatch.setattr(no_probe.sys, "platform", "win32")
    seen = {}

    def _popen(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return MagicMock()
    monkeypatch.setattr(no_probe.subprocess, "Popen", _popen)
    w._launch_login(["pi"])
    assert seen["cmd"] == ["pi"]
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
