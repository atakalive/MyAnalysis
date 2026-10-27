"""common.self_update.run_update（ヘルプ → MyAnalysis を更新 の本体）。

本物の git で、手元の bare リポジトリを「GitHub」に見立てて url に渡す（ネットワークは使わない）。
pip の実行は差し替える。
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from common import self_update as SU
from common.git import read_head
from common.proc import CommandResult

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git が無い")


def run(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """外から来た GIT_* を外し、git の設定を空のファイルだけにする（tests/test_git.py と同じ）。
    tmp_path を返す。"""
    config = tmp_path / "gitconfig"
    config.write_text("")
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.delenv("LANGUAGE", raising=False)
    return tmp_path


def out(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def commit(repo, name, text, message):
    (repo / name).write_text(text, encoding="utf-8")
    run(repo, "add", name)
    run(repo, "commit", "-q", "-m", message)
    return read_head(repo)


@pytest.fixture
def hub(isolated):
    """GitHub 役の bare（main）と、そこへ push する作者の clone "dev"。(hub, dev, 最初のコミット)。"""
    run(isolated, "init", "-q", "--bare", "hub.git")
    hub = isolated / "hub.git"
    run(hub, "symbolic-ref", "HEAD", "refs/heads/main")
    run(isolated, "init", "-q", "dev")
    dev = isolated / "dev"
    run(dev, "symbolic-ref", "HEAD", "refs/heads/main")
    (dev / "requirements.txt").write_text("PySide6\n", encoding="utf-8")
    run(dev, "add", "requirements.txt")
    first = commit(dev, "f.txt", "1\n", "first")
    run(dev, "push", "-q", str(hub), "main")
    return hub, dev, first


@pytest.fixture
def user(hub, isolated):
    """利用者の clone（main）。"""
    run(isolated, "clone", "-q", str(hub[0]), "user")
    return isolated / "user"


@pytest.fixture
def pip_calls(monkeypatch):
    calls: list = []
    result = {"value": CommandResult(True, "Successfully installed x", 0)}

    def fake(repo):
        calls.append(repo)
        return result["value"]
    monkeypatch.setattr(SU, "_run_pip", fake)
    return calls, result


def publish(hub, dev, name, text, message):
    head = commit(dev, name, text, message)
    run(dev, "push", "-q", str(hub), "main")
    return head


def temp_refs(repo) -> str:
    return out(repo, "for-each-ref", "refs/myanalysis")


def test_fast_forward(hub, user, pip_calls):
    hub_dir, dev, first = hub
    second = publish(hub_dir, dev, "f.txt", "2\n", "second")
    progress = []
    r = SU.run_update(user, url=str(hub_dir), progress=progress.append)
    assert (r.status, r.before, r.after, r.branch, r.pip) == ("done", first, second, "main", None)
    assert r.advanced
    assert read_head(user) == second and (user / "f.txt").read_text() == "2\n"
    assert temp_refs(user) == ""            # 一時 ref は残さない
    assert pip_calls[0] == [] and progress == []

    again = SU.run_update(user, url=str(hub_dir))
    assert (again.status, again.before, again.after) == ("done", second, second)
    assert not again.advanced


def test_untracked_files_are_kept(hub, user, pip_calls):
    hub_dir, dev, _ = hub
    (user / "datasets.local.json").write_text("{}", encoding="utf-8")   # 利用者のファイル
    second = publish(hub_dir, dev, "f.txt", "2\n", "second")
    r = SU.run_update(user, url=str(hub_dir))
    assert r.status == "done" and read_head(user) == second
    assert (user / "datasets.local.json").read_text() == "{}"


def test_local_commit_is_not_overwritten(hub, user, pip_calls):
    hub_dir, dev, _ = hub
    mine = commit(user, "mine.txt", "x\n", "local")
    publish(hub_dir, dev, "f.txt", "2\n", "second")
    r = SU.run_update(user, url=str(hub_dir))
    assert (r.status, r.stage, r.before) == ("failed", "merge", mine)
    assert r.output.strip()
    assert read_head(user) == mine and temp_refs(user) == ""


def test_local_change_that_the_update_would_overwrite(hub, user, pip_calls):
    hub_dir, dev, first = hub
    (user / "f.txt").write_text("edited\n", encoding="utf-8")
    publish(hub_dir, dev, "f.txt", "2\n", "second")
    r = SU.run_update(user, url=str(hub_dir))
    assert (r.status, r.stage) == ("failed", "merge")
    assert read_head(user) == first and (user / "f.txt").read_text() == "edited\n"


def test_ahead_of_github_is_up_to_date(hub, user, pip_calls):
    """開発 PC: 手元が GitHub より先（GitHub の main は祖先）なら何も変えない。"""
    hub_dir, _, _ = hub
    mine = commit(user, "mine.txt", "x\n", "local")
    r = SU.run_update(user, url=str(hub_dir))
    assert (r.status, r.before, r.after) == ("done", mine, mine) and not r.advanced


def test_fetch_failure_changes_nothing(hub, user, pip_calls, isolated):
    _, _, first = hub
    r = SU.run_update(user, url=str(isolated / "missing.git"))
    assert (r.status, r.stage, r.before) == ("failed", "fetch", first)
    assert r.output.strip()
    assert read_head(user) == first and temp_refs(user) == ""


def test_wrong_branch_and_detached(hub, user, pip_calls):
    hub_dir, dev, first = hub
    publish(hub_dir, dev, "f.txt", "2\n", "second")
    run(user, "switch", "-q", "-c", "other")
    r = SU.run_update(user, url=str(hub_dir))
    assert (r.status, r.branch) == ("wrong-branch", "other") and read_head(user) == first
    run(user, "switch", "-q", "--detach", "main")
    r = SU.run_update(user, url=str(hub_dir))
    assert (r.status, r.branch) == ("wrong-branch", None) and read_head(user) == first


def test_not_repo_and_no_git(tmp_path, monkeypatch):
    assert SU.run_update(tmp_path, url="unused").status == "not-repo"
    monkeypatch.setattr(SU.git, "find_git", lambda: None)
    assert SU.run_update(tmp_path, url="unused").status == "no-git"


def test_pip_only_when_requirements_changed_since_running(hub, user, pip_calls):
    hub_dir, dev, first = hub
    calls, result = pip_calls
    second = publish(hub_dir, dev, "requirements.txt", "PySide6\nnumpy\n", "add numpy")
    progress = []
    r = SU.run_update(user, url=str(hub_dir), progress=progress.append)
    assert (r.after, r.pip, r.pip_output) == (second, "ok", "Successfully installed x")
    assert calls == [user] and progress == ["pip"]

    # 動いているのが更新前（first）のままもう一度押すと、pip をやり直す（前回の失敗・[あとで] の後）
    result["value"] = CommandResult(False, "ERROR: no network", 1)
    r = SU.run_update(user, running=first, url=str(hub_dir))
    assert (r.before, r.after, r.pip, r.pip_output) == (second, second, "failed", "ERROR: no network")
    assert len(calls) == 2
    # 動いているのが今の HEAD なら呼ばない
    r = SU.run_update(user, running=second, url=str(hub_dir))
    assert r.pip is None and len(calls) == 2
    # requirements.txt 以外の変更では呼ばない
    third = publish(hub_dir, dev, "f.txt", "3\n", "third")
    r = SU.run_update(user, running=second, url=str(hub_dir))
    assert (r.after, r.pip) == (third, None) and len(calls) == 2


def test_pip_when_running_commit_is_unknown(hub, user, pip_calls):
    """動いているコミットが手元に無い（git diff が失敗する）なら、変わったとみなして pip を呼ぶ。"""
    hub_dir, dev, _ = hub
    calls, _ = pip_calls
    publish(hub_dir, dev, "f.txt", "2\n", "second")
    r = SU.run_update(user, running="f" * 40, url=str(hub_dir))
    assert r.pip == "ok" and calls == [user]


def test_never_raises(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(SU.git, "find_git", boom)
    r = SU.run_update(tmp_path, url="unused")
    assert r.status == "failed" and "boom" in r.output


def test_python_for_pip_prefers_console_python(tmp_path, monkeypatch):
    (tmp_path / "pythonw.exe").write_bytes(b"")
    (tmp_path / "python.exe").write_bytes(b"")
    monkeypatch.setattr(SU.sys, "executable", str(tmp_path / "pythonw.exe"))
    assert SU.python_for_pip() == str(tmp_path / "python.exe")
    (tmp_path / "python.exe").unlink()
    assert SU.python_for_pip() == str(tmp_path / "pythonw.exe")
    monkeypatch.setattr(SU.sys, "executable", str(tmp_path / "python3"))
    assert SU.python_for_pip() == str(tmp_path / "python3")
