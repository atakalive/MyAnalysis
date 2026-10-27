"""ヘルプ → MyAnalysis を更新 の本体（Qt 非依存。GUI は gui/updater.py）。

手動のみ: 利用者がメニューを押したときに ``run_update`` が 1 回だけ走る。起動時・定期の確認、
裏での fetch、「更新があります」の通知はしない。

手順（never raise。結果は ``UpdateResult``。文章は返さず、表示の文は GUI が ``tr()`` で作る）:

1. git が無い → ``no-git``。``.git`` から HEAD を読めない → ``not-repo``。今のブランチが
   ``UPDATE_BRANCH`` でない（detached を含む） → ``wrong-branch``。
2. GitHub（``UPDATE_URL``）の main を一時 ref ``_TEMP_REF`` に取る（``FETCH_HEAD`` は使わない。
   VS Code の自動 fetch などが間に ``FETCH_HEAD`` を origin で書き換えると、別の取得元へ進めてしまう）。
   認証は聞かない（公開リポジトリなので要らない。``credential.helper`` / ``core.askPass`` を空にし、
   env は ``common.git.git_env``）。失敗 → ``failed`` / ``fetch``。
3. ``git merge --ff-only <一時 ref>``。手元のコミットや、上書きされる手元の変更があれば git が止めて
   何も変わらない → ``failed`` / ``merge``。``pull`` を使わないのは、利用者の ``pull.rebase`` などの
   設定に左右されないため。一時 ref は成否に関わらず消す。
4. 動いているコードの HEAD（``running``。起動時の HEAD、分からなければ手順の前の HEAD）と更新後の
   HEAD の間で ``requirements.txt`` が変わっていれば ``pip install -r requirements.txt``。起動時の
   HEAD を基準にするので、pip が失敗した後や再起動を後にした後にもう一度押すと、pip をやり直す。
   版を固定していないので、入っているパッケージは上げず、足りないものだけが入る。

反映（再起動）は GUI が既存の Tier 4（``HotReloadController.restart``）で行う。
"""
from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from common import git
from common.proc import CommandResult, run_capture

UPDATE_URL = "https://github.com/atakalive/MyAnalysis.git"
UPDATE_BRANCH = "main"
_TEMP_REF = "refs/myanalysis/update"

FETCH_TIMEOUT_S = 300
MERGE_TIMEOUT_S = 60
LOCAL_TIMEOUT_S = 30      # update-ref・diff（手元だけで終わるもの）
PIP_TIMEOUT_S = 600


@dataclass(frozen=True)
class UpdateResult:
    status: str                     # no-git / not-repo / wrong-branch / failed / done
    stage: str | None = None        # failed のとき: fetch / merge
    branch: str | None = None       # 今のブランチ（detached は None）
    before: str | None = None       # 手順の前の HEAD
    after: str | None = None        # 手順の後の HEAD（done のとき）
    output: str = ""                # 失敗した git の出力
    timed_out: bool = False
    left_running: bool = False
    pip: str | None = None          # None（実行しなかった）/ ok / failed
    pip_output: str = ""
    pip_timed_out: bool = False

    @property
    def advanced(self) -> bool:
        """この手順で HEAD が進んだか。"""
        return self.status == "done" and self.before != self.after


def run_update(repo, *, running: str | None = None, url: str = UPDATE_URL,
               branch: str = UPDATE_BRANCH,
               progress: Callable[[str], None] | None = None) -> UpdateResult:
    """repo を url の branch へ fast-forward する（手順はモジュールの説明）。never raise。

    running は動いているコードの HEAD（pip の要否の基準）。progress は pip に入るときに
    ``"pip"`` で呼ぶ（呼ぶのは実行しているスレッド）。url / branch はテストが手元の bare を渡す。"""
    try:
        return _run_update(Path(repo), running, url, branch, progress)
    except Exception as e:  # noqa: BLE001 — 何があっても結果で返す
        return UpdateResult("failed", stage="fetch", output=f"{type(e).__name__}: {e}")


def _run_update(repo: Path, running: str | None, url: str, branch: str,
                progress: Callable[[str], None] | None) -> UpdateResult:
    if git.find_git() is None:
        return UpdateResult("no-git")
    before = git.read_head(repo)
    if before is None:
        return UpdateResult("not-repo")
    current = git.current_branch(repo)
    if current != branch:
        return UpdateResult("wrong-branch", branch=current, before=before)

    try:
        res = git.run_git(repo, [
            "-c", "credential.helper=", "-c", "core.askPass=",
            "fetch", "--no-tags", url, f"+refs/heads/{branch}:{_TEMP_REF}",
        ], FETCH_TIMEOUT_S)
        if not res.ok:
            return _failed("fetch", current, before, res)
        res = git.run_git(repo, ["merge", "--ff-only", _TEMP_REF], MERGE_TIMEOUT_S)
        if not res.ok:
            return _failed("merge", current, before, res)
    finally:
        git.run_git(repo, ["update-ref", "-d", _TEMP_REF], LOCAL_TIMEOUT_S)

    after = git.read_head(repo) or before
    base = running or before
    if base == after or not _requirements_changed(repo, base, after):
        return UpdateResult("done", branch=current, before=before, after=after)
    if progress is not None:
        try:
            progress("pip")
        except Exception:  # noqa: BLE001, S110 — 表示のための知らせ。失敗しても pip は続ける
            pass
    pip = _run_pip(repo)
    return UpdateResult("done", branch=current, before=before, after=after,
                        pip="ok" if pip.ok else "failed", pip_output=pip.output,
                        pip_timed_out=pip.timed_out)


def _failed(stage: str, branch: str | None, before: str, res: CommandResult) -> UpdateResult:
    return UpdateResult("failed", stage=stage, branch=branch, before=before, output=res.output,
                        timed_out=res.timed_out, left_running=res.left_running)


def _requirements_changed(repo: Path, base: str, after: str) -> bool:
    """base と after の間で requirements.txt が変わったか。git diff が 0 以外（差分あり・base が
    手元に無いなど）は変わったとみなす（pip は入っているものを上げないので、余計に走っても害が無い）。"""
    res = git.run_git(repo, ["diff", "--quiet", base, after, "--", "requirements.txt"],
                      LOCAL_TIMEOUT_S)
    return res.returncode != 0


def python_for_pip() -> str:
    """pip を実行する Python。pythonw（run.bat の窓なし起動）なら隣の python.exe を使う
    （窓は no_window_kwargs で出さない）。"""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.is_file():
            return str(console)
    return str(exe)


def _run_pip(repo: Path) -> CommandResult:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PIP_NO_INPUT"] = "1"
    return run_capture(
        [python_for_pip(), "-m", "pip", "install", "-r", str(repo / "requirements.txt"),
         "--disable-pip-version-check", "--no-input"],
        cwd=repo, env=env, timeout=PIP_TIMEOUT_S,
    )
