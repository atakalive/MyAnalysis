"""devtools.mount_probe の引数検証と後片付け（Issue #102）。

所有範囲は「この実行が mkdtemp で作った mount_probe-* と、そこに書いた {cond}.bin」だけ。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from common import rclone_paths
from devtools import mount_probe

_REAL_SLEEP = mount_probe.time.sleep


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv(rclone_paths.ENV_LOG, raising=False)
    monkeypatch.delenv(rclone_paths.ENV_CACHE, raising=False)
    log = tmp_path / "rclone.log"
    log.write_bytes(b"")
    w = tmp_path / "w"
    w.mkdir()
    run = ["--dir", str(w), "--log", str(log), "--n", "1", "--pace", "0",
           "--settle", "0", "--size", "64"]
    return tmp_path, log, w, run


def _exit_code(argv) -> int | str | None:
    with pytest.raises(SystemExit) as ei:
        mount_probe.main(argv)
    return ei.value.code


def _run_dirs(w: Path) -> list[Path]:
    return [p for p in w.iterdir() if p.name.startswith("mount_probe-")]


def test_dir_required(setup, capsys):
    _tmp, log, _w, _run = setup
    assert _exit_code(["--log", str(log)]) == 2
    assert "--dir は必須" in capsys.readouterr().err


def test_describe_only_without_dir(setup):
    assert mount_probe.main(["--describe-only"]) == 0


def test_dir_must_exist(setup, capsys):
    tmp, _log, _w, run = setup
    argv = list(run)
    argv[1] = str(tmp / "nope")
    assert _exit_code(argv) == 2
    assert "--dir が既存のディレクトリでない" in capsys.readouterr().err
    assert not (tmp / "nope").exists()


def test_log_required(setup, capsys):
    _tmp, _log, w, run = setup
    i = run.index("--log")
    argv = run[:i] + run[i + 2:]
    assert _exit_code(argv) == 2
    assert "--log（または環境変数 MYANALYSIS_RCLONE_LOG）は必須" in capsys.readouterr().err
    assert os.listdir(w) == []


def test_missing_cache_rejected(setup, capsys):
    tmp, _log, w, run = setup
    assert _exit_code(run + ["--cache", str(tmp / "nocache")]) == 2
    assert "rclone のキャッシュが見つからない" in capsys.readouterr().err
    assert os.listdir(w) == []


def test_run_leaves_others_files(setup):
    _tmp, _log, w, run = setup
    (w / "a_replace.bin").write_bytes(b"other")
    (w / "mount_probe-other").mkdir()
    (w / "mount_probe-other" / "b_inplace.bin").write_bytes(b"other")
    assert mount_probe.main(run) == 0
    assert (w / "a_replace.bin").read_bytes() == b"other"
    assert (w / "mount_probe-other" / "b_inplace.bin").read_bytes() == b"other"
    assert sorted(os.listdir(w)) == ["a_replace.bin", "mount_probe-other"]


def test_file_injected_after_validation_is_kept(setup, monkeypatch):
    _tmp, _log, w, run = setup
    real = mount_probe.read_log_tail
    state = {"first": True}

    def wrapper(log, offset):
        if state["first"]:
            state["first"] = False
            (w / "a_replace.bin").write_bytes(b"other")
        return real(log, offset)

    monkeypatch.setattr(mount_probe, "read_log_tail", wrapper)
    assert mount_probe.main(run) == 0
    assert (w / "a_replace.bin").read_bytes() == b"other"


def test_foreign_file_in_run_dir_kept(setup, monkeypatch, capsys):
    _tmp, _log, w, run = setup
    me = threading.current_thread()

    def sleep(sec):
        if threading.current_thread() is me:
            for d in _run_dirs(w):
                (d / "foreign.txt").write_bytes(b"x")
            return None
        return _REAL_SLEEP(sec)

    monkeypatch.setattr(mount_probe.time, "sleep", sleep)
    assert mount_probe.main(run) == 0
    dirs = _run_dirs(w)
    assert len(dirs) == 1
    assert os.listdir(dirs[0]) == ["foreign.txt"]
    assert "消せなかったので残した" in capsys.readouterr().out


def test_exception_before_first_write(setup, monkeypatch):
    _tmp, _log, w, run = setup

    def boom(i, size):
        raise RuntimeError("boom")

    monkeypatch.setattr(mount_probe, "_payload", boom)
    with pytest.raises(RuntimeError):
        mount_probe.main(run)
    assert os.listdir(w) == []


def test_interrupt_cleans_up(setup, monkeypatch):
    _tmp, _log, w, run = setup
    me = threading.current_thread()

    def sleep(sec):
        if threading.current_thread() is me:
            raise KeyboardInterrupt
        return _REAL_SLEEP(sec)

    monkeypatch.setattr(mount_probe.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        mount_probe.main(run)
    assert os.listdir(w) == []


def test_keep_leaves_run_dir(setup):
    _tmp, _log, w, run = setup
    assert mount_probe.main(run + ["--keep"]) == 0
    dirs = _run_dirs(w)
    assert len(dirs) == 1
    assert sorted(os.listdir(dirs[0])) == sorted(f"{c}.bin" for c in mount_probe.CONDITIONS)


def test_concurrent_runs_same_dir(setup, monkeypatch):
    _tmp, _log, w, run = setup
    (w / "keep.txt").write_bytes(b"keep")
    barrier = threading.Barrier(2)
    ours: set[threading.Thread] = set()

    def sleep(sec):
        if threading.current_thread() in ours:
            barrier.wait(timeout=10)
            return None
        return _REAL_SLEEP(sec)

    monkeypatch.setattr(mount_probe.time, "sleep", sleep)
    results: list = [None, None]
    errors: list = []

    def target(k):
        try:
            results[k] = mount_probe.main(run)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=target, args=(k,)) for k in range(2)]
    ours.update(threads)
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert results == [0, 0]
    assert os.listdir(w) == ["keep.txt"]
    assert (w / "keep.txt").read_bytes() == b"keep"


def test_probe_targets():
    assert mount_probe._probe_targets(
        "D:/w", platform="win32", listdrives=lambda: ["C:\\", "D:\\"]
    ) == (["C:\\", "D:\\", "D:/w"], None)
    assert mount_probe._probe_targets("D:/w", platform="win32", listdrives=None) == (
        ["D:/w"], "Python 3.12 未満のためドライブ一覧は省略")
    assert mount_probe._probe_targets(None, platform="linux") == ([], None)
