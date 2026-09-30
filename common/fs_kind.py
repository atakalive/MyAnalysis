"""ファイルシステムの「rename-into-place が信頼できるか」判定（Issue #96）。

## なぜ必要か

rclone/WinFsp マウント上では `os.replace(tmp, target)` が **マウント層では成功を返しながら
キャッシュ層で失敗する**。実測（rclone のログ）:

    アプリ: os.replace(tmp, target)  →  WinFsp: 成功を返す
      → rclone: File.Rename failed in Cache: ... Access is denied.
      → rclone: removed cache file as stale (remote deleted)          (cache item を破棄)
      → 以後の open: cannot find the file specified → Couldn't read size of file
      → 読み手には 0 バイト。その空が remote へ upload される。

原因は Windows の `MoveFileEx(REPLACE_EXISTING)` が「宛先が FILE_SHARE_DELETE 無しで
開かれていると ERROR_ACCESS_DENIED」であること。Go の `os.OpenFile` は FILE_SHARE_DELETE を
付けないので、**直前に読んだファイルは rclone が cache item の fd を保持していて rename できない**。
実測がこれを裏づける: `figures/*.png`（読まずに書く）は rename が失敗しない、
`meta.json`（読んでから書く）は rename が多数失敗。`durable_write_json` が primary→`.bak` の順に書くと
primary（直前に読んだ）だけが失敗し `.bak` が成功するため、「`.bak` は primary より新しくならない」
という不変条件が恒常的に破れる。

したがって **fragile FS では rename を一切使わず in-place write にする**のが唯一の根治。
その切替判定を行うのがこのモジュール。ポリシー（実際の書き方）は common.paths が持つ。

## 判定方法（Windows）

`GetVolumeInformationW` の **ファイルシステム名**だけで判定する。実測値（ドライブ文字は一例）:

    'C:\\'  fs='NTFS'         drivetype=3 (DRIVE_FIXED)
    'M:\\'  fs='FUSE-rclone'  drivetype=3 (DRIVE_FIXED)   ← rclone マウント

**`GetDriveTypeW` は使ってはならない**: WinFsp のディスクは `DRIVE_FIXED` を返し、ローカル
NTFS と区別できない（上の実測どおり M: と C: が同じ値）。DRIVE_REMOTE 判定は空振りする。
補助として残してあるのは SMB 等の素直なネットワークドライブを拾うためだけで、
**主判定に昇格させないこと**。

判定不能（probe 失敗・未知の FS 名）は **fragile 側**に倒す。誤って fragile と判定しても
in-place write になるだけで正常 FS でも正しく動くが、逆（fragile を local と誤判定）は
サイレントなデータ損失に直結するため、非対称に扱う。
"""
import functools
import os
import sys
from pathlib import Path

FS_LOCAL = "local"      # rename-into-place が信頼できる
FS_FRAGILE = "fragile"  # in-place write を使う（rename 禁止）

# rename-into-place が信頼できると確認済みのファイルシステム名（大文字）。
# ここに無い名前はすべて fragile 扱い（未知＝安全側）。
_RENAME_SAFE_FS = frozenset({
    "NTFS", "REFS", "EXFAT", "FAT", "FAT16", "FAT32", "CDFS", "UDF",
})

# POSIX で fragile と扱う fstype。FUSE 経由のクラウドマウントとネットワーク FS。
_FRAGILE_FSTYPE_PREFIXES = ("fuse", "cifs", "smb", "nfs", "9p", "davfs", "ceph", "glusterfs")

_ENV_OVERRIDE = "MYANALYSIS_FS_OVERRIDE"     # "M:=fragile,D:=local"
_ENV_FORCE_FRAGILE = "MYANALYSIS_FORCE_FRAGILE"


def _norm(p: str) -> str:
    """比較用の正規化（Windows は大文字小文字とセパレータを吸収）。"""
    p = str(p).replace("/", os.sep)
    return os.path.normcase(p)


def volume_root(path) -> str:
    r"""`path` が属するボリュームのルート文字列（キャッシュキー）。

    Windows: 'G:\\' / UNC は '\\\\server\\share'。POSIX: '/'。
    `\\?\X:\...` の拡張プレフィックスは剥がしてからドライブを取る。
    """
    p = os.path.abspath(str(path))
    if sys.platform == "win32":
        for pref in ("\\\\?\\UNC\\", "\\\\?\\"):
            if p.startswith(pref):
                p = ("\\\\" + p[len(pref):]) if pref.endswith("UNC\\") else p[len(pref):]
                break
        drive, _rest = os.path.splitdrive(p)
        if not drive:
            return os.sep
        # 'G:' → 'G:\'（GetVolumeInformationW は末尾セパレータ必須）。UNC は splitdrive が
        # '\\server\share' を返すので、そのまま末尾セパレータを付ける。
        return drive + os.sep if not drive.endswith(os.sep) else drive
    return "/"


def _overrides() -> list[tuple[str, str]]:
    """env の明示上書きを (正規化プレフィックス, kind) の長い順リストで返す。"""
    raw = os.environ.get(_ENV_OVERRIDE, "").strip()
    out: list[tuple[str, str]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        prefix, _, kind = item.rpartition("=")
        kind = kind.strip().lower()
        if kind in (FS_LOCAL, FS_FRAGILE) and prefix.strip():
            out.append((_norm(prefix.strip()), kind))
    out.sort(key=lambda t: -len(t[0]))   # 最長プレフィックス一致
    return out


def _win_probe(root: str) -> tuple[str | None, int | None]:
    """(ファイルシステム名, GetDriveTypeW) を返す。probe 失敗時は (None, ...)。"""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
    ]
    k32.GetVolumeInformationW.restype = wintypes.BOOL
    k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    k32.GetDriveTypeW.restype = wintypes.UINT

    fs_buf = ctypes.create_unicode_buffer(261)
    vol_buf = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()
    maxlen = wintypes.DWORD()
    flags = wintypes.DWORD()
    try:
        ok = k32.GetVolumeInformationW(
            root, vol_buf, len(vol_buf), ctypes.byref(serial),
            ctypes.byref(maxlen), ctypes.byref(flags), fs_buf, len(fs_buf),
        )
        drive_type = int(k32.GetDriveTypeW(root))
    except OSError:
        return (None, None)
    return (fs_buf.value if ok else None, drive_type)


def _posix_fstype(path: str) -> str | None:
    """/proc/mounts の最長プレフィックス一致で fstype を返す（無ければ None）。"""
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            entries = [ln.split() for ln in f]
    except OSError:
        return None
    best, best_type = "", None
    target = os.path.abspath(path)
    for parts in entries:
        if len(parts) < 3:
            continue
        mnt, fstype = parts[1].replace("\\040", " "), parts[2]
        if (target == mnt or target.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
            best, best_type = mnt, fstype
    return best_type


@functools.lru_cache(maxsize=64)
def _probe_root(root: str) -> tuple[str, str]:
    """ボリュームルート単位の判定（キャッシュ対象）。→ (kind, reason)。"""
    if sys.platform == "win32":
        if root.startswith("\\\\"):
            return (FS_FRAGILE, "UNC path")
        fs_name, drive_type = _win_probe(root)
        if fs_name is None:
            return (FS_FRAGILE, "GetVolumeInformationW failed")
        if fs_name.upper() not in _RENAME_SAFE_FS:
            return (FS_FRAGILE, f"fs={fs_name}")
        if drive_type == 4:  # DRIVE_REMOTE。補助のみ — 主判定に昇格させないこと（docstring 参照）
            return (FS_FRAGILE, f"fs={fs_name} but DRIVE_REMOTE")
        return (FS_LOCAL, f"fs={fs_name}")

    fstype = _posix_fstype(root)
    if fstype is None:
        # /proc/mounts が無い（macOS 等）。観測された障害は WinFsp 固有で、POSIX の FUSE は
        # rename を正しく実装するため local 扱いにする。
        return (FS_LOCAL, "no /proc/mounts")
    low = fstype.lower()
    if any(low.startswith(p) for p in _FRAGILE_FSTYPE_PREFIXES):
        return (FS_FRAGILE, f"fstype={fstype}")
    return (FS_LOCAL, f"fstype={fstype}")


def _classify(path) -> tuple[str, str]:
    if os.environ.get(_ENV_FORCE_FRAGILE, "").strip().lower() in ("1", "true", "yes"):
        return (FS_FRAGILE, f"{_ENV_FORCE_FRAGILE} set")
    target = _norm(os.path.abspath(str(path)))
    for prefix, kind in _overrides():
        if target == prefix or target.startswith(prefix.rstrip(os.sep) + os.sep):
            return (kind, f"{_ENV_OVERRIDE} {prefix}={kind}")
    root = volume_root(path)
    if sys.platform == "win32":
        return _probe_root(root)
    # POSIX はマウントポイントがボリュームルートと一致しないので実パスで引く
    return _probe_root(os.path.abspath(str(path)))


def fs_kind(path) -> str:
    """`path` の FS 種別を返す: FS_LOCAL | FS_FRAGILE。never raise。"""
    try:
        return _classify(path)[0]
    except Exception:      # noqa: BLE001 — 判定不能は安全側（fragile）へ倒す
        return FS_FRAGILE


def is_fragile(path) -> bool:
    """rename-into-place を避けるべき FS なら True。"""
    return fs_kind(path) == FS_FRAGILE


def describe(path) -> dict:
    """診断用（mount_probe / doctor）: 判定と根拠を返す。"""
    try:
        kind, reason = _classify(path)
    except Exception as e:  # noqa: BLE001
        kind, reason = FS_FRAGILE, f"probe raised {e!r}"
    info = {"path": str(path), "volume_root": volume_root(path),
            "kind": kind, "reason": reason}
    if sys.platform == "win32":
        fs_name, drive_type = _win_probe(volume_root(path))
        info["fs_name"] = fs_name
        info["drive_type"] = drive_type
    return info


def cache_clear() -> None:
    """ボリューム判定キャッシュを捨てる（テスト・ホットリロード用）。"""
    _probe_root.cache_clear()
