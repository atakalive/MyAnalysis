# common/crashlog.py — Qt 非依存・stdlib only・ゼロ依存
from __future__ import annotations

import os
import sys
import threading
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import TracebackType

# クラッシュ扱いしない（＝ファイルに記録しない）例外。意図的終了 / Ctrl-C は
# ノイズなので残さず、委譲先（既定フック）に渡すだけにする。
_SKIP: tuple[type[BaseException], ...] = (KeyboardInterrupt, SystemExit)

# 1 プロセスあたりのクラッシュログ上限（無条件バックストップ。起動ごとにリセット）。
_MAX_LOG_FILES = 200


def _default_log_dir() -> Path:
    # <repo>/data/logs。crashlog.py は <repo>/common/ 直下なので parent.parent が repo root。
    return Path(__file__).resolve().parent.parent / "data" / "logs"


def _record(
    log_dir: Path,
    seen: set[object],
    lock: threading.Lock,
    thread_name: str,
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    tb: TracebackType | None,
) -> None:
    """未捕捉例外を <log_dir>/gui-crash-*.log に 1 件書く。全例外を握りつぶす。"""
    try:
        if exc_type is not None and issubclass(exc_type, _SKIP):
            return  # 意図的終了はクラッシュではない
        # dedup キー = メッセージ非依存の site シグネチャ（型 + フレーム位置）。
        # 動的メッセージでも同一箇所は同一キー。ファイル本文には full traceback を書く。
        frames = tuple((f.filename, f.lineno) for f in traceback.extract_tb(tb))
        sig = (
            getattr(exc_type, "__module__", ""),
            getattr(exc_type, "__qualname__", ""),
            frames,
        )
        # 判定・予約は lock 内で不可分に（並行クラッシュでの同一 site 重複・上限超過を防ぐ）。
        with lock:
            if sig in seen:
                return  # 同一 site は初回のみ
            if len(seen) >= _MAX_LOG_FILES:
                return  # 無条件バックストップ
            seen.add(sig)  # 予約（他スレッドの同一 sig / 上限超過をここで弾く）
        # 書き込みは lock 外（I/O 中はロックを保持しない）。失敗したら予約を解除。
        wrote = False
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            text = "".join(traceback.format_exception(exc_type, exc, tb))
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            pid = os.getpid()
            header = f"Uncaught exception (thread {thread_name})"
            # %f 単独では一意にならない（同時発火・時刻逆戻り）。pid + 連番 + x モード
            # （排他作成）で開き、衝突したら次の連番へ retry。既存ログを上書きしない。
            for i in range(1000):
                path = log_dir / f"gui-crash-{stamp}-{pid}-{i}.log"
                try:
                    with open(path, "x", encoding="utf-8") as f:
                        f.write(header + "\n")
                        f.write(text)
                    wrote = True
                    break
                except FileExistsError:
                    continue
        finally:
            if not wrote:
                with lock:
                    seen.discard(sig)  # 書けなかった予約を解除（次回再試行可能に）
    except BaseException:  # noqa: BLE001 — ログ失敗で二次クラッシュしない
        pass


def _chain(prev: Callable[..., object], *args: object) -> None:
    try:
        prev(*args)
    except BaseException:  # noqa: BLE001 — pythonw で stderr 無効等でも二次被害を出さない
        pass


def install(log_dir: Path | None = None) -> None:
    # 冪等ガード: 既に自前フックが載っていれば多重ラップしない。`is True` 比較は
    # Mock 等が任意属性に truthy を返す事故を避けるため。
    if getattr(sys.excepthook, "_crashlog_installed", False) is True:
        return

    log_dir = log_dir or _default_log_dir()
    seen: set[object] = set()  # install ごとに新規（プロセス寿命で dedup／テスト間で非共有）
    lock = threading.Lock()

    prev_sys = sys.excepthook

    def _hook(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        _record(
            log_dir, seen, lock, threading.current_thread().name,
            exc_type, exc, tb,
        )
        _chain(prev_sys, exc_type, exc, tb)

    _hook._crashlog_installed = True  # type: ignore[attr-defined]
    sys.excepthook = _hook

    prev_thread = threading.excepthook

    def _thook(args: threading.ExceptHookArgs) -> None:
        # args の属性アクセスは _record の外なので、ここも防御的に握りつぶす。
        try:
            thread = getattr(args, "thread", None)
            name = thread.name if thread is not None else "<unknown>"
            _record(
                log_dir, seen, lock, name,
                getattr(args, "exc_type", None),
                getattr(args, "exc_value", None),
                getattr(args, "exc_traceback", None),
            )
        except BaseException:  # noqa: BLE001 — フック本体を二次クラッシュさせない
            pass
        _chain(prev_thread, args)

    threading.excepthook = _thook
