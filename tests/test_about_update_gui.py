"""ヘルプ → MyAnalysis を更新（gui/updater.py）とバージョン情報…（gui/about_dialog.py）。offscreen Qt。

run_update とダイアログ（show_message / ask_restart / AboutDialog.exec）は差し替える
（offscreen では exec() が返らない）。本体の手順は tests/test_self_update.py が本物の git で見る。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from common.self_update import UPDATE_URL, UpdateResult

HEAD_A = "abcdef1" + "0" * 33
HEAD_B = "1234567" + "f" * 33
JST = timezone(timedelta(hours=9))


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    from gui.about_dialog import set_startup_head
    set_startup_head(app, None)


@pytest.fixture()
def ja():
    from common import i18n
    i18n._ensure_loaded()
    saved = i18n._active
    i18n._active = "ja"
    yield
    i18n._active = saved


@pytest.fixture()
def win(qapp, ja, monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    from gui.window import ToolWindow
    w = ToolWindow()
    yield w
    # close() は closeEvent が保存の確認を出すので呼ばない。
    w._close_all_floats()
    w.hide()
    w.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


# ----- バージョン情報 -----

def test_version_label_and_text(qapp, ja):
    from gui.about_dialog import version_label, version_line, version_text
    when = datetime(2026, 1, 5, 0, 30, tzinfo=JST)
    assert version_label(HEAD_A, when) == "2026.01.05 (abcdef1)"
    # 日付は記録された時差のまま（UTC に直すと前の日になる）
    assert version_label(HEAD_A, datetime(2026, 10, 27, 4, 0, tzinfo=JST)) == "2026.10.27 (abcdef1)"
    assert version_label(HEAD_A) == "abcdef1"
    assert version_label(None) == "不明"
    assert version_label(None, when) == "不明"
    assert version_line(HEAD_A, when) == "バージョン: 2026.01.05 (abcdef1)"
    assert version_text(HEAD_A, when) == "MyAnalysis\nバージョン: 2026.01.05 (abcdef1)"


def test_version_text_english(qapp):
    from common import i18n
    from gui.about_dialog import version_text
    i18n._ensure_loaded()
    saved, i18n._active = i18n._active, "en"
    try:
        assert version_text(HEAD_A) == "MyAnalysis\nVersion: abcdef1"
        assert version_text(None) == "MyAnalysis\nVersion: unknown"
    finally:
        i18n._active = saved


def test_startup_head_property(qapp):
    from gui.about_dialog import set_startup_head, startup_head
    assert startup_head() is None
    set_startup_head(qapp, HEAD_A)
    assert startup_head() == HEAD_A
    set_startup_head(qapp, None)
    assert startup_head() is None


@pytest.fixture()
def about_fakes(monkeypatch):
    from gui import about_dialog
    fakes = {"copied": [], "opened": []}
    monkeypatch.setattr(about_dialog, "_set_clipboard", fakes["copied"].append)
    monkeypatch.setattr(about_dialog, "_open_url", fakes["opened"].append)
    return fakes


def test_about_dialog_copy_and_author_link(qapp, ja, about_fakes):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent, QTextDocument
    from PySide6.QtWidgets import QDialog

    from gui.about_dialog import _LINK_COLOR, AUTHOR, AUTHOR_URL, AboutDialog

    def key(widget, k):
        qapp.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, k, Qt.KeyboardModifier.NoModifier))

    committed = datetime(2026, 10, 27, 4, 13, tzinfo=JST)
    dlg = AboutDialog(HEAD_A, committed)
    dlg.show()
    qapp.processEvents()
    assert dlg.windowTitle() == "MyAnalysis のバージョン情報"
    assert dlg._version.text() == "バージョン: 2026.10.27 (abcdef1)"
    text = dlg._author.text()
    assert f'href="{AUTHOR_URL}"' in text and ">atakalive<" in text and text.endswith(" (GitHub)")
    assert dlg._author.toolTip() == AUTHOR_URL and not dlg._author.openExternalLinks()
    v, b = dlg._version.geometry(), dlg._copy_btn.geometry()
    assert b.left() > v.right() and b.top() < v.bottom() and b.bottom() > v.top()   # [コピー] は行の右
    assert dlg._close_btn.text() == "閉じる" and dlg._close_btn.isDefault()
    assert not dlg._copy_btn.autoDefault()
    assert dlg.focusWidget() is dlg._close_btn
    doc = QTextDocument()
    doc.setHtml(dlg._author.text())
    fmt = doc.find(AUTHOR).charFormat()
    assert fmt.isAnchor() and fmt.anchorHref() == AUTHOR_URL
    assert fmt.foreground().color().name() == _LINK_COLOR

    width = dlg._copy_btn.width()
    dlg._copy_btn.click()
    qapp.processEvents()
    assert about_fakes["copied"] == ["MyAnalysis\nバージョン: 2026.10.27 (abcdef1)"]
    assert dlg.isVisible() and dlg._copy_btn.text() == "コピーしました"
    assert dlg._copy_btn.width() == width       # 文字が変わっても行が動かない
    dlg._author.linkActivated.emit(AUTHOR_URL)
    assert about_fakes["opened"] == [AUTHOR_URL]

    dlg._copy_btn.setFocus()
    key(dlg._copy_btn, Qt.Key.Key_Return)       # フォーカスが [コピー] でも Return は [閉じる]
    assert not dlg.isVisible() and dlg.result() == QDialog.DialogCode.Rejected
    assert len(about_fakes["copied"]) == 1
    dlg.deleteLater()


def test_help_menu_items_and_retranslate(win):
    from common import i18n
    actions = [a for a in win._help_menu.actions() if not a.isSeparator()]
    assert actions == [win._update_action, win._about_action]
    assert [a.text() for a in actions] == ["MyAnalysis を更新", "バージョン情報…"]
    i18n._active = "en"
    win.retranslate()
    assert [a.text() for a in actions] == ["Update MyAnalysis", "About MyAnalysis…"]


def test_about_menu_shows_start_head(win, qapp, monkeypatch):
    from gui import window as W
    from gui.about_dialog import AboutDialog, set_startup_head

    shown, asked = [], []
    when = datetime(2026, 10, 27, 4, 13, tzinfo=JST)

    def commit_time(repo, commit):
        asked.append(commit)
        return when if commit == HEAD_A else None

    monkeypatch.setattr(AboutDialog, "exec", lambda self: shown.append(self.copy_text()) or 0)
    monkeypatch.setattr(W, "read_commit_time", commit_time)
    set_startup_head(qapp, HEAD_A)
    win._about_action.trigger()
    set_startup_head(qapp, HEAD_B)
    win._about_action.trigger()
    set_startup_head(qapp, None)
    win._about_action.trigger()
    assert shown == ["MyAnalysis\nバージョン: 2026.10.27 (abcdef1)",
                     "MyAnalysis\nバージョン: 1234567",
                     "MyAnalysis\nバージョン: 不明"]
    assert asked == [HEAD_A, HEAD_B, None]


def test_about_reads_this_repo(win, qapp, monkeypatch):
    """差し替えない read_commit_time で、このリポジトリの HEAD の日付が付く。"""
    from common.git import read_head
    from common.paths import repo_root
    from gui.about_dialog import AboutDialog, set_startup_head

    head = read_head(repo_root())
    if head is None:
        pytest.skip("not a git checkout")
    shown = []
    monkeypatch.setattr(AboutDialog, "exec", lambda self: shown.append(self.copy_text()) or 0)
    set_startup_head(qapp, head)
    win._about_action.trigger()
    assert shown[0].startswith("MyAnalysis\nバージョン: 20") and shown[0].endswith(f"({head[:7]})")


# ----- MyAnalysis を更新 -----

class _FakeHotReload:
    def __init__(self, reply="reload-scheduled"):
        self.reply = reply
        self.calls = 0

    def restart(self):
        self.calls += 1
        return self.reply


@pytest.fixture()
def upd(win, qapp, monkeypatch):
    """SelfUpdater を同期で動かし、run_update とダイアログを差し替える。"""
    from gui import updater as U
    from gui.about_dialog import set_startup_head

    state = {"result": UpdateResult("done", before=HEAD_A, after=HEAD_A), "runs": [],
             "messages": [], "asks": [], "answer": True, "during": None}

    def run_update(repo, *, running=None, progress=None):
        state["runs"].append((repo, running))
        if state["during"] is not None:
            state["during"](progress)
        return state["result"]

    def show_message(w, kind, text, details=""):
        state["messages"].append((kind, text, details))

    def ask_restart(w, text):
        state["asks"].append(text)
        return state["answer"]

    monkeypatch.setattr(U.self_update, "run_update", run_update)
    monkeypatch.setattr(U, "show_message", show_message)
    monkeypatch.setattr(U, "ask_restart", ask_restart)
    monkeypatch.setattr(U, "read_commit_time", lambda repo, commit: None)
    monkeypatch.setattr(win._self_updater, "_spawn", lambda fn: fn())
    set_startup_head(qapp, HEAD_A)
    state["win"] = win
    state["updater"] = win._self_updater
    return state


def test_update_runs_on_menu_after_the_menu_closes(upd, qapp):
    from common.paths import repo_root
    upd["win"]._update_action.trigger()
    assert upd["runs"] == []                        # singleShot(0)。メニューが閉じてから
    qapp.processEvents()
    assert upd["runs"] == [(repo_root(), HEAD_A)]
    assert upd["messages"] == [("info", "最新です（abcdef1）。", "")]
    assert upd["asks"] == []
    assert upd["win"]._update_action.isEnabled()


def test_update_refused_while_chat_is_replying(upd, monkeypatch):
    monkeypatch.setattr(upd["win"], "_chat_busy", lambda: True)
    upd["updater"].start()
    assert upd["runs"] == []
    assert upd["messages"] == [("info", "チャットの応答中は更新できません。応答が終わってから、もう一度押してください。", "")]


def test_updated_then_restart(upd):
    hr = _FakeHotReload()
    upd["win"]._hotreload = hr
    upd["result"] = UpdateResult("done", before=HEAD_A, after=HEAD_B)
    upd["updater"].start()
    assert len(upd["asks"]) == 1
    assert upd["asks"][0].startswith("1234567 に更新しました（動いているのは abcdef1）。")
    assert hr.calls == 1 and upd["messages"] == []


def test_updated_then_later(upd):
    hr = _FakeHotReload()
    upd["win"]._hotreload = hr
    upd["answer"] = False
    upd["result"] = UpdateResult("done", before=HEAD_A, after=HEAD_B)
    upd["updater"].start()
    assert hr.calls == 0
    assert upd["win"].statusBar().currentMessage().startswith("再起動するまでは、更新前と更新後のコードが混ざって")


def test_restart_refused_when_busy(upd):
    upd["win"]._hotreload = _FakeHotReload("reload-busy:a chat turn is streaming — retry when idle")
    upd["result"] = UpdateResult("done", before=HEAD_A, after=HEAD_B)
    upd["updater"].start()
    assert [m[1] for m in upd["messages"]] == [
        ("チャットの応答中か、ダイアログを開いているので、今は再起動できません。"
         "終わってから、もう一度「MyAnalysis を更新」を押すと再起動できます。")]


def test_restart_without_hotreload(upd):
    upd["result"] = UpdateResult("done", before=HEAD_A, after=HEAD_B)
    upd["updater"].start()
    assert [m[1] for m in upd["messages"]] == ["アプリを閉じて、起動し直してください。"]


def test_fetched_earlier_asks_again(upd):
    """前に取って [あとで] にした: 今回は進まなくても、起動時の HEAD と違えば再起動を訊く。"""
    upd["win"]._hotreload = _FakeHotReload()
    upd["result"] = UpdateResult("done", before=HEAD_B, after=HEAD_B)
    upd["updater"].start()
    assert upd["asks"][0].startswith("1234567 は取得済みで、まだ反映していません（動いているのは abcdef1）。")


def test_up_to_date_when_start_head_unknown(upd, qapp):
    from gui.about_dialog import set_startup_head
    set_startup_head(qapp, None)
    upd["result"] = UpdateResult("done", before=HEAD_B, after=HEAD_B)
    upd["updater"].start()
    assert upd["runs"][0][1] is None
    assert upd["asks"] == [] and upd["messages"][0][1] == "最新です（1234567）。"


def test_updated_when_start_head_unknown_names_the_previous_head(upd, qapp):
    from gui.about_dialog import set_startup_head
    set_startup_head(qapp, None)
    upd["result"] = UpdateResult("done", before=HEAD_A, after=HEAD_B)
    upd["updater"].start()
    assert upd["asks"][0].startswith("1234567 に更新しました（動いているのは abcdef1）。")


def test_pip_failure_does_not_offer_restart(upd):
    upd["win"]._hotreload = hr = _FakeHotReload()
    upd["result"] = UpdateResult("done", before=HEAD_A, after=HEAD_B, pip="failed",
                                 pip_output="ERROR: no network\n", pip_timed_out=True)
    upd["updater"].start()
    assert upd["asks"] == [] and hr.calls == 0
    (kind, text, details), = upd["messages"]
    assert kind == "warning" and text.startswith("コードは 1234567 に更新しましたが、依存パッケージを")
    assert text.endswith("時間内に終わらなかったので止めました。")
    assert details == "ERROR: no network"


def test_pip_ok_offers_restart(upd):
    upd["win"]._hotreload = _FakeHotReload()
    upd["result"] = UpdateResult("done", before=HEAD_A, after=HEAD_B, pip="ok")
    upd["updater"].start()
    assert len(upd["asks"]) == 1


@pytest.mark.parametrize(("result", "expected"), [
    (UpdateResult("no-git"), "git が見つかりません。"),
    (UpdateResult("not-repo"), "このフォルダは git clone で入れたものではない"),
    (UpdateResult("wrong-branch", branch="dev"), "今のブランチが dev なので更新しません（更新するのは main だけです）。"),
    (UpdateResult("wrong-branch", branch=None), "ブランチではなくコミットを直接チェックアウトしている"),
    (UpdateResult("failed", stage="fetch", output="fatal: repository not found"),
     f"GitHub（{UPDATE_URL}）から取得できませんでした。"),
    (UpdateResult("failed", stage="merge", output="fatal: Not possible to fast-forward"),
     "手元に GitHub に無いコミットか、更新で上書きされる変更がある"),
])
def test_failures_are_reported(upd, result, expected):
    upd["result"] = result
    upd["updater"].start()
    (kind, text, details), = upd["messages"]
    assert kind == "warning" and text.startswith(expected)
    assert details == result.output
    assert upd["asks"] == []


def test_fetch_timeout_left_running(upd):
    upd["result"] = UpdateResult("failed", stage="fetch", output="partial", timed_out=True,
                                 left_running=True)
    upd["updater"].start()
    text = upd["messages"][0][1]
    assert "時間内に終わらなかったので止めました。" in text
    assert text.endswith("git status でリポジトリの状態を確かめてください。")


def test_long_output_is_clipped(upd):
    lines = "\n".join(f"line{i}" for i in range(1, 201))
    upd["result"] = UpdateResult("failed", stage="merge", output=lines)
    upd["updater"].start()
    details = upd["messages"][0][2].splitlines()
    assert len(details) == 40 and details[-1] == "line200" and "…（ほか 161 行）…" in details


def test_status_bar_and_action_while_running(upd):
    """走っている間はメニューの項目を無効にし、ステータスバーに段階を出す。2 本目は走らせない。"""
    win, seen = upd["win"], []
    pending = []
    win._self_updater._spawn = pending.append

    def during(progress):
        seen.append(win.statusBar().currentMessage())
        progress("pip")
        seen.append(win.statusBar().currentMessage())
    upd["during"] = during
    upd["updater"].start()
    assert not win._update_action.isEnabled() and upd["updater"].is_running()
    assert win.statusBar().currentMessage() == "GitHub から更新を取得しています…"
    upd["updater"].start()                          # 2 回目は何もしない
    assert len(pending) == 1
    pending[0]()
    assert seen == ["GitHub から更新を取得しています…", "依存パッケージを入れています（pip install）…"]
    assert win._update_action.isEnabled() and not upd["updater"].is_running()
    assert win.statusBar().currentMessage() == ""
    assert len(upd["runs"]) == 1


def test_result_comes_back_from_a_thread(upd, qapp, monkeypatch):
    """本物のスレッドから emit した結果が GUI スレッドに届く。"""
    from gui import updater as U
    threads = []

    def spawn(fn):
        t = threading.Thread(target=fn, daemon=True)
        threads.append(t)
        t.start()
    upd["updater"]._spawn = spawn
    main = threading.get_ident()
    where = []
    upd["during"] = lambda progress: where.append(threading.get_ident())
    reported = []
    monkeypatch.setattr(U.SelfUpdater, "_report",
                        lambda self, r: reported.append((threading.get_ident(), r.status)))
    try:
        upd["updater"].start()
        deadline = time.monotonic() + 5
        while upd["updater"].is_running() and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
    finally:
        for t in threads:
            t.join(5)
    assert where and where[0] != main
    assert reported == [(main, "done")]


def test_emit_after_the_window_is_gone(qapp, monkeypatch):
    """ウィンドウ（と updater）が先に消えても、スレッドの側は投げない。"""
    import shiboken6
    from PySide6.QtWidgets import QMainWindow

    from gui import updater as U
    monkeypatch.setattr(U.self_update, "run_update",
                        lambda repo, running=None, progress=None: UpdateResult("done"))
    host = QMainWindow()
    updater = U.SelfUpdater(host)
    shiboken6.delete(host)
    updater._work("repo", None)                     # RuntimeError を握る


def test_dialog_helpers(qapp, ja, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from gui import updater as U
    seen, reply = [], {"value": QMessageBox.StandardButton.Yes}

    def fake_exec(box):
        seen.append({
            "title": box.windowTitle(), "text": box.text(), "details": box.detailedText(),
            "buttons": sorted(b.text() for b in box.buttons() if b.text()),
            "default": box.defaultButton().text() if box.defaultButton() else None,
            "escape": box.escapeButton().text() if box.escapeButton() else None,
        })
        return reply["value"]
    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    assert U.ask_restart(None, "本文") is True
    reply["value"] = QMessageBox.StandardButton.No
    assert U.ask_restart(None, "本文") is False
    assert seen[0]["title"] == "MyAnalysis を更新"
    assert seen[0]["buttons"] == sorted(["再起動", "あとで"])
    assert (seen[0]["default"], seen[0]["escape"]) == ("再起動", "あとで")
    U.show_message(None, "warning", "失敗", "fatal: x")
    assert (seen[-1]["text"], seen[-1]["details"]) == ("失敗", "fatal: x")
