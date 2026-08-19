"""同期マウント上のデータ健全性チェック（Issue #96）。

**検出と報告だけを行い、自動修復はしない。** 理由: 修復は「どちらが正しいか」の判断を
含み、`--dir-cache-time <長時間>` の下では他 PC の新しい内容を古い内容で上書きし返す事故に
なりうる。復旧は `--repair` を明示的に指定したときだけ、newest-wins で収束させる。

見るもの:
  1. データセット配下の 0 バイトファイル（= 書込失敗の痕跡）
  2. meta.json / session.json / annotations.json の primary と .bak の乖離
  3. myanalysis.toml の空/破損（work_dir が黙って `_work` に戻る穴）
  4. rclone のキャッシュに取り残された孤児 tmp（= 復旧候補かつ rename 失敗の証拠）
  5. rclone のログに出た直近の失敗イベント（--log-file が唯一信頼できる oracle。
     マウント越しの read-back は VFS キャッシュから返るので upload の成否を語れない）
"""
from __future__ import annotations

import os
from pathlib import Path

import config
import dataset_config
from common import fs_kind
from common.paths import (bak_path, durable_read_json, durable_write_json,
                          read_json_classified, strip_seq)

# rclone のログに出る失敗イベント（v1.74 の文言）
_LOG_EVENTS = (
    "File.Rename failed in Cache",
    "removed cache file as stale",
    "Couldn't read size of file",
    "vfs cache: failed to open item",
    "d41d8cd98f00b204e9800998ecf8427e",     # 空文字列の MD5 = 空を転送しかけた
)

_DEFAULT_LOG = Path(os.environ.get("LOCALAPPDATA", "")) / "rclone" / "mount.log"
_DEFAULT_CACHE = Path(r"X:\remote\vfs\remote")

# durable_write_json で 2 コピー管理しているファイル（乖離チェックの対象）
_DURABLE_NAMES = ("meta.json", "session.json", "annotations.json")

# data/llm_state 直下で検査する PC ローカル状態。**明示列挙する** — os.walk にすると
# commands/ のキュー（正常に空のことがある）や mkstemp の残骸まで拾って誤検出になる。
_LOCAL_STATE_NAMES = (
    "active.json", "backend_sessions.json", "last_window.json",
    "recent_datasets.json", "ui_prefs.json",
)
_LOCAL_DURABLE_NAMES = ("personas.json",)    # primary + .bak の 2 コピー


def _iter_datasets(only: str | None):
    names = [only] if only else sorted(config.DATASETS)
    for name in names:
        try:
            yield name, config.get_dataset_dir(name)
        except (KeyError, RuntimeError) as e:
            yield name, e


def find_zero_byte_files(root: Path) -> list[Path]:
    """0 バイトのファイル（.lock は空が正常なので除く）。"""
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn.endswith(".lock"):
                continue
            p = Path(dirpath) / fn
            try:
                if p.stat().st_size == 0:
                    out.append(p)
            except OSError:
                pass
    return out


def check_local_state(*, repair: bool = False) -> tuple[list[str], list[str]]:
    """``data/llm_state`` の PC ローカル JSON を検査する。``(issues, notes)`` を返す。

    ここは同期マウントではなくローカル FS（実測で ``fs_kind.is_fragile`` は False）だが、
    0 バイト化は現に起きた: ``backend_sessions.json`` が 0 バイトのまま固着し、
    ``read_json_classified`` の 'unreadable' 判定と噛み合って書込が恒久 skip され、
    resume token が全エンジンで一度も保存されていなかった。writer 側は
    ``llm_bridge.paths._preserve_unreadable`` で自己修復するようになったが、
    「そもそも 0 バイトが在る」ことを見せる経路は要る。

    中身は再計算可能な PC ローカル状態なので ``--repair`` は削除でよい（次回起動で
    作り直される）。同期マウントのキャッシュ復旧用の ``--rescue`` は対象外。
    """
    from common.paths import bak_path, durable_read_json, read_json_classified
    from llm_bridge.paths import global_state_dir

    issues: list[str] = []
    notes: list[str] = []
    root = global_state_dir()
    plain = [root / n for n in _LOCAL_STATE_NAMES]
    durable_primaries = [root / n for n in _LOCAL_DURABLE_NAMES]
    targets = plain + durable_primaries + [bak_path(p) for p in durable_primaries]

    for p in targets:
        try:
            size = p.stat().st_size
        except FileNotFoundError:
            continue                          # 未作成は正常（初回起動前など）
        except OSError as e:
            issues.append(f"{p.name}: stat できない — {e}")
            continue
        if size == 0:
            if not repair:
                issues.append(f"{p.name}: 0 バイト（--repair で削除して作り直す）")
                continue
            try:
                p.unlink()
                notes.append(f"{p.name}: 0 バイト -> 削除（次回書込で作り直される）")
            except OSError as e:
                issues.append(f"{p.name}: 0 バイト・削除できない — {e}")
            continue
        if p in durable_primaries:
            st, _ = durable_read_json(p)
            if st == "recovered":
                notes.append(f"{p.name}: primary より .bak が新しい（読取は .bak を採用）")
            elif st not in ("ok", "absent"):
                issues.append(f"{p.name}: primary/.bak とも JSON として読めない")
        elif p in plain:
            st, _ = read_json_classified(p)
            if st == "unreadable":
                issues.append(f"{p.name}: JSON として読めない")
    return issues, notes


def find_divergent_pairs(root: Path) -> list[tuple[Path, str, bool]]:
    """primary と .bak が食い違っている durable JSON を探す。

    → (path, 説明, 深刻か)。`.bak` が **absent** なのは「まだ一度も 2 コピー化されて
    いない」だけで次の書込で自然に解消するので深刻ではない。**unreadable**（0 バイト・
    破損）は冗長性が黙って壊れているので深刻。
    """
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn not in _DURABLE_NAMES:
                continue
            p = Path(dirpath) / fn
            pst, pdata = read_json_classified(p)
            bst, bdata = read_json_classified(bak_path(p))
            if pst != "ok" and bst != "ok":
                out.append((p, f"primary={pst} bak={bst}", True))
            elif pst != "ok":
                out.append((p, f"primary={pst}（.bak は健全 → 復旧可能）", True))
            elif bst == "absent":
                out.append((p, ".bak 未作成（次の保存で自動的に二重化される）", False))
            elif bst != "ok":
                out.append((p, f"bak={bst}（冗長性が黙って 1 コピーに劣化）", True))
            elif strip_seq(pdata) != strip_seq(bdata):
                st, _ = durable_read_json(p)
                newer = "primary" if st == "ok" else ".bak"
                out.append((p, f"内容が乖離（新しいのは {newer}）", True))
    return out


def check_toml(name: str) -> str | None:
    try:
        dataset_config.load_config(name)
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"
    return None


def find_orphan_tmps(cache: Path) -> list[Path]:
    if not cache.is_dir():
        return []
    out = []
    for dirpath, _dirs, files in os.walk(cache):
        out += [Path(dirpath) / fn for fn in files if ".tmp" in fn]
    return out


def tail_log_events(log: Path, *, lines: int = 4000) -> tuple[dict[str, int], str, str]:
    """→ (イベント別件数, 走査範囲の開始時刻, 最後に失敗が起きた時刻)。

    件数だけだと「過去の残骸」と「今も起きている」の区別がつかないので、最後の
    失敗イベントのタイムスタンプを一緒に返す。
    """
    counts = dict.fromkeys(_LOG_EVENTS, 0)
    if not log.is_file():
        return (counts, "", "")
    try:
        with open(log, "rb") as f:
            try:
                f.seek(-2_000_000, os.SEEK_END)     # 末尾 2MB だけ見る
            except OSError:
                f.seek(0)
            tail = f.read().decode("utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return (counts, "", "")
    last = ""
    for ln in tail:
        for ev in _LOG_EVENTS:
            if ev in ln:
                counts[ev] += 1
                last = ln[:19]
    since = tail[0][:19] if tail else ""
    return (counts, since, last)


def rescue_from_cache(root: Path, cache: Path, *, apply: bool) -> list[str]:
    """0 バイトのファイルを rclone キャッシュの孤児 tmp から復元する。

    孤児 tmp は「cache 層の rename が失敗して置き去りにされた**書けていたはずの中身**」
    なので、0 バイト化したファイルの復旧源になる（実測で 22,771 バイトの解析スクリプトを
    ここから回収した）。候補が複数あるときは mtime が最新の非空を採る。

    **キャッシュディレクトリには絶対に書かない**（rclone が "detected external removal"
    を起こす）。読むだけ。
    """
    msgs = []
    zeros = find_zero_byte_files(root)
    if not zeros:
        return msgs
    # 孤児 tmp はすべて `<元のファイル名>.<なにか>.tmp...` の形なので、元の名前を前方一致で
    # 引ける。remote 相対パスの対応付けはマウント構成に依存するため、名前の前方一致に加えて
    # 親ディレクトリ名の一致でも絞る（同名ファイルが複数の解析にあるため）。
    orphans: list[Path] = []
    for dirpath, _dirs, files in os.walk(cache):
        orphans += [Path(dirpath) / fn for fn in files if ".tmp" in fn]
    for z in zeros:
        cands = []
        for p in orphans:
            if not p.name.startswith(z.name) or p.parent.name != z.parent.name:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size > 0:
                cands.append((st.st_mtime, st.st_size, p))
        if not cands:
            msgs.append(f"0 バイト: {z}  -> 復旧候補なし")
            continue
        cands.sort()
        mtime, size, src = cands[-1]
        msgs.append(f"0 バイト: {z}\n        候補 {len(cands)} 件 -> {src.name} "
                    f"({size:,} バイト)")
        if apply:
            data = src.read_bytes()
            from common.paths import atomic_write_bytes
            atomic_write_bytes(z, data)
            msgs[-1] += f"  -> RESTORED ({len(data):,} バイト)"
    return msgs


def run(dataset: str | None = None, *, repair: bool = False, rescue: bool = False,
        cache: Path | None = None, log: Path | None = None) -> int:
    """健全性チェック。未解決の問題が残れば 1 を返す。"""
    cache = Path(cache) if cache else _DEFAULT_CACHE
    log = Path(log) if log else _DEFAULT_LOG
    problems = 0

    print("=== filesystem strategy ===")
    for name, d in _iter_datasets(dataset):
        if isinstance(d, Exception):
            print(f"  {name}: unresolvable — {d}")
            continue
        info = fs_kind.describe(d)
        print(f"  {name:24s} {info['kind']:8s} ({info['reason']})  {d}")

    print("\n=== per-dataset integrity ===")
    for name, d in _iter_datasets(dataset):
        if isinstance(d, Exception) or not d.is_dir():
            continue
        issues, notes = [], []
        toml_err = check_toml(name)
        if toml_err:
            issues.append(f"myanalysis.toml: {toml_err}")
        for msg in rescue_from_cache(d, cache, apply=rescue):
            (issues if "RESTORED" not in msg else notes).append(msg)
        for p, why, severe in find_divergent_pairs(d):
            line = f"{p.name}: {why}  ({p.parent})"
            if repair:
                st, data = durable_read_json(p)
                if st in ("ok", "recovered"):
                    durable_write_json(p, data)
                    notes.append(line + "  -> REPAIRED")
                    continue
                line += "  -> 修復不能（両コピーとも読めない）"
            (issues if severe else notes).append(line)
        if issues or notes:
            print(f"  [{name}]")
            for i in issues:
                print(f"     ! {i}")
            for n in notes:
                print(f"     - {n}")
            problems += len(issues)
        else:
            print(f"  [{name}] ok")

    print("\n=== PC ローカル状態 (data/llm_state) ===")
    ls_issues, ls_notes = check_local_state(repair=repair)
    for n in ls_notes:
        print(f"  - {n}")
    for i in ls_issues:
        print(f"  ! {i}")
    if not ls_issues and not ls_notes:
        print("  ok")
    problems += len(ls_issues)

    print("\n=== rclone cache / log ===")
    orphans = find_orphan_tmps(cache)
    if orphans:
        total = sum(p.stat().st_size for p in orphans if p.exists())
        print(f"  孤児 tmp: {len(orphans)} 個 / {total:,} バイト  ({cache})")
        print("     ↑ rename がキャッシュ層で失敗した回数とほぼ 1:1。0 バイト化した"
              " ファイルの中身がここに残っていることがある（復旧候補）")
    else:
        print(f"  孤児 tmp: なし  ({cache})")
    counts, since, last = tail_log_events(log)
    if any(counts.values()):
        print(f"  ログ {log.name}（{since} 以降を走査）:")
        for ev, n in counts.items():
            if n:
                print(f"     {n:5d}  {ev}")
        print(f"     最後の失敗: {last}")
        print("     ↑ この時刻が「今」に近いなら、まだ rename 経路が残っている。"
              "過去の日付なら修正前の残骸。")
    else:
        print(f"  ログに失敗イベントなし（{log}）")

    print(f"\n=== 未解決の問題: {problems} 件 ===")
    if problems:
        if not repair:
            print("  `doctor --repair`  : primary/.bak の乖離を newest-wins で収束")
        if not rescue:
            print("  `doctor --rescue`  : 0 バイトファイルをキャッシュの孤児 tmp から復元")
    return 1 if problems else 0
