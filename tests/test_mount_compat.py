"""Tests for common.mount_compat.install — the global os.path.realpath shim.

Same failure family as common.paths.safe_resolve (Issue #68): PIL's Image.open/
Image.save call os.path.realpath internally, which raises OSError [WinError 1005]
on WinFsp/rclone mounts. The shim wraps os.path.realpath so those library-internal
(and agent-written) calls degrade to os.path.abspath on the mount, while forwarding
strict=True unchanged.

We simulate the mount by monkeypatching os.path.realpath to raise. An autouse
fixture snapshots and restores the real os.path.realpath so the process-wide patch
installed here never leaks into other tests in the session.
"""
from __future__ import annotations

import os

import pytest

from common import mount_compat


@pytest.fixture(autouse=True)
def _restore_realpath():
    orig = os.path.realpath
    try:
        yield
    finally:
        os.path.realpath = orig


def _raise_1005(*_a, **_k):
    # Mirrors what rclone/WinFsp raise when GetFinalPathNameByHandle is unsupported.
    raise OSError(1005, "The volume does not contain a recognized file system")


def test_install_falls_back_to_abspath_on_oserror(monkeypatch, tmp_path):
    monkeypatch.setattr(os.path, "realpath", _raise_1005)
    mount_compat.install()
    p = tmp_path / "sub" / ".." / "x.png"
    got = os.path.realpath(p)                 # must not raise
    assert got == os.path.abspath(p)          # lexical fallback ('..' collapsed)


def test_install_passthrough_on_normal_fs(tmp_path):
    real = os.path.realpath                    # the genuine stdlib realpath
    mount_compat.install()
    p = tmp_path / "sub" / ".." / "x.png"
    # On success the shim returns the underlying realpath result byte-identically
    # (compare against `real`, not abspath, so macOS /tmp symlink doesn't trip us).
    assert os.path.realpath(p) == real(p)


def test_install_reraises_on_strict(monkeypatch, tmp_path):
    def raise_enoent(path, *, strict=False):
        raise FileNotFoundError(2, "missing")

    monkeypatch.setattr(os.path, "realpath", raise_enoent)
    mount_compat.install()
    # strict=True callers must still see their error (contract preserved).
    with pytest.raises(FileNotFoundError):
        os.path.realpath(tmp_path / "nope", strict=True)
    # strict=False degrades to abspath instead of crashing.
    assert os.path.realpath(tmp_path / "nope") == os.path.abspath(tmp_path / "nope")


def test_install_is_idempotent(monkeypatch):
    monkeypatch.setattr(os.path, "realpath", _raise_1005)
    mount_compat.install()
    first = os.path.realpath
    assert getattr(first, "_mount_compat_shim", False) is True
    mount_compat.install()
    assert os.path.realpath is first           # marker guard prevents re-wrapping


def test_shim_lets_pil_open_and_save_on_simulated_mount(monkeypatch, tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    monkeypatch.setattr(os.path, "realpath", _raise_1005)
    mount_compat.install()
    target = tmp_path / "pic.png"
    # Without the shim both of these raise OSError(1005): PIL calls realpath(path).
    Image.new("RGB", (4, 4), (10, 20, 30)).save(target)
    assert target.exists()
    with Image.open(target) as im:
        assert im.size == (4, 4)
