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

## 判定方法（macOS）

macOS には `/proc/mounts` が無いので、`statfs(2)` の `f_fstypename` と `f_flags` の `MNT_LOCAL` で判定する
（`_darwin_statfs` / `_parse_statfs`。ctypes に触るのは `_libc_statfs` だけ）。apfs・hfs・msdos・exfat かつ
MNT_LOCAL だけ local、ほか（macFUSE・FUSE-T・rclone の nfsmount・smbfs・nfs・afpfs・webdav・未知の名前）は fragile。
File Provider（Google ドライブ・Dropbox・OneDrive・iCloud）の `~/Library/CloudStorage` は APFS のデータボリューム上なので local。

構造体のレイアウトの誤りはパスに依らず全部のパスで起きるので、仕組みが使えるかどうかは必ずローカルのディスクにある
`/` で確かめる（`_darwin_mechanism_ok`。statvfs の値と突き合わせる自己検査を含む）。

- `/` でも通らない（libc・シンボルが読めない、自己検査に外れる）→ 仕組みが使えないとして **local**
  （今までの macOS と同じ。fragile に倒すと全部の Mac で guard-write が全部の Write/Edit を拒否する）。
- `/` は通るのに、そのパスだけ statfs・statvfs が失敗する（ENOENT・ENOTDIR 以外。切れたネットワーク・FUSE のマウント）
  か自己検査に外れる → **fragile**（判定不能は fragile。原則どおり）。

firmlink のため、`/Users/...` の statfs は `f_mntonname` に `/System/Volumes/Data` を返す。`f_mntonname` をパスの
接頭辞と照合してはならない。st_dev で照合してもいけない（firmlink では当てにならない。JDK-8230085）。
"""
import ctypes
import errno
import functools
import os
import re
import struct
import sys
from collections.abc import Callable
from typing import Any

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

# macOS（statfs）の判定。
_MNT_RDONLY = 0x1          # sys/mount.h
_MNT_LOCAL = 0x1000        # sys/mount.h
_ST_RDONLY = 0x1           # sys/statvfs.h。os モジュールの ST_RDONLY は Windows に無いので使わない
_DARWIN_LOCAL_FS = frozenset({"apfs", "hfs", "msdos", "exfat"})
_STATFS_BUF = 4096
_FSTYPENAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,15}")   # 大文字の例: 旧 Google Drive の dfsfuse_DFS
_STATFS_UNAVAILABLE = "unavailable"   # 仕組みが使えない（/ でも通らない）→ local
_STATFS_FAILED = "failed"             # このパスだけ答えない → fragile


def _platform() -> str:
    return sys.platform


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
    if _platform() == "win32":
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


def _parse_statfs(buf: bytes, vfs: Any) -> tuple[str, int, str] | None:
    """``struct statfs``（sys/mount.h の __DARWIN_STRUCT_STATFS64）を読む。自己検査に外れたら None。

    ``vfs`` は同じパスの statvfs の結果（f_frsize・f_bsize・f_flag）。返すのは (fstypename, f_flags, mntonname)。
    mntonname はパスの接頭辞と照合しない（firmlink。モジュールの docstring）。
    """
    if len(buf) < 1112:
        return None
    f_bsize, f_iosize = struct.unpack_from("<Ii", buf, 0)
    (f_flags,) = struct.unpack_from("<I", buf, 64)
    raw_type = buf[72:88]
    raw_mnt = buf[88:1112]
    if b"\0" not in raw_type or b"\0" not in raw_mnt:
        return None
    try:
        fstype = raw_type.split(b"\0", 1)[0].decode("ascii")
    except UnicodeDecodeError:
        return None
    if not _FSTYPENAME_RE.fullmatch(fstype):
        return None
    mnt = os.fsdecode(raw_mnt.split(b"\0", 1)[0])
    if not mnt.startswith("/"):
        return None
    if {f_bsize, f_iosize} != {vfs.f_frsize, vfs.f_bsize}:
        return None
    if bool(f_flags & _MNT_RDONLY) != bool(vfs.f_flag & _ST_RDONLY):
        return None
    return fstype, f_flags, mnt


@functools.lru_cache(maxsize=1)
def _libc_statfs() -> Callable[[bytes, Any], int] | None:
    """libc の statfs を呼ぶ関数（0 か errno を返す）。読めなければ None。投げない。

    ``c_char_p`` には bytes しか渡せない（str は ctypes.ArgumentError）。呼び出し側が os.fsencode する。
    """
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        try:
            fn = getattr(libc, "statfs$INODE64")   # x86_64 の 64 ビット inode 版
        except AttributeError:
            fn = libc.statfs                       # arm64 はこれが 64 ビット版
        fn.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
        fn.restype = ctypes.c_int
    except Exception:  # noqa: BLE001
        return None

    def call(path_b: bytes, buf) -> int:
        rc = fn(path_b, buf)
        if rc == 0:
            return 0
        return ctypes.get_errno() or errno.EIO

    return call


def _statvfs(path: str) -> Any:
    return os.statvfs(path)


def _statfs_once(call: Callable[[bytes, Any], int], p: str) -> tuple[int, Any]:
    """1 つのパスの statfs。(errno, buf)。"""
    buf = ctypes.create_string_buffer(_STATFS_BUF)
    return call(os.fsencode(p), buf), buf


def _darwin_statfs(path: str) -> tuple[str, int, str] | str:
    """macOS の statfs の判定材料。タプル・_STATFS_UNAVAILABLE・_STATFS_FAILED のどれか。投げない。"""
    try:
        p = os.path.abspath(str(path))
        call = _libc_statfs()
        if call is None:
            return _STATFS_UNAVAILABLE
        while True:
            err, buf = _statfs_once(call, p)
            if err in (errno.ENOENT, errno.ENOTDIR):
                parent = os.path.dirname(p)
                if parent == p:
                    return _STATFS_FAILED
                p = parent
                continue
            break
        if err != 0:
            return _STATFS_FAILED if _darwin_mechanism_ok() else _STATFS_UNAVAILABLE
        try:
            vfs = _statvfs(p)
        except OSError:
            return _STATFS_FAILED if _darwin_mechanism_ok() else _STATFS_UNAVAILABLE
        st = _parse_statfs(buf.raw, vfs)
        if st is None:
            return _STATFS_FAILED if _darwin_mechanism_ok() else _STATFS_UNAVAILABLE
        return st
    except Exception:  # noqa: BLE001
        return _STATFS_UNAVAILABLE


@functools.lru_cache(maxsize=1)
def _darwin_mechanism_ok() -> bool:
    """statfs の仕組み（libc・シンボル・構造体のレイアウト）が使えるかを、ローカルのディスクの / で確かめる。"""
    try:
        call = _libc_statfs()
        if call is None:
            return False
        err, buf = _statfs_once(call, "/")
        if err != 0:
            return False
        return _parse_statfs(buf.raw, _statvfs("/")) is not None
    except Exception:  # noqa: BLE001
        return False


@functools.lru_cache(maxsize=64)
def _probe_root(root: str) -> tuple[str, str]:
    """ボリュームルート単位の判定（キャッシュ対象）。→ (kind, reason)。"""
    if _platform() == "win32":
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

    if _platform() == "darwin":
        st = _darwin_statfs(root)
        if st == _STATFS_UNAVAILABLE:
            return (FS_LOCAL, "statfs unavailable")
        if st == _STATFS_FAILED:
            return (FS_FRAGILE, "statfs failed")
        name, flags, _mnt = st
        local = bool(flags & _MNT_LOCAL)
        reason = f"fstype={name} local={local}"
        if name in _DARWIN_LOCAL_FS and local:
            return (FS_LOCAL, reason)
        return (FS_FRAGILE, reason)

    fstype = _posix_fstype(root)
    if fstype is None:
        # /proc/mounts が無い（macOS 以外の POSIX）。観測された障害は WinFsp 固有で、POSIX の FUSE は
        # rename を正しく実装するため local 扱いにする。macOS は上の statfs の分岐で判定する。
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
    if _platform() == "win32":
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
    if _platform() == "win32":
        fs_name, drive_type = _win_probe(volume_root(path))
        info["fs_name"] = fs_name
        info["drive_type"] = drive_type
    elif _platform() == "darwin":
        st = _darwin_statfs(str(path))
        if isinstance(st, tuple):
            info["fstypename"] = st[0]
            info["mnt_local"] = bool(st[1] & _MNT_LOCAL)
        else:
            info["fstypename"] = None
            info["mnt_local"] = None
    return info


def cache_clear() -> None:
    """ボリューム判定キャッシュを捨てる（テスト・ホットリロード用）。"""
    _probe_root.cache_clear()
    _libc_statfs.cache_clear()
    _darwin_mechanism_ok.cache_clear()
