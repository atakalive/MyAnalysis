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


# ---------------------------------------------------------------------------
# 起動失敗の通知とファイルログ（Issue #99 C-2）。
# 起動失敗時は i18n（tomllib に依存）が使えないので、文言は英日併記の定数にする。
# ---------------------------------------------------------------------------

LOG_FILE_NAME = "myanalysis.log"

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(process)d]: %(message)s"

STARTUP_TITLE = "MyAnalysis: startup failed / 起動に失敗しました"

_MSG_TOO_OLD = (
    "MyAnalysis needs Python 3.11 or newer (running {version}).\n"
    "MyAnalysis には Python 3.11 以上が必要です（実行中: {version}）。"
)
_MSG_MISSING_MODULE = (
    "A required package is missing: {module}\n"
    "Install the dependencies: pip install -r requirements.txt\n"
    "必要なパッケージがありません: {module}\n"
    "依存パッケージを入れてください: pip install -r requirements.txt"
)
_MSG_REGISTRY = (
    "The dataset registry datasets.local.json is corrupt:\n{error}\n"
    "Move it aside (e.g. datasets.local.json.corrupt) and fix it by hand, or run "
    "'python -m llm_bridge config-pull' after moving it if you use R2 sync.\n"
    "登録簿 datasets.local.json が壊れています: {error}\n"
    "別名（例: datasets.local.json.corrupt）に退避して手で直すか、R2 同期を使って"
    "いるなら退避した後に 'python -m llm_bridge config-pull' を実行してください。"
)
_MSG_GENERIC = (
    "{error}\n"
    "An error occurred during startup.\n"
    "起動中にエラーが起きました。"
)
_MSG_DETAILS = "\n\nDetails / 詳細: {log_dir}"


def install_file_logging(log_dir: Path | None = None) -> None:
    """ルートロガーに <log_dir>/myanalysis.log（WARNING 以上）を付ける。冪等・never raise。

    ルートロガー自身のレベルは変えない。stderr があればコンソールにも同じ書式で
    出す（ファイルハンドラを付けると logging.lastResort が働かなくなるため）。

    既知の制限: Windows では、他のプロセスがログファイルを開いている間は回転の
    rename が PermissionError になる。shouldRollover は真のままなので、1 MB を
    超えた後は、複数のプロセス（Tier 4 restart の重なり、GUI の多重起動）が同時に
    動いている間の記録がすべて落ちる。1 プロセスだけになれば次の記録で回転する。
    """
    try:
        import logging
        import logging.handlers

        root = logging.getLogger()
        if any(getattr(h, "_myanalysis_logging", False) is True for h in root.handlers):
            return
        log_dir = log_dir or _default_log_dir()
        formatter = logging.Formatter(_LOG_FORMAT)
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_dir / LOG_FILE_NAME, maxBytes=1_000_000, backupCount=3,
                encoding="utf-8", delay=True,
            )
            fh.setLevel(logging.WARNING)
            fh.setFormatter(formatter)
            fh._myanalysis_logging = True  # type: ignore[attr-defined]
            root.addHandler(fh)
        except Exception:  # noqa: BLE001 — ログの保存は best-effort
            pass
        if sys.stderr is not None:
            sh = logging.StreamHandler(sys.stderr)
            sh.setLevel(logging.WARNING)
            sh.setFormatter(formatter)
            sh._myanalysis_logging = True  # type: ignore[attr-defined]
            root.addHandler(sh)
    except Exception:  # noqa: BLE001 — never raise
        pass


def _user32():
    """Windows の user32（それ以外は None）。テストはこれを差し替える。"""
    if sys.platform != "win32":
        return None
    import ctypes

    return ctypes.windll.user32  # type: ignore[attr-defined]


def _windows_message_box(title: str, text: str) -> bool:
    """Windows ならメッセージボックスを出し、表示できたら True。それ以外・失敗は False。

    MessageBoxW は失敗を例外ではなく戻り値 0 で返す（表示できたときは押したボタンの
    ID で非 0）。0 を成功扱いにすると、show_fatal が stderr への代替通知を飛ばす。
    """
    try:
        user32 = _user32()
        if user32 is None:
            return False
        return user32.MessageBoxW(None, text, title, 0x10) != 0
    except Exception:  # noqa: BLE001
        return False


def show_fatal(title: str, text: str) -> None:
    """起動失敗を利用者に知らせる（Windows はダイアログ、他は stderr）。never raise。"""
    try:
        if _windows_message_box(title, text):
            return
        if sys.stderr is not None:
            sys.stderr.write(f"{title}\n{text}\n")
            sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


def require_python(
    minimum: tuple[int, int] = (3, 11), *, current: tuple[int, ...] | None = None,
) -> None:
    """Python が minimum 未満ならダイアログを出して終了する（ログには残さない）。"""
    if current is None:
        current = tuple(sys.version_info[:3])
    if tuple(current) >= tuple(minimum):
        return
    version = ".".join(str(x) for x in current)
    show_fatal(STARTUP_TITLE, _MSG_TOO_OLD.format(version=version))
    sys.exit(1)


def report_startup_failure(exc: BaseException) -> None:
    """起動時の例外を記録し（install 済みの excepthook 経由）、原因を示す。never raise。"""
    try:
        try:
            sys.excepthook(type(exc), exc, exc.__traceback__)
        except Exception:  # noqa: BLE001
            pass
        registry = sys.modules.get("dataset_registry")
        registry_error = getattr(registry, "RegistryError", None)
        if isinstance(registry_error, type) and isinstance(exc, registry_error):
            text = _MSG_REGISTRY.format(error=exc)
        elif isinstance(exc, ModuleNotFoundError):
            text = _MSG_MISSING_MODULE.format(module=exc.name or str(exc))
        else:
            text = _MSG_GENERIC.format(error=f"{type(exc).__name__}: {exc}")
        text += _MSG_DETAILS.format(log_dir=_default_log_dir())
        show_fatal(STARTUP_TITLE, text)
    except Exception:  # noqa: BLE001
        pass
