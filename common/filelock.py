"""Cross-platform advisory exclusive file lock.

POSIX uses fcntl.flock (blocks indefinitely); Windows uses msvcrt.locking,
which times out after ~10s per attempt, so we retry up to 3 times (~30s total).

Asymmetry note: POSIX blocks forever, Windows caps at ~30s. Fine for this
project's short GUI+CLI mutual exclusion; revisit if long holds are ever needed.

## ロックファイルはマウント外に置く（Issue #96）

`exclusive_lock` に同期マウント上のパスを渡しても、ロック実体はローカルディスクへ
マッピングされる。理由:

1. **マウント上では排他が効いているか怪しい。** WinFsp/rclone がバイト範囲ロックを実装して
   いなければ `msvcrt.locking` は失敗するか、排他せずに成功する。後者だと
   `meta.json` / `annotations.json` の read-modify-write が無防備なまま「守られている」と
   誤認することになる。ローカル NTFS なら確実に効く。
2. **PC 間の排他はもともと成立していない**（ロックはローカルな機構で、同じ remote を
   2 台がマウントしていれば無関係）。したがって移設で失う機能は無い。
3. ロックファイル自体が同期対象になり、取得のたびに upload チャーンを生んでいた
   （実測で `meta.json.lock` が クラウドストレージ上まで同期されていた）。

マッピング先は `<repo>/data/locks/<sha1(絶対パス)>.lock`。`data/` は gitignored の
PC ローカル状態で、`data/llm_state/` と同じ位置づけ。
"""

import contextlib
import hashlib
import os
import sys
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


def lock_file_for(path: Path | str) -> Path:
    """`path` を守るためのロックファイルの実体パス。

    fragile FS（同期マウント/ネットワーク）上のパスはローカルの `data/locks/` へ
    マッピングする。ローカル FS 上のパスはそのまま（既存の挙動・既存テスト不変）。
    """
    from common import fs_kind
    from common.paths import repo_root

    p = Path(path)
    if not fs_kind.is_fragile(p):
        return p
    key = hashlib.sha1(os.path.normcase(str(p.absolute())).encode("utf-8")).hexdigest()
    d = repo_root() / "data" / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.lock"


@contextlib.contextmanager
def exclusive_lock(path: Path | str):
    """Acquire an exclusive advisory lock on `path` for the with-block.

    同期マウント上のパスは `lock_file_for` でローカルへマッピングされる（docstring 参照）。
    """
    path = lock_file_for(path)
    if sys.platform == "win32":
        # "a" で開く（"w" ではない）: ロックに truncate は不要で、書込を伴わない方が
        # マウント/AV との相互作用も少ない。
        lf = open(path, "a")
        try:
            last_err: OSError | None = None
            for _ in range(3):
                try:
                    msvcrt.locking(lf.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError as e:
                    last_err = e
            else:
                raise last_err
            try:
                yield
            finally:
                msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            lf.close()
    else:
        lf = open(path, "a")
        try:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
        finally:
            lf.close()
