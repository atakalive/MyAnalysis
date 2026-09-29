"""Qt 非依存・純粋な crashlog のユニットテスト（Issue #70）。

各テストで sys.excepthook / threading.excepthook を退避→復元する
（fixture teardown で他テストへ波及させない）。seen は install ごとに
新規生成されるためテスト間で dedup 状態は共有されない。
"""
from __future__ import annotations

import sys
import threading

import pytest

from common import crashlog


@pytest.fixture(autouse=True)
def _restore_hooks():
    prev_sys = sys.excepthook
    prev_thread = threading.excepthook
    # チェーン先をインタプリタ既定に固定する（pytest-qt 等がラップした excepthook へ
    # 委譲すると手動発火した例外がテスト失敗として記録されるため）。
    sys.excepthook = sys.__excepthook__
    threading.excepthook = threading.__excepthook__
    try:
        yield
    finally:
        sys.excepthook = prev_sys
        threading.excepthook = prev_thread


def _make_exc(msg: str = "boom"):
    """明示 raise で (type, exc, tb) を得る。"""
    try:
        raise RuntimeError(msg)
    except RuntimeError as e:
        return type(e), e, e.__traceback__


def _crash_files(tmp_path):
    return list(tmp_path.glob("gui-crash-*.log"))


def test_main_hook_writes(tmp_path):
    """(1) メインフック書き込み。"""
    crashlog.install(log_dir=tmp_path)
    exc_type, exc, tb = _make_exc("boom")
    sys.excepthook(exc_type, exc, tb)
    files = _crash_files(tmp_path)
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "Uncaught exception" in content
    assert "RuntimeError" in content
    assert "boom" in content
    assert "MainThread" in content


def test_chain_delegation(tmp_path):
    """(2) チェーン委譲（素の stub 関数へ委譲される）。"""
    calls = []

    def prev(*a):
        calls.append(a)

    sys.excepthook = prev
    crashlog.install(log_dir=tmp_path)
    exc_type, exc, tb = _make_exc()
    sys.excepthook(exc_type, exc, tb)
    assert calls


def test_thread_hook_writes(tmp_path):
    """(3) thread フック書き込み。"""
    crashlog.install(log_dir=tmp_path)
    exc_type, exc, tb = _make_exc()
    args = threading.ExceptHookArgs(
        (exc_type, exc, tb, threading.current_thread())
    )
    threading.excepthook(args)
    files = _crash_files(tmp_path)
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert threading.current_thread().name in content


def test_write_failure_swallowed(tmp_path):
    """(4) 書き込み失敗の握りつぶし（monkeypatch なし）。"""
    bad = tmp_path / "not_a_dir"
    bad.write_text("x")
    crashlog.install(log_dir=bad)
    exc_type, exc, tb = _make_exc()
    # 例外を伝播しない
    sys.excepthook(exc_type, exc, tb)
    assert _crash_files(tmp_path) == []


def test_idempotent_install(tmp_path):
    """(5) 冪等（多重 install）。"""
    crashlog.install(log_dir=tmp_path)
    h1 = sys.excepthook
    crashlog.install(log_dir=tmp_path)
    assert sys.excepthook is h1


def test_intentional_exit_skipped(tmp_path):
    """(6) 意図的終了はスキップ（記録なし・委譲あり）。"""
    calls = []

    def prev(*a):
        calls.append(a)

    sys.excepthook = prev
    crashlog.install(log_dir=tmp_path)
    sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
    assert _crash_files(tmp_path) == []
    assert calls


def test_site_dedup_message_independent(tmp_path):
    """(7) site dedup（メッセージ非依存）。"""
    crashlog.install(log_dir=tmp_path)

    def boom(m):
        raise RuntimeError(m)

    def get(m):
        try:
            boom(m)
        except RuntimeError as e:
            return type(e), e, e.__traceback__

    for m in ("id=1", "id=2", "id=3"):
        sys.excepthook(*get(m))
    assert len(_crash_files(tmp_path)) == 1


def test_hard_cap_sequential(tmp_path, monkeypatch):
    """(8) ハード上限（逐次）。"""
    monkeypatch.setattr(crashlog, "_MAX_LOG_FILES", 2)
    crashlog.install(log_dir=tmp_path)

    def s1():
        raise RuntimeError("a")

    def s2():
        raise RuntimeError("b")

    def s3():
        raise RuntimeError("c")

    for fn in (s1, s2, s3):
        try:
            fn()
        except RuntimeError as e:
            sys.excepthook(type(e), e, e.__traceback__)
    assert len(_crash_files(tmp_path)) == 2


def test_transient_failure_then_recovery(tmp_path):
    """(9) 一過性失敗→回復で記録される。"""
    seen: set[object] = set()
    lock = threading.Lock()
    exc_type, exc, tb = _make_exc()

    bad = tmp_path / "bad"
    bad.write_text("x")  # 既存ファイル → mkdir 失敗で書けない
    crashlog._record(bad, seen, lock, "T", exc_type, exc, tb)
    assert seen == set()  # 失敗は予約解除

    good = tmp_path / "good"
    crashlog._record(good, seen, lock, "T", exc_type, exc, tb)
    assert len(list(good.glob("gui-crash-*.log"))) == 1
    assert len(seen) == 1


def test_concurrent_same_site_exactly_one(tmp_path):
    """(10) 並行・同一 site → 厳密に 1 件。"""
    crashlog.install(log_dir=tmp_path)
    exc_type, exc, tb = _make_exc()
    n = 30
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()
        sys.excepthook(exc_type, exc, tb)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(_crash_files(tmp_path)) == 1


def test_concurrent_distinct_sites_cap(tmp_path, monkeypatch):
    """(11) 並行・distinct site 多数 + 上限 → cap 以下。"""
    monkeypatch.setattr(crashlog, "_MAX_LOG_FILES", 5)
    crashlog.install(log_dir=tmp_path)

    n = 20
    barrier = threading.Barrier(n)

    # distinct site は別々の行から raise する小関数で作る。単一ソースに n 個の
    # 関数を並べて exec し、各 raise が別 lineno になるようにする（同一行からの
    # raise は同一 site シグネチャになるため）。
    src = "".join(
        f"def site_{i}():\n    raise RuntimeError('site-{i}')\n" for i in range(n)
    )
    ns: dict = {}
    exec(src, ns)  # noqa: S102 — テスト用に別 lineno の関数群を生成
    sites = [ns[f"site_{i}"] for i in range(n)]

    def worker(fn):
        barrier.wait()
        try:
            fn()
        except RuntimeError as e:
            sys.excepthook(type(e), e, e.__traceback__)

    threads = [threading.Thread(target=worker, args=(s,)) for s in sites]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(_crash_files(tmp_path)) <= 5


# ---- ファイルログと起動失敗の通知（Issue #99 C-2） ----


@pytest.fixture
def _clean_root_handlers():
    import logging

    def _strip():
        root = logging.getLogger()
        for h in list(root.handlers):
            if getattr(h, "_myanalysis_logging", False) is True:
                root.removeHandler(h)
                h.close()
    _strip()
    yield
    _strip()


def _ours():
    import logging
    return [h for h in logging.getLogger().handlers
            if getattr(h, "_myanalysis_logging", False) is True]


def test_install_file_logging_is_idempotent(tmp_path, _clean_root_handlers):
    import logging
    import logging.handlers
    from pathlib import Path

    crashlog.install_file_logging(tmp_path)
    crashlog.install_file_logging(tmp_path)
    files = [h for h in _ours() if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(files) == 1
    assert files[0].level == logging.WARNING
    assert Path(files[0].baseFilename) == tmp_path / "myanalysis.log"


@pytest.mark.parametrize("has_stderr", [False, True])
def test_install_file_logging_adds_stream_handler_only_with_stderr(
    tmp_path, monkeypatch, _clean_root_handlers, has_stderr,
):
    import io
    import logging
    import logging.handlers

    monkeypatch.setattr(sys, "stderr", io.StringIO() if has_stderr else None)
    crashlog.install_file_logging(tmp_path)
    streams = [h for h in _ours()
               if isinstance(h, logging.StreamHandler)
               and not isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(streams) == (1 if has_stderr else 0)


def test_install_file_logging_writes_warnings(tmp_path, _clean_root_handlers):
    import logging

    crashlog.install_file_logging(tmp_path)
    logging.getLogger("x").warning("hello")
    for h in _ours():
        h.flush()
    assert "hello" in (tmp_path / "myanalysis.log").read_text(encoding="utf-8")


def test_require_python_exits_when_too_old(monkeypatch):
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(crashlog, "show_fatal", lambda t, x: shown.append((t, x)))
    with pytest.raises(SystemExit) as ei:
        crashlog.require_python((3, 11), current=(3, 10, 12))
    assert ei.value.code == 1
    assert len(shown) == 1
    assert "3.11" in shown[0][1]
    assert "Details" not in shown[0][1]

    shown.clear()
    crashlog.require_python((3, 11), current=(3, 11, 0))
    assert shown == []


def test_report_startup_failure_messages(monkeypatch):
    from dataset_registry import RegistryError

    hooked: list[BaseException] = []
    shown: list[str] = []
    monkeypatch.setattr(sys, "excepthook", lambda t, e, tb: hooked.append(e))
    monkeypatch.setattr(crashlog, "show_fatal", lambda t, x: shown.append(x))

    cases = [
        (RegistryError("x"), ["datasets.local.json"], []),
        (ModuleNotFoundError("No module named 'PySide6'", name="PySide6"),
         ["PySide6", "pip install -r requirements.txt"], []),
        (ImportError("DLL load failed while importing QtWidgets",
                     name="PySide6.QtWidgets"),
         ["DLL load failed"], ["pip install"]),
        (RuntimeError("boom"), ["boom"], []),
    ]
    for exc, must, must_not in cases:
        hooked.clear()
        shown.clear()
        crashlog.report_startup_failure(exc)
        assert hooked == [exc]
        assert len(shown) == 1
        for m in must:
            assert m in shown[0], (exc, m)
        for m in must_not:
            assert m not in shown[0], (exc, m)


def test_show_fatal_falls_back_to_stderr(monkeypatch, capsys):
    monkeypatch.setattr(crashlog, "_windows_message_box", lambda t, x: False)
    crashlog.show_fatal("the-title", "the-text")
    err = capsys.readouterr().err
    assert "the-title" in err and "the-text" in err
