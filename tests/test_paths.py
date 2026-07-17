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

from common.paths import atomic_write_bytes, atomic_write_text, safe_resolve


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


def test_atomic_write_text_writes_and_overwrites(tmp_path):
    target = tmp_path / "a.json"
    atomic_write_text(target, "one")
    assert target.read_text(encoding="utf-8") == "one"
    atomic_write_text(target, "two")
    assert target.read_text(encoding="utf-8") == "two"


def test_atomic_write_text_leaves_no_tmp(tmp_path):
    atomic_write_text(tmp_path / "a.json", "payload")
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_text_ignores_stale_fixed_tmp(tmp_path):
    target = tmp_path / "data.json"
    fixed = target.with_suffix(".json.tmp")     # 旧パターンが使う固定名 data.json.tmp
    fixed.write_text("STALE", encoding="utf-8")
    atomic_write_text(target, "payload")
    assert target.read_text(encoding="utf-8") == "payload"
    assert fixed.read_text(encoding="utf-8") == "STALE"   # helper は固定名を触らない＝一意名を使う証明


def test_atomic_write_text_replace_failure_keeps_old_and_cleans_tmp(monkeypatch, tmp_path):
    target = tmp_path / "data.json"
    target.write_text("OLD", encoding="utf-8")
    real_replace = os.replace

    def scoped_boom(src, dst):
        if os.fspath(dst) == os.fspath(target):
            raise OSError("replace failed")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", scoped_boom)
    with pytest.raises(OSError):
        atomic_write_text(target, "NEW")
    assert target.read_text(encoding="utf-8") == "OLD"
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_text_survives_ghost_eexist(monkeypatch, tmp_path):
    calls = {"n": 0}
    real_os_open = os.open

    def flaky(path, flags, mode=0o777):
        name = os.fspath(path)
        if name.startswith(str(tmp_path)) and name.endswith(".tmp") and calls["n"] == 0:
            calls["n"] += 1
            raise FileExistsError(17, "ghost tmp")   # 最初の .tmp 作成がゴースト衝突
        return real_os_open(path, flags, mode)

    monkeypatch.setattr(os, "open", flaky)   # tempfile.mkstemp は同一 os モジュールの os.open を呼ぶ
    target = tmp_path / "data.json"
    atomic_write_text(target, "payload")     # mkstemp が別名で取り直して成功
    assert target.read_text(encoding="utf-8") == "payload"
    assert calls["n"] == 1


def test_atomic_write_text_cleanup_failure_preserves_original_error(monkeypatch, tmp_path):
    target = tmp_path / "data.json"
    target.write_text("OLD", encoding="utf-8")
    real_replace = os.replace

    def scoped_boom(src, dst):
        if os.fspath(dst) == os.fspath(target):
            raise RuntimeError("REPLACE_FAILED")   # 元例外（distinct な型で識別）
        return real_replace(src, dst)

    def boom_unlink(self, *a, **k):
        raise OSError("UNLINK_FAILED")             # 後始末側の失敗

    monkeypatch.setattr(os, "replace", scoped_boom)
    monkeypatch.setattr(Path, "unlink", boom_unlink)
    with pytest.raises(RuntimeError, match="REPLACE_FAILED"):   # OSError にマスクされない
        atomic_write_text(target, "NEW")
    assert target.read_text(encoding="utf-8") == "OLD"


# ---- atomic_write_bytes（PNG 等バイナリ出力用・atomic_write_text のバイナリ版） ----

def test_atomic_write_bytes_writes_and_overwrites(tmp_path):
    target = tmp_path / "a.png"
    atomic_write_bytes(target, b"\x89PNG-one")
    assert target.read_bytes() == b"\x89PNG-one"
    atomic_write_bytes(target, b"two")
    assert target.read_bytes() == b"two"


def test_atomic_write_bytes_leaves_no_tmp(tmp_path):
    atomic_write_bytes(tmp_path / "a.png", b"payload")
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_bytes_ignores_stale_fixed_tmp(tmp_path):
    target = tmp_path / "img.png"
    fixed = target.with_suffix(".png.tmp")          # 旧パターンが使う固定名 img.png.tmp
    fixed.write_bytes(b"STALE")
    atomic_write_bytes(target, b"payload")
    assert target.read_bytes() == b"payload"
    assert fixed.read_bytes() == b"STALE"           # helper は固定名を触らない＝一意名を使う証明


def test_atomic_write_bytes_replace_failure_keeps_old_and_cleans_tmp(monkeypatch, tmp_path):
    target = tmp_path / "img.png"
    target.write_bytes(b"OLD")
    real_replace = os.replace

    def scoped_boom(src, dst):
        if os.fspath(dst) == os.fspath(target):
            raise OSError("replace failed")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", scoped_boom)
    with pytest.raises(OSError):
        atomic_write_bytes(target, b"NEW")
    assert target.read_bytes() == b"OLD"
    assert list(tmp_path.glob("*.tmp")) == []
