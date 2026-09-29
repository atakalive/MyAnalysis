"""マウント上の書込戦略を実測で比較する制御実験（Issue #96）。

過去 5 回の 0 バイト化修正（#68/#69/#73/#75/#89/#92）は、いずれも
**実マウント上で仮説を検証しないまま**出荷されて失敗した。このモジュールはその欠落を埋める。

4 条件を同数ずつ実行し、rclone のログ（唯一信頼できる oracle。マウント越しの read-back は
VFS キャッシュから返るので「upload できたか」を語れない）から失敗イベントを数える:

    a_replace         mkstemp + os.replace                （現行 atomic_write_text）
    b_inplace         open(target,'wb') で上書き           （提案する新戦略）
    c_read_replace    読んでから mkstemp + os.replace      （meta.json / session.json の実パターン）
    d_read_inplace    読んでから in-place 上書き

仮説（実ログの分析から）: 失敗は「宛先 cache file が開かれている状態への rename」で起きるので
    c_read_replace ≫ a_replace ≫ 0 == b_inplace == d_read_inplace

**b/d がゼロでなければ in-place 戦略の前提が崩れる。その場合は設計をやり直すこと。**

使い方:
    python -m devtools.mount_probe --dir <マウント上の既存のディレクトリ> --log <rclone のログ> [--cache <rclone の VFS キャッシュ>] --n 50
    python -m devtools.mount_probe --describe-only        # FS 判定だけ見る
"""
import argparse
import os
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import fs_kind  # noqa: E402
from common import rclone_paths  # noqa: E402

# ログ中の失敗イベント（rclone v1.74 の文言）
EVENTS = {
    "rename_failed": "File.Rename failed in Cache",
    "stale_removed": "removed cache file as stale",
    "cant_read_size": "Couldn't read size of file",
    "open_failed": "vfs cache: failed to open item",
    "empty_upload": "d41d8cd98f00b204e9800998ecf8427e",   # 空文字列の MD5 = 空を転送しかけた
}


def _payload(i: int, size: int) -> bytes:
    """毎回内容が変わる（＝内容同一短絡に当たらない）ペイロード。"""
    head = f"# mount_probe iteration {i:05d} at {time.time():.6f}\n".encode()
    return head + (b"x" * max(0, size - len(head) - 1)) + b"\n"


def w_replace(path: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def w_inplace(path: Path, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass


CONDITIONS = {
    "a_replace":      (False, w_replace),
    "b_inplace":      (False, w_inplace),
    "c_read_replace": (True,  w_replace),
    "d_read_inplace": (True,  w_inplace),
}


def count_orphan_tmps(cache: Path) -> int:
    if not cache.is_dir():
        return -1
    n = 0
    for _root, _dirs, files in os.walk(cache):
        n += sum(1 for fn in files if ".tmp" in fn)
    return n


def read_log_tail(log: Path, offset: int) -> tuple[list[str], int]:
    """log の offset 以降を読み、(行, 新offset) を返す。"""
    if not log.is_file():
        return ([], offset)
    size = log.stat().st_size
    if size < offset:      # ローテートされた
        offset = 0
    with open(log, "rb") as f:
        f.seek(offset)
        raw = f.read()
    return (raw.decode("utf-8", errors="replace").splitlines(), offset + len(raw))


def tally(lines: list[str], needle: str) -> dict[str, int]:
    out = dict.fromkeys(EVENTS, 0)
    for ln in lines:
        if needle and needle not in ln:
            continue
        for key, pat in EVENTS.items():
            if pat in ln:
                out[key] += 1
    return out


def _probe_targets(dir_arg: str | None, *, platform: str = sys.platform,
                   listdrives: Callable[[], list[str]] | None = getattr(os, "listdrives", None),
                   ) -> tuple[list[str], str | None]:
    """FS 判定を表示する対象と、省略したときの注記。Windows では実在するドライブ
    （os.listdrives。3.12 以降）と --dir。"""
    note = None
    drives: list[str] = []
    if platform == "win32":
        if listdrives is None:
            note = "Python 3.12 未満のためドライブ一覧は省略"
        else:
            drives = list(listdrives())
    return drives + ([dir_arg] if dir_arg else []), note


def _cleanup(run_dir: Path, written: set[str]) -> None:
    """この実行が書込を試みた {cond}.bin だけを消し、この実行が作った run_dir を rmdir する。
    ディレクトリごとの再帰削除はしない（他のファイルがあれば残す）。"""
    for cond in sorted(written):
        p = run_dir / f"{cond}.bin"
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            print(f"  消せなかった: {p} ({e})")
    try:
        run_dir.rmdir()
    except OSError as e:
        print(f"  {run_dir} を消せなかったので残した（{e}）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=None,
                    help="マウント上の既存のディレクトリ（--describe-only 以外で必須）。この下に"
                         "実行ごとの mount_probe-* を作って書き、終了時にそれだけを消す")
    ap.add_argument("--n", type=int, default=50, help="1 条件あたりの書込回数")
    ap.add_argument("--size", type=int, default=4096, help="1 ファイルのバイト数")
    ap.add_argument("--log", default=None,
                    help="rclone のログファイル（必須。省略時は環境変数 MYANALYSIS_RCLONE_LOG）")
    ap.add_argument("--cache", default=None,
                    help="rclone の VFS キャッシュ（任意。省略時は環境変数 MYANALYSIS_RCLONE_CACHE。"
                         "無ければ孤児 tmp を数えない）")
    ap.add_argument("--settle", type=float, default=12.0,
                    help="計測後に待つ秒数（write-back 5s + upload を跨ぐ）")
    ap.add_argument("--pace", type=float, default=12.0,
                    help="1 ラウンドごとに待つ秒数。**再現の要**: rclone の write-back(5s)+upload が"
                         " 完了して cache item が clean/remote-backed になってから次の read+write を"
                         " 行わせる。0 にすると全書込が dirty item のまま進み、実運用の失敗が再現しない"
                         "（n=50 の連続書込では 4 条件とも失敗 0 件だった）。")
    ap.add_argument("--keep", action="store_true",
                    help="この実行の mount_probe-* ディレクトリと .bin を消さない")
    ap.add_argument("--describe-only", action="store_true", help="FS 判定だけ表示して終了")
    args = ap.parse_args(argv)

    if not args.describe_only:
        if args.dir is None:
            ap.error("--dir は必須（--describe-only のときを除く）")
        work = Path(args.dir)
        if not work.is_dir():
            ap.error(f"--dir が既存のディレクトリでない: {work}")
        log = rclone_paths.resolve(args.log, rclone_paths.ENV_LOG)
        if log is None:
            ap.error("--log（または環境変数 MYANALYSIS_RCLONE_LOG）は必須")
        if not log.is_file():
            ap.error(f"rclone のログが見つからない: {log}")
        cache = rclone_paths.resolve(args.cache, rclone_paths.ENV_CACHE)
        if cache is not None and not cache.is_dir():
            ap.error(f"rclone のキャッシュが見つからない: {cache}")

    print("=" * 78)
    targets, note = _probe_targets(args.dir)
    if note:
        print(f"  ({note})")
    for p in targets:
        d = fs_kind.describe(p)
        print(f"  {d['kind']:8s} {d['volume_root']:14s} fs={str(d.get('fs_name')):14s} "
              f"drive_type={d.get('drive_type')}  {d['reason']}")
    if args.describe_only:
        return 0

    run_dir = Path(tempfile.mkdtemp(prefix="mount_probe-", dir=work))   # この実行専用（排他的に作成）
    needle = run_dir.name          # ログ行のパスに現れる作業ディレクトリ名で自分の書込を特定
    written: set[str] = set()      # 書込を試みた条件。後片付けで消すのはこれだけ
    try:
        _lines, offset = read_log_tail(log, 0)      # 現在の末尾までスキップ
        orphans_before = None if cache is None else count_orphan_tmps(cache)
        print("=" * 78)
        print(f"  work={run_dir}   n={args.n} x {args.size}B   log={log.name}")
        if cache is None:
            print("  orphan tmps: キャッシュ未指定のため数えない")
        else:
            print(f"  orphan tmps in cache before: {orphans_before}")

        # 4 条件を 1 ラウンド内でインターリーブし、ラウンド末に pace 秒待つ。こうすると
        # (a) 全条件が同じ時間帯・同じ負荷条件を共有する（公平な比較）
        # (b) 各ターゲットが「write → upload 完了 → read → write」という実運用の並びを踏む
        results: dict[str, dict] = {c: {"write_errors": 0, "readback_mismatch": 0}
                                    for c in CONDITIONS}
        t0 = time.time()
        for i in range(args.n):
            for cond, (do_read, writer) in CONDITIONS.items():
                target = run_dir / f"{cond}.bin"
                data = _payload(i, args.size)
                if do_read and target.exists():
                    try:
                        target.read_bytes()          # ← 宛先 cache item を開かせる
                    except OSError:
                        pass
                written.add(cond)
                try:
                    writer(target, data)
                except OSError:
                    results[cond]["write_errors"] += 1
                    continue
                try:
                    if target.read_bytes() != data:
                        results[cond]["readback_mismatch"] += 1
                except OSError:
                    results[cond]["readback_mismatch"] += 1
            if i % 5 == 0 or i == args.n - 1:
                print(f"  round {i + 1:3d}/{args.n}  ({time.time() - t0:5.0f}s elapsed)", flush=True)
            if args.pace:
                time.sleep(args.pace)

        time.sleep(args.settle)
        lines, offset = read_log_tail(log, offset)
        # ログ行のパスからどの条件のファイルかを特定して集計する
        for cond in CONDITIONS:
            results[cond].update(tally(lines, f"{needle}/{cond}.bin"))
        orphans_after = None if cache is None else count_orphan_tmps(cache)

        print("=" * 78)
        cols = ["rename_failed", "stale_removed", "cant_read_size", "open_failed",
                "empty_upload", "write_errors", "readback_mismatch"]
        print(f"  {'condition':16s} " + " ".join(f"{c[:13]:>13s}" for c in cols))
        for cond in CONDITIONS:
            print(f"  {cond:16s} " + " ".join(f"{results[cond][c]:>13d}" for c in cols))
        if orphans_before is not None and orphans_after is not None:
            print(f"\n  orphan tmps in cache: {orphans_before} -> {orphans_after} "
                  f"(delta {orphans_after - orphans_before:+d})")

        rename_based = results["a_replace"]["rename_failed"] + results["c_read_replace"]["rename_failed"]
        inplace_based = results["b_inplace"]["rename_failed"] + results["d_read_inplace"]["rename_failed"]
        inplace_damage = sum(results[c][k] for c in ("b_inplace", "d_read_inplace")
                             for k in ("stale_removed", "cant_read_size", "readback_mismatch"))
        print("=" * 78)
        print(f"  rename 系 (a+c) の rename 失敗 : {rename_based}")
        print(f"  in-place 系 (b+d) の rename 失敗: {inplace_based}")
        print(f"  in-place 系の破損イベント合計   : {inplace_damage}")
        if inplace_damage == 0 and rename_based > 0:
            print("  => 仮説を支持: in-place は失敗せず、rename は失敗する。")
        elif rename_based == 0:
            print("  => 判定不能: この試行では rename 失敗が再現しなかった（n を増やすか負荷を上げる）。")
        else:
            print("  => 仮説に反する。in-place でも破損している。設計をやり直すこと。")
    finally:
        if args.keep:
            print(f"  --keep: {run_dir} を残した")
        else:
            _cleanup(run_dir, written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
