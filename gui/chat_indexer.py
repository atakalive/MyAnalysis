"""チャットの関連検索の索引を、GUI の裏で差分更新する（Issue #126。実験的・既定オフ）。

Qt 非依存。``gui/config_push.py`` の ``ConfigPusher`` と同じ形: 1 本のデーモンスレッドで
``chat_index.ChatIndex.open().update(blocking=False)`` を走らせ、要求はデバウンス（最後の要求から
``DEBOUNCE_S`` 秒後に 1 回）でまとめる。``request_update`` のたびに ``chat_index.is_ready()`` を読み直す
（設定のウインドウで有効にした直後から効く）。ロックが取れない（CLI・ほかの MyAnalysis が更新中）なら
何もしない。成功は無音、失敗は debug ログだけ（ステータスバーを騒がせない）。

設定のウインドウを開いている間は ``hold()`` で止める（ウインドウの操作と索引のロック・モデルの
ファイルを取り合わないように）。hold の間の要求は覚えておき、``release()`` の後に 1 回だけ走らせる。
hold した時点で走っている更新には、その回が終わるまで中止を返し（次のバッチの境目で止まる。中止した
更新は何も書かない）、終わった後で予約し直す。ウインドウの操作は ``wait_idle()`` で止まるのを待つ。

状態は 1 つの Condition で守る。不変条件:
- ``_deadline`` が値を持つのは「要求があり（``_wanted``）、hold が無く、stop していない」ときだけ。
- 予約（``_arm``）はそれを満たしたときだけ締め切りを置き、ワーカーがいなければ起こす。ワーカーは
  締め切りが無くなったら抜ける（hold・stop・要求なし）。
- 中止で終わった回は、ワーカー自身が要求を立て直して ``_arm`` する。hold 中なら予約は残り、
  ``release()`` が拾う。release が先に済んでいれば、ここで予約される（どちらの順でも 1 回だけ走る）。

stop() は締め切り、走っている回に中止を返し、待っているワーカーを起こしてから最大 wait_s 秒待つ
（期限を過ぎたらデーモンのまま残す）。停止の経路は ToolWindow._stop_chat_indexer（closeEvent の
accept 経路と hot reload Tier 3 の再構築）。

``chat_index`` は GUI スレッドの ``request_update`` で import してワーカーへ渡す（ワーカーで import の
連鎖を起こさない）。ワーカーは ``config.DATASETS``（GUI と共有の dict）を書き換えない（``reload=False``）。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

_log = logging.getLogger(__name__)

THREAD_NAME = "myanalysis-chat-index"


class ChatIndexer:
    DEBOUNCE_S = 5.0       # 最後の要求から走らせるまで（実行時に読む。テストが差し替える）
    DEFAULT_WAIT_S = 5.0   # stop() が走っている回を待つ上限
    _POLL_S = 0.1          # wait_idle が中止を見る間隔

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._thread: threading.Thread | None = None
        self._ci = None               # llm_bridge.chat_index（GUI スレッドで import したもの）
        self._stopped = False
        self._held = 0
        self._wanted = False          # まだ走らせていない要求がある
        self._deadline: float | None = None
        self._alive = False           # ワーカーのループが生きている
        self._active = False          # update を呼んでいる最中
        self._cancel_run = False      # 今の 1 回の中止（走り始めで落とし、終わるまで保つ）

    # ---------------------------------------------------------------- 要求

    def request_update(self, reason: str = "") -> bool:
        """差分更新を要求する（GUI スレッドから）。使える状態でなければ False。例外は外へ出さない。"""
        try:
            with self._cv:
                if self._stopped:
                    return False
            from llm_bridge import chat_index
            if not chat_index.is_ready():
                return False
            with self._cv:
                if self._stopped:
                    return False
                self._ci = chat_index
                self._wanted = True
                self._arm()
            return True
        except Exception:
            _log.debug("chat index update request failed (%s)", reason, exc_info=True)
            return False

    def _arm(self) -> None:
        """（_cv の中で）要求があり、hold も stop も無ければ締め切りを置き、ワーカーを起こす。"""
        if self._stopped or not self._wanted or self._held > 0:
            return
        self._deadline = time.monotonic() + self.DEBOUNCE_S
        if not self._alive:
            t = threading.Thread(target=self._run, name=THREAD_NAME, daemon=True)
            self._alive = True
            self._thread = t
            try:
                t.start()
            except Exception:
                self._alive = False
                self._thread = None
                self._deadline = None          # 要求は残す（次の要求で起こし直す）
                _log.debug("chat index worker failed to start", exc_info=True)
                return
        self._cv.notify_all()

    # ---------------------------------------------------------------- hold

    def hold(self) -> None:
        """背景の更新を止める（入れ子にできる）。走っている回には中止を返す。"""
        with self._cv:
            self._held += 1
            self._deadline = None
            if self._active:
                self._cancel_run = True
            self._cv.notify_all()

    def release(self) -> None:
        """hold を 1 つ外す。全部外れたら、止めていた間の要求を予約する。"""
        with self._cv:
            if self._held > 0:
                self._held -= 1
            if self._held == 0:
                self._arm()

    def wait_idle(self, timeout: float, cancelled: Callable[[], bool] | None = None) -> bool:
        """走っている回が終わるまで待つ。終われば True、期限切れか cancelled() で False。

        hold の中で呼ぶこと（hold の間は新しい回が始まらないので、True の後は空いたまま）。"""
        end = time.monotonic() + timeout
        with self._cv:
            while self._active:
                if cancelled is not None and cancelled():
                    return False
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(min(left, self._POLL_S))
            return True

    def is_running(self) -> bool:
        with self._cv:
            return self._active

    def has_pending(self) -> bool:
        with self._cv:
            return self._wanted

    # ---------------------------------------------------------------- ワーカー

    def _should_cancel(self) -> bool:
        return self._stopped or self._cancel_run

    def _next_run(self):
        """（_cv の中で）締め切りまで待ち、走らせる回なら chat_index を、抜けるなら None を返す。"""
        while True:
            if self._stopped or self._deadline is None:
                self._alive = False
                self._cv.notify_all()
                return None
            left = self._deadline - time.monotonic()
            if left > 0:
                self._cv.wait(left)
                continue
            self._deadline = None
            self._wanted = False
            self._active = True
            self._cancel_run = False
            return self._ci

    def _run(self) -> None:
        while True:
            with self._cv:
                ci = self._next_run()
            if ci is None:
                return
            cancelled = self._update_once(ci)
            with self._cv:
                interrupted = cancelled or self._cancel_run
                self._active = False
                self._cancel_run = False
                if interrupted and not self._stopped:
                    self._wanted = True        # 止めた回は、hold が外れてから走り直す
                    self._arm()
                self._cv.notify_all()

    def _update_once(self, ci) -> bool:
        """1 回の差分更新。中止で終わったら True。例外は外へ出さない。"""
        try:
            if not ci.is_ready():              # 締め切りまでの間に無効にされた
                return False
            rep = ci.ChatIndex.open().update(progress=None, cancel=self._should_cancel,
                                             blocking=False, reload=False)
            return bool(getattr(rep, "cancelled", False))
        except Exception:
            _log.debug("background chat index update failed", exc_info=True)
            return False

    # ---------------------------------------------------------------- 停止

    def stop(self, wait_s: float | None = None) -> bool:
        """新しい要求を締め切り、走っている回に中止を返し、最大 wait_s 秒待つ（冪等）。

        間に合えば True。間に合わなければ False（ワーカーはデーモンのまま完走させる）。
        wait_s 省略時は DEFAULT_WAIT_S（テストがクラス属性を差し替えられるよう実行時に読む）。"""
        if wait_s is None:
            wait_s = self.DEFAULT_WAIT_S
        with self._cv:
            self._stopped = True
            self._wanted = False
            self._deadline = None
            if self._active:
                self._cancel_run = True
            self._cv.notify_all()
            t = self._thread
        if t is None or t is threading.current_thread():
            return True
        t.join(wait_s)
        if t.is_alive():
            _log.debug("chat index update still running after stop; left as a daemon thread")
            return False
        return True
