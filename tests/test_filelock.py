"""Tests for common.filelock — cross-platform advisory locking."""

from __future__ import annotations

from common.filelock import exclusive_lock


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
