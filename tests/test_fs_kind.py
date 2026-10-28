"""common/fs_kind.py — rename-into-place が信頼できる FS かの判定（Issue #96）。

判定を誤ると (a) fragile を local と誤判定 → サイレントなデータ損失、
(b) local を fragile と誤判定 → in-place になるだけで正常動作、と非対称なので、
**判定不能は必ず fragile 側へ倒す**ことをここで固定する。
"""
from __future__ import annotations

import ctypes
import errno
import functools
import os
import struct
import sys
import types

import pytest

from common import fs_kind
from common.filelock import exclusive_lock, lock_file_for


@pytest.fixture(autouse=True)
def _clear_cache():
    fs_kind.cache_clear()
    yield
    fs_kind.cache_clear()


def _probe(monkeypatch, fs_name, drive_type=3):
    monkeypatch.setattr(fs_kind, "_win_probe", lambda _root: (fs_name, drive_type))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows FS probe")
@pytest.mark.parametrize("fs_name,expected", [
    ("NTFS", fs_kind.FS_LOCAL),
    ("ReFS", fs_kind.FS_LOCAL),
    ("exFAT", fs_kind.FS_LOCAL),
    ("FUSE-rclone", fs_kind.FS_FRAGILE),   # ← 実測: rclone マウントが返す値
    ("SomethingUnknown", fs_kind.FS_FRAGILE),
])
def test_fs_name_decides(monkeypatch, fs_name, expected):
    _probe(monkeypatch, fs_name)
    assert fs_kind.fs_kind(r"G:\data\x.json") == expected


@pytest.mark.skipif(sys.platform != "win32", reason="Windows FS probe")
def test_probe_failure_falls_back_to_fragile(monkeypatch):
    """probe できない＝判定不能。安全側（fragile）へ倒す。"""
    monkeypatch.setattr(fs_kind, "_win_probe", lambda _root: (None, None))
    assert fs_kind.fs_kind(r"Q:\whatever") == fs_kind.FS_FRAGILE


@pytest.mark.skipif(sys.platform != "win32", reason="Windows FS probe")
def test_drive_remote_is_fragile_even_when_ntfs(monkeypatch):
    """SMB 共有は FS 名 NTFS を返すので、DRIVE_REMOTE を補助判定として使う。

    ただし主判定にしてはならない: 実測で rclone マウントは DRIVE_FIXED を返し、
    ローカル NTFS と区別できない（だから FS 名が主判定）。
    """
    _probe(monkeypatch, "NTFS", drive_type=4)      # DRIVE_REMOTE
    assert fs_kind.fs_kind(r"Z:\share\x") == fs_kind.FS_FRAGILE


@pytest.mark.skipif(sys.platform != "win32", reason="Windows UNC")
def test_unc_path_is_fragile_without_probing(monkeypatch):
    monkeypatch.setattr(fs_kind, "_win_probe",
                        lambda _r: pytest.fail("UNC は probe せず fragile のはず"))
    assert fs_kind.fs_kind(r"\\server\share\x.json") == fs_kind.FS_FRAGILE


@pytest.mark.skipif(sys.platform != "win32", reason="Windows drive letters")
def test_probe_is_cached_per_volume(monkeypatch):
    calls = {"n": 0}

    def counting(_root):
        calls["n"] += 1
        return ("NTFS", 3)
    monkeypatch.setattr(fs_kind, "_win_probe", counting)
    fs_kind.fs_kind(r"C:\a\b\c")
    fs_kind.fs_kind(r"C:\d\e")
    assert calls["n"] == 1        # 同一ボリュームは 1 回だけ probe する


def test_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("MYANALYSIS_FS_OVERRIDE", f"{tmp_path}=fragile")
    assert fs_kind.fs_kind(tmp_path / "sub" / "x.json") == fs_kind.FS_FRAGILE


def test_force_fragile_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MYANALYSIS_FORCE_FRAGILE", "1")
    assert fs_kind.is_fragile(tmp_path / "x") is True


def test_describe_reports_reason(tmp_path):
    d = fs_kind.describe(tmp_path)
    assert d["kind"] in (fs_kind.FS_LOCAL, fs_kind.FS_FRAGILE)
    assert d["reason"]


# ---- ロックファイルの移設（Issue #96） -------------------------------------

def test_lock_for_fragile_path_moves_off_the_mount(monkeypatch, tmp_path):
    """同期マウント上のパスのロックはローカルへ逃がす。

    理由は 3 つ: (1) WinFsp 上で msvcrt.locking が本当に排他しているか不明、
    (2) PC 間排他は元々成立していない、(3) ロックファイル自体が同期チャーンを生む。
    """
    monkeypatch.setattr(fs_kind, "is_fragile", lambda _p: True)
    target = tmp_path / "meta.json.lock"
    mapped = lock_file_for(target)
    assert mapped != target
    assert not target.exists()          # マウント側にロックを作らない
    with exclusive_lock(target):
        pass
    assert not target.exists()
    assert mapped.exists()


def test_lock_for_local_path_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(fs_kind, "is_fragile", lambda _p: False)
    target = tmp_path / "x.lock"
    assert lock_file_for(target) == target


# ---- macOS（statfs。Issue #122）— ctypes と statvfs を差し替えて Windows でも全部の分岐を通す ----

_VFS = types.SimpleNamespace(f_frsize=4096, f_bsize=1048576, f_flag=0)
_LOCAL_FLAG = 0x1000


def _statfs_buf(fstype: bytes, flags: int, mnt: bytes, bsize: int = 4096, iosize: int = 1048576) -> bytes:
    buf = bytearray(4096)
    struct.pack_into("<Ii", buf, 0, bsize, iosize)
    struct.pack_into("<I", buf, 64, flags)
    buf[72:72 + len(fstype)] = fstype
    buf[88:88 + len(mnt)] = mnt
    return bytes(buf)


_APFS = _statfs_buf(b"apfs", _LOCAL_FLAG, b"/System/Volumes/Data")
_BAD = _statfs_buf(b"apfs", _LOCAL_FLAG, b"/", bsize=512, iosize=512)


def test_parse_statfs_cases():
    assert fs_kind._parse_statfs(_APFS, _VFS) == ("apfs", _LOCAL_FLAG, "/System/Volumes/Data")
    assert fs_kind._parse_statfs(_statfs_buf(b"macfuse", 0, b"/Volumes/r"), _VFS) == ("macfuse", 0, "/Volumes/r")
    assert fs_kind._parse_statfs(_statfs_buf(b"smbfs", 0, b"/Volumes/s"), _VFS) == ("smbfs", 0, "/Volumes/s")
    assert fs_kind._parse_statfs(_BAD, _VFS) is None
    assert fs_kind._parse_statfs(_statfs_buf(b"apfs", _LOCAL_FLAG | 0x1, b"/"), _VFS) is None
    assert fs_kind._parse_statfs(_statfs_buf(b"ap fs" + b"x" * 11, _LOCAL_FLAG, b"/"), _VFS) is None
    assert fs_kind._parse_statfs(_statfs_buf(b"ap fs", _LOCAL_FLAG, b"/"), _VFS) is None
    assert fs_kind._parse_statfs(_statfs_buf(b"apfs", _LOCAL_FLAG, b"Volumes"), _VFS) is None
    assert fs_kind._parse_statfs(_APFS[:1111], _VFS) is None


def _set_libc(monkeypatch, call):
    """_libc_statfs を差し替える（cache_clear を持たせる。fs_kind.cache_clear が呼ぶ）。"""
    monkeypatch.setattr(fs_kind, "_libc_statfs", functools.lru_cache(maxsize=1)(lambda: call))


def _fake_libc(monkeypatch, table: dict, default=None):
    """table: abspath → bytes（成功）または int（errno）。無いパスは default（None は ENOENT）。"""
    seen: list[bytes] = []

    def call(path_b, buf):
        assert isinstance(path_b, bytes)
        seen.append(path_b)
        entry = table.get(os.fsdecode(path_b), default)
        if entry is None:
            return errno.ENOENT
        if isinstance(entry, int):
            return entry
        ctypes.memmove(buf, entry, len(entry))
        return 0

    _set_libc(monkeypatch, call)
    monkeypatch.setattr(fs_kind, "_statvfs", lambda p: _VFS)
    fs_kind.cache_clear()
    return seen


# _darwin_mechanism_ok が statfs に渡すのは macOS のルートの文字列 "/" そのもの（abspath しない）。
# 偽物の表のキーも "/" にする（os.path.abspath("/") は Windows で "C:\\" になり、一致しない）。
_MECH_ROOT = "/"


def test_darwin_statfs_existing_path(monkeypatch):
    path = os.path.abspath("/Users/u/data")
    seen = _fake_libc(monkeypatch, {path: _APFS, _MECH_ROOT: _APFS})
    assert fs_kind._darwin_statfs(path) == ("apfs", _LOCAL_FLAG, "/System/Volumes/Data")
    assert seen[0] == os.fsencode(os.path.abspath(path))


def test_darwin_statfs_missing_path_climbs(monkeypatch):
    base = os.path.abspath("/Users/u")
    _fake_libc(monkeypatch, {base: _APFS, _MECH_ROOT: _APFS})
    assert fs_kind._darwin_statfs(os.path.join(base, "data", "x.json"))[0] == "apfs"


def test_darwin_statfs_failed_paths(monkeypatch):
    eio = os.path.abspath("/Volumes/gone")
    bad = os.path.abspath("/Volumes/odd")
    seen = _fake_libc(monkeypatch, {eio: errno.EIO, bad: _BAD, _MECH_ROOT: _APFS})
    assert fs_kind._darwin_mechanism_ok() is True      # 前提: 仕組みは正しい（ここが崩れると下は unavailable になる）
    assert os.fsencode(_MECH_ROOT) in seen
    fs_kind.cache_clear()
    assert fs_kind._darwin_statfs(eio) == fs_kind._STATFS_FAILED
    fs_kind.cache_clear()
    assert fs_kind._darwin_statfs(bad) == fs_kind._STATFS_FAILED


def test_darwin_statfs_unavailable(monkeypatch):
    _fake_libc(monkeypatch, {}, default=_BAD)
    assert fs_kind._darwin_statfs(os.path.abspath("/Users/u")) == fs_kind._STATFS_UNAVAILABLE
    _set_libc(monkeypatch, None)
    fs_kind.cache_clear()
    assert fs_kind._darwin_statfs(os.path.abspath("/Users/u")) == fs_kind._STATFS_UNAVAILABLE


def test_darwin_statfs_relative_path(monkeypatch):
    seen = _fake_libc(monkeypatch, {}, default=_APFS)
    assert isinstance(fs_kind._darwin_statfs("x.json"), tuple)
    assert b"" not in seen


_BASE = os.path.abspath("/Users/u")
_TARGET = os.path.join(_BASE, "data", "x.json")


@pytest.mark.parametrize("st,expected", [
    (("apfs", _LOCAL_FLAG, "/"), fs_kind.FS_LOCAL),
    (("hfs", _LOCAL_FLAG, "/"), fs_kind.FS_LOCAL),
    (("msdos", _LOCAL_FLAG, "/"), fs_kind.FS_LOCAL),
    (("exfat", _LOCAL_FLAG, "/"), fs_kind.FS_LOCAL),
    (("apfs", 0, "/"), fs_kind.FS_FRAGILE),
    (("macfuse", 0, "/"), fs_kind.FS_FRAGILE),
    (("smbfs", 0, "/"), fs_kind.FS_FRAGILE),
    (("nfs", 0, "/"), fs_kind.FS_FRAGILE),
    (("webdav", 0, "/"), fs_kind.FS_FRAGILE),
])
def test_darwin_classify(monkeypatch, st, expected):
    monkeypatch.setattr(fs_kind, "_platform", lambda: "darwin")
    monkeypatch.setattr(fs_kind, "_darwin_statfs", lambda p: st)
    fs_kind.cache_clear()
    assert fs_kind._classify(_TARGET)[0] == expected


@pytest.mark.parametrize("st,expected", [
    (fs_kind._STATFS_FAILED, (fs_kind.FS_FRAGILE, "statfs failed")),
    (fs_kind._STATFS_UNAVAILABLE, (fs_kind.FS_LOCAL, "statfs unavailable")),
])
def test_darwin_classify_statfs_problems(monkeypatch, st, expected):
    monkeypatch.setattr(fs_kind, "_platform", lambda: "darwin")
    monkeypatch.setattr(fs_kind, "_darwin_statfs", lambda p: st)
    fs_kind.cache_clear()
    assert fs_kind._classify(_TARGET) == expected


def test_darwin_override_and_force_win(monkeypatch):
    monkeypatch.setattr(fs_kind, "_platform", lambda: "darwin")
    monkeypatch.setattr(fs_kind, "_darwin_statfs", lambda p: ("macfuse", 0, "/"))
    monkeypatch.setenv("MYANALYSIS_FS_OVERRIDE", f"{_BASE}=local")
    fs_kind.cache_clear()
    assert fs_kind.fs_kind(_TARGET) == fs_kind.FS_LOCAL

    monkeypatch.delenv("MYANALYSIS_FS_OVERRIDE")
    monkeypatch.setattr(fs_kind, "_darwin_statfs", lambda p: ("apfs", _LOCAL_FLAG, "/"))
    monkeypatch.setenv("MYANALYSIS_FORCE_FRAGILE", "1")
    fs_kind.cache_clear()
    assert fs_kind.fs_kind(_TARGET) == fs_kind.FS_FRAGILE


def test_darwin_does_not_read_proc_mounts(monkeypatch):
    def _boom(path):
        raise AssertionError("_posix_fstype must not be called on darwin")

    monkeypatch.setattr(fs_kind, "_platform", lambda: "darwin")
    monkeypatch.setattr(fs_kind, "_posix_fstype", _boom)
    monkeypatch.setattr(fs_kind, "_darwin_statfs", lambda p: ("apfs", _LOCAL_FLAG, "/"))
    fs_kind.cache_clear()
    assert fs_kind.fs_kind(_TARGET) == fs_kind.FS_LOCAL


def test_darwin_describe(monkeypatch):
    monkeypatch.setattr(fs_kind, "_platform", lambda: "darwin")
    monkeypatch.setattr(fs_kind, "_darwin_statfs", lambda p: ("macfuse", 0, "/"))
    fs_kind.cache_clear()
    info = fs_kind.describe(_TARGET)
    assert info["fstypename"] == "macfuse"
    assert info["mnt_local"] is False
    monkeypatch.setattr(fs_kind, "_darwin_statfs", lambda p: fs_kind._STATFS_FAILED)
    fs_kind.cache_clear()
    info = fs_kind.describe(_TARGET)
    assert info["fstypename"] is None
    assert info["mnt_local"] is None
