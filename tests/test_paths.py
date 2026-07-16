"""Tests for common.paths.safe_resolve — the WinFsp/rclone-safe resolve() drop-in.

WinFsp/rclone mounts don't implement GetFinalPathNameByHandle, so Path.resolve()
raises OSError [WinError 1005] there. safe_resolve must degrade to os.path.abspath
on that OSError while staying byte-identical to resolve() on a normal filesystem.

We simulate the mount by monkeypatching os.path.realpath (which pathlib's flavour
uses under Path.resolve()) to raise — os.path.abspath does not go through
realpath, so the fallback still works. This reproduces the failure portably on
POSIX CI too.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from common.paths import safe_resolve


def _winfsp_realpath_stub(*_args, **_kwargs):
    # Mirrors what rclone/WinFsp raise when GetFinalPathNameByHandle is unsupported.
    raise OSError(1005, "The volume does not contain a recognized file system")


def test_safe_resolve_equals_resolve_on_normal_fs(tmp_path):
    # On a normal filesystem safe_resolve is exactly Path.resolve() — so every
    # existing assertion that compares against `.resolve()` keeps holding.
    p = tmp_path / "sub" / ".." / "file.txt"
    assert safe_resolve(p) == p.resolve()
    assert safe_resolve(str(p)) == p.resolve()


def test_safe_resolve_falls_back_when_realpath_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(os.path, "realpath", _winfsp_realpath_stub)
    # Precondition: plain resolve() now blows up like it does on the mount.
    with pytest.raises(OSError):
        (tmp_path / "x").resolve()

    p = tmp_path / "sub" / ".." / "file.txt"
    got = safe_resolve(p)  # must not raise
    assert got == Path(os.path.abspath(p))
    assert got.is_absolute()
    # '..' is collapsed lexically, same target resolve() would have reached.
    assert got == tmp_path / "file.txt"


def test_safe_resolve_containment_holds_under_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(os.path, "realpath", _winfsp_realpath_stub)
    base = tmp_path / "dataset"
    inside = base / "_work" / "figures"
    outside = tmp_path / "other"
    # The containment checks this helper replaces must still be correct.
    assert safe_resolve(inside).is_relative_to(safe_resolve(base))
    assert not safe_resolve(outside).is_relative_to(safe_resolve(base))
    # A '..' escape is still caught after lexical normalization.
    escape = base / ".." / "other"
    assert not safe_resolve(escape).is_relative_to(safe_resolve(base))
