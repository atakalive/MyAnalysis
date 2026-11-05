"""Tests for common.filelock — cross-platform advisory locking."""

from __future__ import annotations

import errno
import subprocess
import sys
import time
import types

import pytest

from common import filelock
from common.filelock import LockTimeout, exclusive_lock, try_exclusive_lock
from common.paths import repo_root


class TestExclusiveLock:
    def test_lock_creates_file_and_yields(self, tmp_path):
        lock_file = tmp_path / "test.lock"
        entered = False
        with exclusive_lock(lock_file):
            entered = True
            assert lock_file.exists()
        assert entered

    def test_lock_releases_on_exit(self, tmp_path):
        lock_file = tmp_path / "test.lock"
        with exclusive_lock(lock_file):
            pass
        # After release, a second lock should succeed immediately.
        with exclusive_lock(lock_file):
            pass

    def test_lock_releases_on_exception(self, tmp_path):
        lock_file = tmp_path / "test.lock"
        try:
            with exclusive_lock(lock_file):
                raise ValueError("test")
        except ValueError:
            pass
        # Lock should be released even after exception.
        with exclusive_lock(lock_file):
            pass


# ---------------------------------------------------------------- 待たないロック（Issue #125）


class TestTryExclusiveLock:
    def test_free_lock_yields_true(self, tmp_path):
        with try_exclusive_lock(tmp_path / "t.lock") as got:
            assert got is True

    def test_held_by_exclusive_lock_yields_false_without_waiting(self, tmp_path):
        p = tmp_path / "t.lock"
        with exclusive_lock(p):
            t0 = time.monotonic()
            with try_exclusive_lock(p) as got:
                assert got is False
            assert time.monotonic() - t0 < 1.0

    def test_two_try_locks_second_is_false(self, tmp_path):
        p = tmp_path / "t.lock"
        with try_exclusive_lock(p) as a:
            assert a is True
            with try_exclusive_lock(p) as b:
                assert b is False

    def test_released_on_exit(self, tmp_path):
        p = tmp_path / "t.lock"
        with try_exclusive_lock(p) as got:
            assert got
        with try_exclusive_lock(p) as got:
            assert got
        with exclusive_lock(p):
            pass

    def test_released_on_exception(self, tmp_path):
        p = tmp_path / "t.lock"
        with pytest.raises(ValueError), try_exclusive_lock(p) as got:
            assert got
            raise ValueError("x")
        with try_exclusive_lock(p) as got:
            assert got

    def test_false_does_not_release_the_holder(self, tmp_path):
        p = tmp_path / "t.lock"
        with exclusive_lock(p):
            with try_exclusive_lock(p) as got:
                assert got is False
            # 失敗したハンドルを閉じても、持ち主のロックは残っている
            with try_exclusive_lock(p) as again:
                assert again is False
        with try_exclusive_lock(p) as got:
            assert got is True

    def test_missing_parent_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError), try_exclusive_lock(tmp_path / "nope" / "t.lock"):
            pass

    def test_non_contention_error_is_raised(self, tmp_path, monkeypatch):
        def bad(*a, **k):
            raise OSError(errno.EINVAL, "invalid")
        if sys.platform == "win32":
            fake = types.SimpleNamespace(LK_NBLCK=filelock.msvcrt.LK_NBLCK,
                                         LK_UNLCK=filelock.msvcrt.LK_UNLCK, locking=bad)
            monkeypatch.setattr(filelock, "msvcrt", fake)
        else:
            fake = types.SimpleNamespace(LOCK_EX=filelock.fcntl.LOCK_EX,
                                         LOCK_NB=filelock.fcntl.LOCK_NB,
                                         LOCK_UN=filelock.fcntl.LOCK_UN, flock=bad)
            monkeypatch.setattr(filelock, "fcntl", fake)
        with pytest.raises(OSError) as ei, try_exclusive_lock(tmp_path / "t.lock"):
            pass
        assert ei.value.errno == errno.EINVAL

    def test_held_by_another_process(self, tmp_path):
        p = tmp_path / "t.lock"
        code = ("import sys\n"
                "from common.filelock import exclusive_lock\n"
                f"with exclusive_lock({str(p)!r}):\n"
                "    print('ready', flush=True)\n"
                "    sys.stdin.read()\n")
        proc = subprocess.Popen([sys.executable, "-c", code], cwd=str(repo_root()),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert proc.stdout.readline().strip() == "ready"
            with try_exclusive_lock(p) as got:
                assert got is False
        finally:
            proc.stdin.close()
            proc.wait(timeout=30)
        with try_exclusive_lock(p) as got:
            assert got is True


@pytest.mark.skipif(sys.platform != "win32", reason="msvcrt の待ちの上限は win32 だけ")
def test_exclusive_lock_timeout_raises_lock_timeout(tmp_path, monkeypatch):
    calls = []

    def deadlock(fd, mode, n):
        calls.append(mode)
        raise OSError(errno.EDEADLOCK, "Resource deadlock avoided")
    fake = types.SimpleNamespace(LK_LOCK=filelock.msvcrt.LK_LOCK,
                                 LK_UNLCK=filelock.msvcrt.LK_UNLCK, locking=deadlock)
    monkeypatch.setattr(filelock, "msvcrt", fake)
    with pytest.raises(LockTimeout) as ei, exclusive_lock(tmp_path / "t.lock"):
        pass
    assert isinstance(ei.value, OSError)
    assert ei.value.errno == errno.EDEADLOCK
    assert len(calls) == 3


@pytest.mark.skipif(sys.platform != "win32", reason="msvcrt の待ちの上限は win32 だけ")
def test_exclusive_lock_other_error_is_not_timeout(tmp_path, monkeypatch):
    def invalid(fd, mode, n):
        raise OSError(errno.EINVAL, "invalid")
    fake = types.SimpleNamespace(LK_LOCK=filelock.msvcrt.LK_LOCK,
                                 LK_UNLCK=filelock.msvcrt.LK_UNLCK, locking=invalid)
    monkeypatch.setattr(filelock, "msvcrt", fake)
    with pytest.raises(OSError) as ei, exclusive_lock(tmp_path / "t.lock"):
        pass
    assert not isinstance(ei.value, LockTimeout)
    assert ei.value.errno == errno.EINVAL
