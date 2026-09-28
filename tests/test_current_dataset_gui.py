"""GUI tests for window-level current dataset (offscreen Qt)."""
import threading
import time

import pytest


@pytest.fixture
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_dataset_tab_switch_updates_current(qapp):
    """dataset 付きタブ切替で current_dataset 更新 + dataset_changed 発火。"""
    from gui.window import ToolWindow
    from gui.tab import AnalysisTab

    win = ToolWindow()
    win.show()

    tab_a = AnalysisTab("a")
    tab_a.session_spec = {"kind": "analysis", "name": "a", "dataset": "ds_a"}
    win.add_tab(tab_a)

    tab_b = AnalysisTab("b")
    tab_b.session_spec = {"kind": "analysis", "name": "b", "dataset": "ds_b"}
    win.add_tab(tab_b)

    signals = []
    win.dataset_changed.connect(lambda ds: signals.append(ds))

    win.set_active_tab("a")
    assert win.current_dataset == "ds_a"

    win.set_active_tab("b")
    assert win.current_dataset == "ds_b"
    assert "ds_b" in signals


def test_datasetless_tab_switches_current_to_none(qapp):
    """明示選択モデル: dataset 無しタブ（None グループ）へ切替えると current は None。

    旧 sticky（追従保持）は廃止。current_dataset は「明示的に選択された
    データセット」で、None グループを前面化すれば None になる。"""
    from gui.window import ToolWindow
    from gui.tab import AnalysisTab

    win = ToolWindow()
    win.show()

    tab_ds = AnalysisTab("ds_tab")
    tab_ds.session_spec = {"kind": "analysis", "name": "ds_tab", "dataset": "ds_x"}
    win.add_tab(tab_ds)

    tab_plain = AnalysisTab("plain")
    tab_plain.session_spec = None
    win.add_tab(tab_plain)   # → None グループ（実 ds_x グループと共存）

    win.set_active_tab("ds_tab")
    assert win.current_dataset == "ds_x"

    win.set_active_tab("plain")
    assert win.current_dataset is None  # 明示選択（sticky ではない）


def test_set_active_tab_selects_dataset(qapp):
    """figure タブへの set_active_tab がその dataset グループを前面化し current を更新。"""
    from gui.window import ToolWindow
    from gui.tab import AnalysisTab

    win = ToolWindow()
    win.show()

    tab = AnalysisTab("t")
    tab.session_spec = {"kind": "figure", "name": "t", "dataset": "ds_y", "figure": "/tmp/x.png"}
    win.add_tab(tab)

    win.set_active_tab("t")
    assert win.current_dataset == "ds_y"
    win.notify_chat_dataset()  # チャット push のみ、current は変えない
    assert win.current_dataset == "ds_y"


# --------------------------------------------------------------------------- #
# File → データセットを新規登録 — 実ストレージ（tmp の JSON）を通す（Issue #95）
# --------------------------------------------------------------------------- #
@pytest.fixture
def register_stubs(monkeypatch, tmp_path):
    """入力ダイアログ・ディレクトリ選択・完了通知を stub 化して返す。"""
    import json

    import config
    import dataset_registry
    from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

    reg = tmp_path / "datasets.local.json"
    monkeypatch.setattr(dataset_registry, "registry_path", lambda: reg)

    ds_dir = tmp_path / "sample_dataset"
    ds_dir.mkdir()

    shown = {"info": 0, "critical": 0}
    calls: list[str] = []
    defaults: list[str] = []
    # 名前入力への返答。空なら既定値（= フォルダ名）をそのまま受け入れる。
    answers: list[tuple[str, bool]] = []

    def get_text(*a, **k):
        calls.append("name")
        defaults.append(k.get("text", ""))
        return answers.pop(0) if answers else (k.get("text", ""), True)

    def get_dir(*a, **k):
        calls.append("dir")
        return str(ds_dir)

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(get_text))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(get_dir))
    monkeypatch.setattr(
        QMessageBox, "information",
        staticmethod(lambda *a, **k: shown.__setitem__("info", shown["info"] + 1)),
    )
    monkeypatch.setattr(
        QMessageBox, "critical",
        staticmethod(lambda *a, **k: shown.__setitem__("critical", shown["critical"] + 1)),
    )

    class S:
        pass

    s = S()
    s.reg = reg
    s.ds_dir = ds_dir
    s.shown = shown
    s.calls = calls
    s.defaults = defaults
    s.answers = answers
    s.read = lambda: json.loads(reg.read_text(encoding="utf-8-sig"))
    s.config = config
    return s


def test_gui_register_writes_json_and_memory(qapp, register_stubs):
    from gui.window import ToolWindow

    win = ToolWindow()
    win._register_dataset()

    assert register_stubs.read() == {
        "sample_dataset": {
            __import__("socket").gethostname().upper(): str(register_stubs.ds_dir)
        }
    }
    assert "sample_dataset" in register_stubs.config.DATASETS
    assert win.current_dataset == "sample_dataset"
    assert register_stubs.shown["info"] == 1
    assert register_stubs.shown["critical"] == 0
    # フォルダ選択が先、名前の既定値はフォルダ名
    assert register_stubs.calls == ["dir", "name"]
    assert register_stubs.defaults == ["sample_dataset"]


def test_gui_register_dir_cancel_asks_nothing(qapp, register_stubs, monkeypatch):
    import config
    from PySide6.QtWidgets import QFileDialog
    from gui.window import ToolWindow

    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: "")
    )

    win = ToolWindow()
    before = dict(config.DATASETS)
    win._register_dataset()

    assert register_stubs.calls == []  # 名前入力は出ない
    assert config.DATASETS == before
    assert not register_stubs.reg.exists()


def test_gui_register_invalid_name_reprompts(qapp, register_stubs):
    from gui.window import ToolWindow

    register_stubs.answers[:] = [("bad name", True), ("good_name", True)]

    win = ToolWindow()
    win._register_dataset()

    # 無効名はエラー表示後、フォルダを選び直させず入力値を残して再入力
    assert register_stubs.calls == ["dir", "name", "name"]
    assert register_stubs.defaults == ["sample_dataset", "bad name"]
    assert register_stubs.shown["critical"] == 1
    assert register_stubs.shown["info"] == 1
    assert list(register_stubs.read()) == ["good_name"]
    assert win.current_dataset == "good_name"


def test_gui_register_failure_updates_nothing(qapp, register_stubs, monkeypatch):
    import config
    from gui.window import ToolWindow

    def boom(*a, **k):
        raise config.RegistryError("nope")

    monkeypatch.setattr(config, "register_dataset", boom)

    win = ToolWindow()
    before = dict(config.DATASETS)
    before_current = win.current_dataset
    win._register_dataset()

    assert config.DATASETS == before
    assert win.current_dataset == before_current
    assert not register_stubs.reg.exists()
    assert register_stubs.shown["critical"] == 1
    assert register_stubs.shown["info"] == 0


# --------------------------------------------------------------------------- #
# GUI 登録直後の R2 送信（送信のみ・バックグラウンド）（Issue #98）
# --------------------------------------------------------------------------- #
def _wait_until(pred, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for condition")
        time.sleep(0.005)


class _Gate:
    """gate が開くまで戻らない代役。同時実行数と呼出スレッドを記録する。"""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.raise_exc: Exception | None = None
        self.on_call = None                   # 呼ばれた直後に 1 回だけ実行する任意のフック
        self.lock = threading.Lock()
        self.calls: list[int] = []            # 呼ばれたスレッドの ident
        self.running = 0
        self.max_running = 0

    def __call__(self, *args, **kwargs):
        with self.lock:
            self.calls.append(threading.get_ident())
            self.running += 1
            self.max_running = max(self.max_running, self.running)
            hook, self.on_call = self.on_call, None
        try:
            if hook is not None:
                hook()
            self.gate.wait(10.0)
            if self.raise_exc is not None:
                raise self.raise_exc
            return None
        finally:
            with self.lock:
                self.running -= 1


def _stub_push(monkeypatch, register_stubs):
    from gui.window import ToolWindow
    from PySide6.QtWidgets import QMessageBox

    def fake_push(self):
        register_stubs.calls.append("push")
        return True

    monkeypatch.setattr(ToolWindow, "request_config_push", fake_push)
    monkeypatch.setattr(
        QMessageBox, "information",
        staticmethod(lambda *a, **k: register_stubs.calls.append("info")),
    )


def test_gui_register_requests_push_once_before_info(qapp, register_stubs, monkeypatch):
    from gui.window import ToolWindow

    _stub_push(monkeypatch, register_stubs)
    win = ToolWindow()
    win._register_dataset()
    assert register_stubs.calls == ["dir", "name", "push", "info"]


def test_gui_register_failure_does_not_push(qapp, register_stubs, monkeypatch):
    import config
    from gui.window import ToolWindow

    _stub_push(monkeypatch, register_stubs)

    def boom(*a, **k):
        raise config.RegistryError("nope")

    monkeypatch.setattr(config, "register_dataset", boom)
    win = ToolWindow()
    win._register_dataset()
    assert "push" not in register_stubs.calls
    assert register_stubs.shown["critical"] == 1


def test_gui_register_cancel_does_not_push(qapp, register_stubs, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    from gui.window import ToolWindow

    _stub_push(monkeypatch, register_stubs)
    win = ToolWindow()

    register_stubs.answers[:] = [("", False)]
    win._register_dataset()
    assert "push" not in register_stubs.calls

    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: "")
    )
    win._register_dataset()
    assert "push" not in register_stubs.calls


def test_gui_register_succeeds_when_pusher_raises(qapp, register_stubs, monkeypatch):
    from gui.config_push import ConfigPusher
    from gui.window import ToolWindow

    def boom(self):
        raise RuntimeError("pusher")

    monkeypatch.setattr(ConfigPusher, "request", boom)
    win = ToolWindow()
    win._register_dataset()
    assert "sample_dataset" in register_stubs.read()
    assert register_stubs.shown["info"] == 1
    assert register_stubs.shown["critical"] == 0
    assert win.request_config_push() is False


def test_request_config_push_default_gate_creates_no_thread(qapp):
    from gui.window import ToolWindow

    win = ToolWindow()
    assert win.request_config_push() is False
    assert win._config_pusher is not None
    assert win._config_pusher._thread is None


def test_stop_config_pusher_drops_and_recreates(qapp):
    from gui.window import ToolWindow

    win = ToolWindow()
    win.request_config_push()
    old = win._config_pusher
    win._stop_config_pusher()
    assert win._config_pusher is None
    assert old.request() is False
    win._stop_config_pusher()
    win.request_config_push()
    assert win._config_pusher is not None and win._config_pusher is not old


def test_gui_register_push_runs_in_background_and_close_is_bounded(
        qapp, register_stubs, monkeypatch):
    import config_share
    from PySide6.QtWidgets import QMessageBox
    from gui.config_push import ConfigPusher
    from gui.window import ToolWindow

    monkeypatch.setattr(ConfigPusher, "DEFAULT_WAIT_S", 0.2)
    monkeypatch.setattr(config_share, "autosync_enabled", lambda: True)
    g = _Gate()
    monkeypatch.setattr(config_share, "try_push", g)
    warned = {"n": 0}
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **k: warned.__setitem__("n", warned["n"] + 1)),
    )

    win = ToolWindow()
    pusher = None
    try:
        win._register_dataset()                     # gate 閉のまま返る
        _wait_until(lambda: len(g.calls) == 1)
        assert g.calls[0] != threading.get_ident()
        win._register_dataset()
        pusher = win._config_pusher
        assert pusher.has_pending()
        t0 = time.monotonic()
        win.show()
        win.close()
        assert time.monotonic() - t0 < 2.0
        assert win._config_pusher is None
        assert pusher._thread.is_alive() and pusher._thread.daemon
    finally:
        g.gate.set()
        if pusher is not None and pusher._thread is not None:
            pusher._thread.join(5.0)
            assert not pusher._thread.is_alive()
    assert len(g.calls) == 1                        # pending は stop で破棄
    assert register_stubs.shown["critical"] == 0
    assert warned["n"] == 0
    assert register_stubs.shown["info"] == 2


def test_gui_register_push_exception_is_silent(qapp, register_stubs, monkeypatch):
    import config_share
    from PySide6.QtWidgets import QMessageBox
    from gui.window import ToolWindow

    monkeypatch.setattr(config_share, "autosync_enabled", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("push")

    monkeypatch.setattr(config_share, "try_push", boom)
    warned = {"n": 0}
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **k: warned.__setitem__("n", warned["n"] + 1)),
    )
    win = ToolWindow()
    win._register_dataset()
    _wait_until(lambda: not win._config_pusher.is_running())
    assert register_stubs.shown["critical"] == 0
    assert warned["n"] == 0
    assert register_stubs.shown["info"] == 1
    assert "sample_dataset" in register_stubs.read()


def test_window_generations_serialize_pushes(qapp, monkeypatch):
    import config_share
    from gui.config_push import ConfigPusher
    from gui.window import ToolWindow

    monkeypatch.setattr(ConfigPusher, "DEFAULT_WAIT_S", 0.05)
    monkeypatch.setattr(config_share, "autosync_enabled", lambda: True)
    g = _Gate()
    monkeypatch.setattr(config_share, "sync", g)    # try_push は実物（push ロックを取る）

    pushers = []
    try:
        win1 = ToolWindow()
        assert win1.request_config_push() is True
        pushers.append(win1._config_pusher)
        _wait_until(lambda: len(g.calls) == 1)
        p1 = win1._config_pusher
        win1._stop_config_pusher()                  # Tier 3 の停止（間に合わない）
        r1 = win1.request_config_push()             # 失敗時の復帰経路
        pushers.append(win1._config_pusher)
        win2 = ToolWindow()                         # 成功時の新ウィンドウ
        r2 = win2.request_config_push()
        pushers.append(win2._config_pusher)
        time.sleep(0.3)
        assert r1 is True and r2 is True
        assert win1._config_pusher is not p1
        assert len(g.calls) == 1                    # 新しい 2 本は push ロック待ち
    finally:
        g.gate.set()
        for p in pushers:
            if p is None:
                continue
            p.stop(5.0)
            if p._thread is not None:
                p._thread.join(5.0)
                assert not p._thread.is_alive()
    assert len(g.calls) == 3
    assert g.max_running == 1
