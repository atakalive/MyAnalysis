"""ヘルプ → MyAnalysis を更新（SelfUpdater）。本体は Qt 非依存の common/self_update.py。

手動のみ: 押したときだけ ``self_update.run_update`` を 1 回走らせ、その結果を知らせる。起動時・定期の
確認や「更新があります」の通知はしない。

- チャットの応答中は始めない（エージェントが ``python -m llm_bridge`` を動かしている間にコードを
  書き換えない）。走っている間はメニューの項目を無効にし、2 本目を走らせない。
- git / pip は daemon スレッドで動かし（``_spawn``。テストは同期で動かす）、結果と進み具合は
  シグナルで GUI スレッドに戻す。ウィンドウが先に消えていたら（アプリ再構築など）emit の
  RuntimeError を握る（git / pip の子はそのまま最後まで走る）。
- 新しい版を取ったとき・前に取って未反映のとき（起動時の HEAD ≠ 今の HEAD）だけ、再起動するかを
  訊く（既定は [再起動]、Esc は [あとで]）。[再起動] は既存の Tier 4（``HotReloadController.restart``。
  セッションを保存して ``tool.py --resume-session`` を起動し、終了する）。
- pip が失敗したら再起動は勧めない（足りないパッケージのまま新しいコードを起動しない）。
- ダイアログは ``show_message`` / ``ask_restart`` から出す（offscreen では exec() が返らないので、
  テストが差し替える）。文は全部 ``tr()``。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox

from common import self_update
from common.git import clip_output, read_commit_time
from common.i18n import tr
from common.paths import repo_root
from gui.about_dialog import startup_head, version_label

_log = logging.getLogger(__name__)

THREAD_NAME = "myanalysis-self-update"
LATER_NOTE_MS = 15000


def show_message(win, kind: str, text: str, details: str = "") -> None:
    """結果を知らせる（kind は "info" / "warning"）。details は「詳細」欄（git / pip の出力）。"""
    icon = QMessageBox.Icon.Warning if kind == "warning" else QMessageBox.Icon.Information
    box = QMessageBox(icon, tr("update.title"), text, QMessageBox.StandardButton.Ok, win)
    if details:
        box.setDetailedText(details)
    try:
        box.exec()
    finally:
        box.deleteLater()


def ask_restart(win, text: str) -> bool:
    """再起動して反映するか訊く。[再起動] なら True（既定）、[あとで]・Esc なら False。"""
    box = QMessageBox(QMessageBox.Icon.Question, tr("update.title"), text,
                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, win)
    box.button(QMessageBox.StandardButton.Yes).setText(tr("update.restart"))
    box.button(QMessageBox.StandardButton.No).setText(tr("update.later"))
    box.setDefaultButton(QMessageBox.StandardButton.Yes)
    box.setEscapeButton(QMessageBox.StandardButton.No)
    try:
        return box.exec() == QMessageBox.StandardButton.Yes
    finally:
        box.deleteLater()


def _clip(text: str) -> str:
    return clip_output(text.strip(), marker=tr("update.output_clipped"))


class SelfUpdater(QObject):
    """ToolWindow の「MyAnalysis を更新」。action は走っている間に無効にするメニューの項目。"""

    _finished = Signal(object)      # self_update.UpdateResult
    _progress = Signal(str)

    def __init__(self, win, action=None):
        super().__init__(win)
        self._win = win
        self._action = action
        self._running = False
        self._status_text = ""
        self._finished.connect(self._on_finished)
        self._progress.connect(self._on_progress)

    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        """メニューから（QTimer.singleShot(0, …) で、メニューが閉じてから）。"""
        if self._running:
            return
        win = self._win
        if win._chat_busy():
            show_message(win, "info", tr("update.busy"))
            return
        self._running = True
        self._set_enabled(False)
        self._show_status(tr("update.status.fetch"))
        repo, running = repo_root(), startup_head()
        try:
            self._spawn(lambda: self._work(repo, running))
        except Exception as e:  # noqa: BLE001 — スレッドを起動できなかった
            _log.exception("could not start the update thread")
            self._done()
            show_message(win, "warning", tr("update.fetch_failed", url=self_update.UPDATE_URL),
                         f"{type(e).__name__}: {e}")

    def _spawn(self, fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, name=THREAD_NAME, daemon=True).start()

    def _work(self, repo, running: str | None) -> None:
        """実行しているスレッドで。run_update は投げない。"""
        result = self_update.run_update(repo, running=running, progress=self._emit_progress)
        try:
            self._finished.emit(result)
        except RuntimeError:        # 先にウィンドウ（と、この QObject）が消えた
            _log.warning("update finished after the window was gone: %s", result.status)

    def _emit_progress(self, stage: str) -> None:
        try:
            self._progress.emit(stage)
        except RuntimeError:
            pass

    def _on_progress(self, stage: str) -> None:
        if stage == "pip" and self._running:
            self._show_status(tr("update.status.pip"))

    def _on_finished(self, result) -> None:
        self._done()
        try:
            self._report(result)
        except Exception:  # noqa: BLE001 — スロットの中。投げると PySide が捨てる
            _log.exception("could not report the update result")

    def _done(self) -> None:
        self._running = False
        self._set_enabled(True)
        bar = self._win.statusBar()
        if self._status_text and bar.currentMessage() == self._status_text:
            bar.clearMessage()
        self._status_text = ""

    def _set_enabled(self, on: bool) -> None:
        if self._action is not None:
            self._action.setEnabled(on)

    def _show_status(self, text: str) -> None:
        self._status_text = text
        self._win.statusBar().showMessage(text)

    # ----- 結果 -----

    def _report(self, r) -> None:
        win = self._win
        if r.status == "no-git":
            show_message(win, "warning", tr("update.no_git"))
            return
        if r.status == "not-repo":
            show_message(win, "warning", tr("update.not_repo"))
            return
        if r.status == "wrong-branch":
            if r.branch is None:
                text = tr("update.detached", expected=self_update.UPDATE_BRANCH)
            else:
                text = tr("update.wrong_branch", branch=r.branch,
                          expected=self_update.UPDATE_BRANCH)
            show_message(win, "warning", text)
            return
        if r.status != "done":
            if r.stage == "merge":
                text = tr("update.merge_failed")
            else:
                text = tr("update.fetch_failed", url=self_update.UPDATE_URL)
            if r.timed_out:
                text += "\n\n" + tr("update.timed_out")
            if r.left_running:
                text += "\n\n" + tr("update.left_running")
            show_message(win, "warning", text, _clip(r.output))
            return

        repo = repo_root()
        start = startup_head()
        old_head = start or r.before
        old = version_label(old_head, read_commit_time(repo, old_head))
        new = version_label(r.after, read_commit_time(repo, r.after))
        if r.pip == "failed":
            text = tr("update.pip_failed", version=new)
            if r.pip_timed_out:
                text += "\n\n" + tr("update.timed_out")
            show_message(win, "warning", text, _clip(r.pip_output))
            return
        if r.advanced:
            text = tr("update.updated", new=new, old=old)
        elif start is not None and start != r.after:
            text = tr("update.fetched_earlier", new=new, old=old)
        else:
            show_message(win, "info", tr("update.up_to_date", version=new))
            return
        if ask_restart(win, text):
            self._restart()
        else:
            win.statusBar().showMessage(tr("update.later_note"), LATER_NOTE_MS)

    def _restart(self) -> None:
        win = self._win
        controller = getattr(win, "_hotreload", None)
        if controller is None:
            show_message(win, "info", tr("update.no_restart"))
            return
        res = controller.restart()
        if isinstance(res, str) and res.startswith("reload-busy"):
            show_message(win, "info", tr("update.restart_busy"))
