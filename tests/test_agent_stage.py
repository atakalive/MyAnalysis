"""エージェントの stage（Issue #118）。

長い本文はシェルコマンドに埋め込まず、Write ツールでチャット専用の stage
（`agent_stage_dir()/<token>`、環境変数 MYANALYSIS_AGENT_STAGE）に置いて `save_text` /
`save_code` の `src=` にファイル名を渡す。ここでは stage の場所・割り当て・検証と、各バックエンドが
チャットごとに別の stage を渡すことを見る。`src=` の細かい挙動は tests/test_explore.py、
案内文は tests/test_guard_write.py と tests/test_backend_prompts.py。
"""
from __future__ import annotations

import logging
import os

import pytest

import common.explore as explore
import common.paths as cp
from common.paths import AGENT_STAGE_ENV
from common.paths import agent_stage_dir   # conftest が差し替える前の本物


def _symlink_or_skip(link, target):
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as e:   # Windows の権限なし等
        pytest.skip(f"symlink unavailable: {e}")


def _resolve_fails_under(monkeypatch, prefix):
    """prefix 以下のパスで Path.resolve() を OSError にする（WinFsp の WinError 1005 の再現）。"""
    from pathlib import Path
    real = Path.resolve

    def resolve(self, *a, **kw):
        if str(self).startswith(str(prefix)):
            raise OSError(1005, "unrecognized file system")
        return real(self, *a, **kw)
    monkeypatch.setattr(Path, "resolve", resolve)


# ---- root ----

def test_agent_stage_dir_is_repo_data_and_not_created(monkeypatch, tmp_path):
    monkeypatch.setattr(cp, "repo_root", lambda: tmp_path)
    assert agent_stage_dir() == tmp_path / "data" / "agent_stage"
    assert not (tmp_path / "data").exists()          # パスを返すだけで mkdir しない


def test_stage_root_is_usable_when_plain():
    assert not cp.agent_stage_dir().exists()         # 未作成でも使える
    assert cp.agent_stage_usable()


def test_stage_root_through_symlink_is_not_usable(monkeypatch, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root = tmp_path / "linked_root"
    _symlink_or_skip(root, elsewhere)
    monkeypatch.setattr(cp, "agent_stage_dir", lambda: root)
    assert not cp.agent_stage_usable()
    assert cp.prepare_agent_stage(None) is None      # リンク先に何も作らない
    assert cp.prepare_agent_stage("chat") is None
    assert list(elsewhere.iterdir()) == []


def test_unresolvable_root_is_not_usable(monkeypatch):
    """root を解決できなければ、リンクの有無を判定できないので使わない（fail-closed）。"""
    _resolve_fails_under(monkeypatch, cp.agent_stage_dir())
    assert not cp.agent_stage_usable()
    assert cp.prepare_agent_stage(None) is None


def test_linked_root_with_failing_resolve_is_not_usable(monkeypatch, tmp_path):
    """root がリンクで、リンク先で resolve が失敗しても（字句に倒さず）使わない。"""
    mount = tmp_path / "mount"
    mount.mkdir()
    root = tmp_path / "linked_root"
    _symlink_or_skip(root, mount)
    monkeypatch.setattr(cp, "agent_stage_dir", lambda: root)
    _resolve_fails_under(monkeypatch, root)
    assert cp.safe_resolve(root) == root             # safe_resolve は字句に倒れる（だから使わない）
    assert not cp.agent_stage_usable()
    assert cp.prepare_agent_stage(None) is None
    assert list(mount.iterdir()) == []


def test_resolve_runtime_error_is_not_usable(monkeypatch):
    """Python 3.12 以前の resolve() は symlink の循環で RuntimeError を出す。漏らさず使わない側に倒す。"""
    from pathlib import Path
    real = Path.resolve
    root = cp.agent_stage_dir()

    def resolve(self, *a, **kw):
        if str(self).startswith(str(root)):
            raise RuntimeError(f"Symlink loop from {str(self)!r}")
        return real(self, *a, **kw)
    monkeypatch.setattr(Path, "resolve", resolve)
    assert cp._resolve_or_none(root) is None
    assert not cp.agent_stage_usable()
    assert cp.prepare_agent_stage(None) is None


@pytest.mark.parametrize("where", ["root", "parent"])
def test_symlink_loop_root_is_not_usable(monkeypatch, tmp_path, where):
    """root か その親（data/）が循環する symlink なら使わない（3.12 は RuntimeError、3.13 以降は
    循環の手前のパスを返すので、is_symlink で見る）。例外は外へ出さない。"""
    if where == "root":
        root = tmp_path / "loop_root"
        _symlink_or_skip(root, root)
    else:
        data = tmp_path / "loop_data"
        _symlink_or_skip(data, data)
        root = data / "agent_stage"
    monkeypatch.setattr(cp, "agent_stage_dir", lambda: root)
    assert not cp.agent_stage_usable()
    assert cp.prepare_agent_stage(None) is None
    assert cp.prepare_agent_stage("chat") is None
    monkeypatch.setenv(AGENT_STAGE_ENV, str(root / "chat"))
    assert cp.current_agent_stage() is None


def test_fragile_root_is_not_usable(monkeypatch):
    """root が fs_kind で fragile（MYANALYSIS_FORCE_FRAGILE を含む）なら stage を使わない。

    stage のファイルはエージェントの Write ツールが検証なしで書くので、同期マウント上に
    置かせない（0 バイト以外の壊れ方は src= で見抜けない）。
    """
    stage = cp.prepare_agent_stage(None)
    assert stage is not None and cp.agent_stage_usable()
    monkeypatch.setenv("MYANALYSIS_FORCE_FRAGILE", "1")
    assert not cp.agent_stage_usable()
    assert cp.prepare_agent_stage(None) is None
    assert cp.prepare_agent_stage(stage.name) is None
    monkeypatch.setenv(AGENT_STAGE_ENV, str(stage))
    assert cp.current_agent_stage() is None


# ---- prepare_agent_stage ----

def test_prepare_agent_stage_allocates_and_reuses():
    stage = cp.prepare_agent_stage(None)
    assert stage.parent == cp.agent_stage_dir() and stage.is_dir()
    assert len(stage.name) == 16                     # secrets.token_hex(8)
    assert cp.prepare_agent_stage(stage.name) == stage   # 同じ token は同じ stage
    stage.rmdir()
    assert cp.prepare_agent_stage(stage.name) == stage   # 消されていれば作り直す
    assert stage.is_dir()


def test_prepare_agent_stage_skips_existing_names(monkeypatch):
    """新しい stage は排他的に作る。既にある名前（別のチャット・前回の残り）は使わない。"""
    root = cp.agent_stage_dir()
    taken = root / ("a" * 16)
    taken.mkdir(parents=True)
    (taken / "left.py").write_text("x", encoding="utf-8")
    names = iter(["a" * 16, "b" * 16])
    monkeypatch.setattr(cp.secrets, "token_hex", lambda n: next(names))
    assert cp.prepare_agent_stage(None) == root / ("b" * 16)
    assert (taken / "left.py").read_text(encoding="utf-8") == "x"


def test_prepare_agent_stage_gives_up_after_collisions(monkeypatch, caplog):
    (cp.agent_stage_dir() / ("a" * 16)).mkdir(parents=True)
    monkeypatch.setattr(cp.secrets, "token_hex", lambda n: "a" * 16)
    with caplog.at_level(logging.WARNING, logger="common.paths"):
        assert cp.prepare_agent_stage(None) is None
    assert "allocate" in caplog.text


def test_prepare_agent_stage_never_raises(monkeypatch, tmp_path, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")        # 親がファイル → mkdir は OSError
    monkeypatch.setattr(cp, "agent_stage_dir", lambda: blocker / "agent_stage")
    with caplog.at_level(logging.WARNING, logger="common.paths"):
        assert cp.prepare_agent_stage(None) is None
        assert cp.prepare_agent_stage("abc") is None
    assert "agent stage" in caplog.text


# ---- current_agent_stage ----

def test_current_agent_stage_reads_env(monkeypatch):
    stage = cp.prepare_agent_stage(None)
    assert cp.current_agent_stage() is None          # 環境変数が無い
    monkeypatch.setenv(AGENT_STAGE_ENV, str(stage))
    assert cp.current_agent_stage() == stage


@pytest.mark.parametrize("value", [
    "relative/chat",                                 # 相対パス
    "{root}/missing",                                # 存在しない
    "{root}",                                        # root そのもの（直下の stage でない）
    "{root}/chat/nested",                            # 直下より深い
    "{tmp}/outside",                                 # root の外
])
def test_current_agent_stage_rejects_bad_env(monkeypatch, tmp_path, value):
    root = cp.agent_stage_dir()
    (root / "chat" / "nested").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    monkeypatch.setenv(AGENT_STAGE_ENV, value.format(root=root, tmp=tmp_path))
    assert cp.current_agent_stage() is None


def test_current_agent_stage_fails_closed_when_unresolvable(monkeypatch):
    stage = cp.prepare_agent_stage(None)
    monkeypatch.setenv(AGENT_STAGE_ENV, str(stage))
    _resolve_fails_under(monkeypatch, stage)
    assert cp.current_agent_stage() is None


@pytest.mark.parametrize("token", [None, "chat"])
def test_prepared_stage_is_accepted_by_current_agent_stage(monkeypatch, token):
    """prepare_agent_stage が返す stage は current_agent_stage も受け入れる（案内と src= が食い違わない）。"""
    stage = cp.prepare_agent_stage(token)
    monkeypatch.setenv(AGENT_STAGE_ENV, str(stage))
    assert cp.current_agent_stage() == stage


def test_current_agent_stage_rejects_symlinked_stage(monkeypatch, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    cp.agent_stage_dir().mkdir()
    link = cp.agent_stage_dir() / "chat"
    _symlink_or_skip(link, elsewhere)
    monkeypatch.setenv(AGENT_STAGE_ENV, str(link))
    assert cp.current_agent_stage() is None


# ---- バックエンドの _build_env ----

def _claude():
    from llm_backend.claude_code import ClaudeCodeBackend
    return ClaudeCodeBackend({"bin": "/usr/bin/claude"})


def _pi():
    from llm_backend.pi import PiCodingAgentBackend
    return PiCodingAgentBackend({})


def _codex():
    from llm_backend.codex import CodexBackend
    return CodexBackend({})


@pytest.mark.parametrize("make", [_claude, _pi, _codex], ids=["claude", "pi", "codex"])
def test_build_env_passes_own_stage(make):
    b = make()
    first = b._build_env()[AGENT_STAGE_ENV]
    stage = cp.agent_stage_dir() / os.path.basename(first)
    assert first == str(stage) and stage.is_dir()
    assert len(stage.name) == 16                     # secrets.token_hex(8)
    assert b._build_env()[AGENT_STAGE_ENV] == first  # 同じチャットでは変わらない
    stage.rmdir()
    assert b._build_env()[AGENT_STAGE_ENV] == first  # 消されても同じ場所に作り直す
    assert stage.is_dir()
    assert make()._build_env()[AGENT_STAGE_ENV] != first   # 別のチャットは別の stage


@pytest.mark.parametrize("make", [_claude, _pi, _codex], ids=["claude", "pi", "codex"])
def test_build_env_drops_stage_when_unusable(make, monkeypatch):
    monkeypatch.setenv(AGENT_STAGE_ENV, "inherited")
    monkeypatch.setattr(cp, "agent_stage_usable", lambda: False)
    assert AGENT_STAGE_ENV not in make()._build_env()   # 継承した値も消す


@pytest.mark.parametrize("make", [_claude, _pi, _codex], ids=["claude", "pi", "codex"])
def test_build_env_survives_symlink_loop_root(make, monkeypatch, tmp_path):
    """root が循環する symlink でも _build_env は例外を出さず、stage を渡さない。"""
    root = tmp_path / "loop_root"
    _symlink_or_skip(root, root)
    monkeypatch.setattr(cp, "agent_stage_dir", lambda: root)
    monkeypatch.setenv(AGENT_STAGE_ENV, "inherited")
    assert AGENT_STAGE_ENV not in make()._build_env()


def test_reused_stage_replaced_by_link_is_dropped(monkeypatch, tmp_path):
    """既存の stage がリンクに差し替わっていたら、再利用せず環境変数も渡さない。"""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    b = _claude()
    stage = cp.agent_stage_dir() / os.path.basename(b._build_env()[AGENT_STAGE_ENV])
    stage.rmdir()
    _symlink_or_skip(stage, elsewhere)
    assert cp.prepare_agent_stage(stage.name) is None
    monkeypatch.setenv(AGENT_STAGE_ENV, "inherited")
    assert AGENT_STAGE_ENV not in b._build_env()
    assert list(elsewhere.iterdir()) == []


def test_idle_chat_stage_survives_other_chats():
    """stage は自動では消さない。長く空のままの別チャットの stage も、他のチャットのターンで消えない。"""
    a = _claude()
    stage_a = a._build_env()[AGENT_STAGE_ENV]
    old = 0.0                                        # 1970 年 = いくら古くても
    os.utime(stage_a, (old, old))
    for other in (_codex(), _pi()):                  # 別のチャットの 1 ターン目と 2 ターン目
        other._build_env()
        other._build_env()
    assert os.path.isdir(stage_a)
    assert a._build_env()[AGENT_STAGE_ENV] == stage_a


def test_two_chats_same_file_name_do_not_mix(monkeypatch, tmp_path):
    """2 つのチャットが同じファイル名で書いても、互いの本文を保存・削除しない。"""
    ds = tmp_path / "ds"
    ds.mkdir()
    monkeypatch.setattr("config.get_dataset_dir", lambda name: ds)
    env_a = _claude()._build_env()[AGENT_STAGE_ENV]
    env_b = _codex()._build_env()[AGENT_STAGE_ENV]
    a = os.path.join(env_a, "snippet.py")
    b = os.path.join(env_b, "snippet.py")
    with open(a, "w", encoding="utf-8") as f:       # A が書く
        f.write("A = 1\n")
    with open(b, "w", encoding="utf-8") as f:       # B が同じ名前で書く
        f.write("B = 2\n")
    monkeypatch.setenv(AGENT_STAGE_ENV, env_a)       # A が保存する
    pa = explore.save_code("ds", "from_a", src="snippet.py")
    assert pa.read_text(encoding="utf-8") == "A = 1\n"
    assert not os.path.exists(a) and os.path.exists(b)   # B の入力は消さない
    monkeypatch.setenv(AGENT_STAGE_ENV, env_b)       # B が保存する
    pb = explore.save_code("ds", "from_b", src="snippet.py")
    assert pb.read_text(encoding="utf-8") == "B = 2\n"
    assert not os.path.exists(b)
