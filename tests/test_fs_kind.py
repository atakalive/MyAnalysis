"""common/fs_kind.py — rename-into-place が信頼できる FS かの判定（Issue #96）。

判定を誤ると (a) fragile を local と誤判定 → サイレントなデータ損失、
(b) local を fragile と誤判定 → in-place になるだけで正常動作、と非対称なので、
**判定不能は必ず fragile 側へ倒す**ことをここで固定する。
"""
from __future__ import annotations

import sys

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
