"""ConfigPusher（gui/config_push.py）の Qt 非依存テストと機械ガード（Issue #98）。

push 本体は config_share.try_push の代役（ゲート付きスタブ）に差し替える。worker を走らせる
テストは reap_pushers fixture の teardown（テスト本体の成否に関わらず走る）でゲートを開け、
stop し、worker スレッドを join して生存していないことを確認する — デーモンスレッドを後続
テストへ漏らさず、元の失敗を後始末の失敗で覆わない。
"""

from __future__ import annotations

import ast
import threading
import time
from pathlib import Path

import pytest

import config_share
from gui.config_push import THREAD_NAME, ConfigPusher

REPO = Path(__file__).resolve().parent.parent


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


@pytest.fixture
def reap_pushers(monkeypatch):
    """このテストで作られた ConfigPusher とゲートを追跡し、成否に関わらず worker を回収する。

    ゲートは戻り値の関数で作る（作ったものは全て追跡される）。teardown: 全ゲートを開ける →
    全 pusher を stop → THREAD_NAME の生存スレッドを全て join し、生存していないことを確認。
    """
    pushers: list[ConfigPusher] = []
    gates: list[_Gate] = []
    orig_init = ConfigPusher.__init__

    def tracking_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        pushers.append(self)

    monkeypatch.setattr(ConfigPusher, "__init__", tracking_init)

    def new_gate() -> _Gate:
        g = _Gate()
        gates.append(g)
        return g

    yield new_gate
    for g in gates:
        g.gate.set()
    for p in pushers:
        p.stop(wait_s=5.0)
    leftovers = [t for t in threading.enumerate() if t.name == THREAD_NAME]
    for t in leftovers:
        t.join(5.0)
    assert not [t for t in leftovers if t.is_alive()]


@pytest.fixture
def pusher(reap_pushers, monkeypatch):
    gate = reap_pushers()
    logs: list[str] = []
    monkeypatch.setattr(config_share, "autosync_enabled", lambda: True)
    monkeypatch.setattr(config_share, "try_push", gate)
    monkeypatch.setattr(config_share, "_log_debug", logs.append)
    p = ConfigPusher()

    class P:
        pass

    ctx = P()
    ctx.p = p
    ctx.gate = gate
    ctx.logs = logs
    return ctx


# --------------------------------------------------------------------------- #
# ConfigPusher
# --------------------------------------------------------------------------- #
def test_request_false_when_gate_closed(pusher, monkeypatch):
    monkeypatch.setattr(config_share, "autosync_enabled", lambda: False)
    assert pusher.p.request() is False
    assert pusher.p._thread is None
    assert pusher.gate.calls == []


def test_request_false_when_gate_raises(pusher, monkeypatch):
    def boom():
        raise RuntimeError("gate")

    monkeypatch.setattr(config_share, "autosync_enabled", boom)
    assert pusher.p.request() is False


def test_push_runs_on_named_daemon_thread(pusher):
    p, gate = pusher.p, pusher.gate
    assert p.request() is True
    _wait_until(lambda: len(gate.calls) == 1)
    assert gate.calls[0] != threading.get_ident()
    assert p._thread.daemon is True
    assert p._thread.name == THREAD_NAME
    assert p.is_running()


def test_rerequests_coalesce_into_one_followup(pusher):
    p, gate = pusher.p, pusher.gate
    assert p.request() is True
    _wait_until(lambda: len(gate.calls) == 1)
    for _ in range(3):
        assert p.request() is True
        assert p.has_pending()
    gate.gate.set()
    _wait_until(lambda: not p.is_running())
    assert len(gate.calls) == 2
    assert gate.max_running == 1
    assert not p.has_pending()


def test_request_during_push_is_never_lost(pusher):
    p, gate = pusher.p, pusher.gate
    gate.gate.set()
    gate.on_call = p.request
    assert p.request() is True
    _wait_until(lambda: not p.is_running())
    assert len(gate.calls) == 2


def test_worker_exception_is_logged_and_next_request_runs(pusher):
    p, gate = pusher.p, pusher.gate
    gate.raise_exc = RuntimeError("boom")
    gate.gate.set()
    assert p.request() is True
    _wait_until(lambda: not p.is_running())
    gate.raise_exc = None
    assert p.request() is True
    _wait_until(lambda: not p.is_running())
    assert len([m for m in pusher.logs if "background push failed: boom" in m]) == 1
    assert len(gate.calls) == 2


def test_start_failure_returns_false_and_recovers(pusher, monkeypatch):
    p, gate = pusher.p, pusher.gate
    orig = ConfigPusher._make_thread

    class _Unstartable:
        def start(self):
            raise RuntimeError("can't start")

    monkeypatch.setattr(ConfigPusher, "_make_thread", lambda self, push, log: _Unstartable())
    assert p.request() is False
    assert not p.is_running()
    assert p._thread is None
    monkeypatch.setattr(ConfigPusher, "_make_thread", orig)
    gate.gate.set()
    assert p.request() is True
    _wait_until(lambda: not p.is_running())
    assert len(gate.calls) == 1


def test_stop_idempotent_and_rejects_requests(pusher):
    p, gate = pusher.p, pusher.gate
    assert p.stop() is True
    assert p.stop() is True
    assert p.request() is False
    assert gate.calls == []


def test_stop_is_bounded_and_leaves_daemon_running(pusher):
    p, gate = pusher.p, pusher.gate
    assert p.request() is True
    _wait_until(lambda: len(gate.calls) == 1)
    assert p.request() is True                 # pending
    t0 = time.monotonic()
    ok = p.stop(wait_s=0.2)
    elapsed = time.monotonic() - t0
    alive = p._thread.is_alive()
    pending = p.has_pending()
    gate.gate.set()
    p._thread.join(5.0)
    assert ok is False
    assert elapsed < 2.0
    assert alive and p._thread.daemon
    assert not pending
    assert len(gate.calls) == 1                # pending は捨てた
    assert p.request() is False


def test_stop_returns_true_when_push_finishes_in_time(pusher):
    p, gate = pusher.p, pusher.gate
    assert p.request() is True
    _wait_until(lambda: len(gate.calls) == 1)
    timer = threading.Timer(0.1, gate.gate.set)
    timer.start()
    try:
        assert p.stop(wait_s=5.0) is True
        assert not p._thread.is_alive()
    finally:
        timer.cancel()
        timer.join(5.0)


def test_generations_are_serialized_by_push_lock(reap_pushers, monkeypatch):
    monkeypatch.setattr(config_share, "autosync_enabled", lambda: True)
    g = reap_pushers()
    monkeypatch.setattr(config_share, "sync", g)   # try_push は実物（push ロックを取る）
    p1 = ConfigPusher()
    p2 = ConfigPusher()
    assert p1.request() is True
    _wait_until(lambda: len(g.calls) == 1)
    assert p1.stop(wait_s=0.05) is False       # 旧世代の stop が間に合わない
    assert p2.request() is True
    time.sleep(0.3)
    assert len(g.calls) == 1                   # p2 の worker は push ロック待ち
    g.gate.set()
    for p in (p1, p2):
        p.stop(5.0)
        p._thread.join(5.0)
        assert not p._thread.is_alive()
    assert len(g.calls) == 2
    assert g.max_running == 1


# --------------------------------------------------------------------------- #
# 機械ガード（AST）
# --------------------------------------------------------------------------- #
def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(tree: ast.AST) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def _gui_files() -> list[Path]:
    return sorted((REPO / "gui").glob("*.py"))


def test_config_push_module_is_qt_free_and_uses_only_try_push():
    tree = _tree(REPO / "gui" / "config_push.py")
    mods = _imported_modules(tree)
    assert not any(m.startswith("PySide6") for m in mods)
    assert "config" not in mods
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "try_push" in attrs
    assert not attrs & {"try_sync", "sync", "push", "pull", "reload_datasets",
                        "DATASETS", "register_dataset", "unregister_dataset"}
    daemon_threads = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "Thread"
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "threading"
        and any(k.arg == "daemon" and isinstance(k.value, ast.Constant)
                and k.value.value is True for k in n.keywords)
    ]
    assert daemon_threads


def test_other_gui_modules_do_not_sync_directly():
    for path in _gui_files():
        if path.name == "config_push.py":
            continue
        for n in ast.walk(_tree(path)):
            if not isinstance(n, ast.Attribute):
                continue
            assert n.attr not in ("try_push", "try_sync"), path
            if isinstance(n.value, ast.Name) and n.value.id == "config_share":
                assert n.attr not in ("sync", "push", "pull"), path


def _calls_attr(fn: ast.AST, names: tuple[str, ...]) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr in names for n in ast.walk(fn))


def test_gui_registry_mutations_request_push():
    mutating = []
    for path in _gui_files():
        for fn in ast.walk(_tree(path)):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _calls_attr(fn, ("register_dataset", "unregister_dataset")):
                mutating.append((path.name, fn.name))
                assert _calls_attr(fn, ("request_config_push", "_request_config_push")), \
                    (path.name, fn.name)
    assert mutating


def test_qt_integration_stops_config_pusher():
    src = (REPO / "devtools" / "qt_integration.py").read_text(encoding="utf-8")
    assert '"_stop_config_pusher"' in src


def test_core_modules_are_qt_free():
    for rel in ("config_share.py", "tests/conftest.py",
                "tests/test_config_share.py", "tests/test_config_push.py"):
        mods = _imported_modules(_tree(REPO / rel))
        assert not any(m.startswith("PySide6") for m in mods), rel
