"""GUI での登録・登録削除の直後に R2 へ送る（送信のみ・バックグラウンド）。Issue #98.

Qt 非依存。ConfigPusher は config_share.try_push() を 1 本のデーモンスレッドで実行し、
実行中の要求は「終了直前にもう 1 回」にまとめる（1 つの ConfigPusher が同時に走らせる
スレッドは高々 1 本）。hot reload で pusher が世代交代しても、push 本体は
config_share.try_push が PC ローカルの push ロックで直列化する。

push は止められない同期 I/O（DNS・ソケット read・ロック待ち）を含むので所要時間を有界と
仮定しない。デーモンスレッドなのでプロセス終了を妨げず、Qt が走行中のスレッドを破棄する
こともない。stop() は新しい要求を締め切って pending を捨て、走行中の push を最大 wait_s 秒
待つだけ（中断はしない）。停止の経路は ToolWindow._stop_config_pusher（closeEvent の
accept 経路と hot reload Tier 3 の再構築）。

push 関数は GUI スレッドの request() で解決してスレッドへ渡す（worker スレッドで
config_share → config の import 連鎖を起こさない）。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

_log = logging.getLogger(__name__)

THREAD_NAME = "myanalysis-config-push"


class ConfigPusher:
    DEFAULT_WAIT_S = 5.0   # stop() が走行中の push を待つ上限（通常の push は 1 s 未満）

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False     # worker が push ループの中（終了判定の前）にいる間 True
        self._pending = False
        self._stopped = False

    def request(self) -> bool:
        """push を要求する（GUI スレッドから）。例外は外へ出さない。"""
        try:
            with self._lock:
                if self._stopped:
                    return False
            import config_share
            if not config_share.autosync_enabled():
                return False
            push = config_share.try_push
            log = config_share._log_debug
            with self._lock:
                if self._stopped:
                    return False
                if self._running:
                    self._pending = True
                    return True
                t = self._make_thread(push, log)
                self._running = True
                self._pending = False
                self._thread = t
                try:
                    t.start()
                except Exception:
                    self._running = False
                    self._thread = None
                    raise
            return True
        except Exception:
            _log.debug("config push request failed", exc_info=True)
            return False

    def _make_thread(self, push: Callable[[], object],
                     log: Callable[[str], None]) -> threading.Thread:
        return threading.Thread(target=self._run, args=(push, log),
                                name=THREAD_NAME, daemon=True)

    def _run(self, push: Callable[[], object], log: Callable[[str], None]) -> None:
        while True:
            try:
                push()
            except Exception as exc:        # try_push は never-raise だが二重防御
                try:
                    log(f"background push failed: {exc}")
                except Exception:
                    pass
            with self._lock:
                if self._pending and not self._stopped:
                    self._pending = False
                    continue
                self._running = False
                return

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def has_pending(self) -> bool:
        with self._lock:
            return self._pending

    def stop(self, wait_s: float | None = None) -> bool:
        """新しい要求を締め切り pending を捨て、走行中の push を最大 wait_s 秒待つ（冪等）。

        間に合えば True。間に合わなければ False（worker はデーモンのまま完走させる）。
        wait_s 省略時は DEFAULT_WAIT_S（テストがクラス属性を差し替えられるよう実行時に読む）。
        """
        if wait_s is None:
            wait_s = self.DEFAULT_WAIT_S
        with self._lock:
            self._stopped = True
            self._pending = False
            t = self._thread
        if t is None:
            return True
        t.join(wait_s)
        if t.is_alive():
            _log.debug("config push still running after stop; left as a daemon thread")
            return False
        return True
