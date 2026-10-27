"""Claude サブ（Issue #120）: トークンの置き場・認証の環境・設定の検査・factory・エンジンの解決。

トークンの置き場と claude の設定の検査は tests/conftest.py の autouse が tmp に向けている（実物に触れない）。
本物の関数を試すテストは、collection 時に取った本物を tmp の home・リポジトリで呼ぶ。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import llm_backend
from llm_backend import claude_code, claude_sub, engines
from llm_backend.base import Message
from llm_backend.claude_code import ClaudeCodeBackend

# conftest の autouse が差し替える前の本物（collection 時に取る）。
_REAL_TOKEN_PATH = claude_sub.token_path
_REAL_CONFIG_DIRS = claude_sub._claude_config_dirs
_REAL_ANCESTORS = claude_sub._ancestor_dirs
_REAL_MOUNTS_READABLE = claude_sub._mounts_readable

_TOKEN = "sk-ant-oat01-" + "A" * 80


class _Spawned(Exception):
    """Popen の偽物が投げる（env を取ったらそこで止める）。"""


@pytest.fixture
def spawn(monkeypatch, tmp_path):
    """Popen に渡った env を集め、起動はしない。"""
    envs: list[dict] = []

    def _popen(cmd, **kw):
        envs.append(kw.get("env"))
        raise _Spawned()

    monkeypatch.setattr(claude_code.subprocess, "Popen", _popen)
    monkeypatch.setattr(ClaudeCodeBackend, "_discover_binary", lambda self: "/opt/claude")
    monkeypatch.setattr(ClaudeCodeBackend, "_agent_home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(claude_code, "_replay_supported", lambda b: False)
    return envs


def _sub() -> ClaudeCodeBackend:
    return ClaudeCodeBackend({}, auth_env=claude_sub.auth_env)


def _run_stream(backend) -> None:
    for _ in backend.stream([Message(role="user", content="hi")]):
        pass


# ---- 置き場 ----


def test_roundtrip_strips_whitespace():
    claude_sub.save_token("  sk-ant-oat01-\n" + "A" * 80 + " \n")
    assert claude_sub.read_token() == _TOKEN
    assert claude_sub.token_state() == "ok"
    assert claude_sub.auth_env() == {claude_sub.TOKEN_ENV: _TOKEN}


@pytest.mark.parametrize("text,code", [
    ("", "empty"),
    ("   \n", "empty"),
    ("sk-ant-api03-" + "A" * 80, "prefix"),
    ("sk-ant-oat01-ＡＢＣ", "chars"),
    ("sk-ant-oat01-" + "A" * 5000, "chars"),
])
def test_save_rejects_bad_tokens_without_echoing_them(text, code):
    with pytest.raises(claude_sub.AuthError) as ei:
        claude_sub.save_token(text)
    assert ei.value.code == code
    stripped = claude_sub.clean_token(text)
    assert not stripped or stripped not in str(ei.value)
    assert not claude_sub.token_path().exists()


def test_read_errors_and_token_state():
    with pytest.raises(claude_sub.AuthError) as ei:
        claude_sub.read_token()
    assert ei.value.code == "not_set"
    assert claude_sub.token_state() == "missing"

    path = claude_sub.token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(claude_sub.AuthError) as ei:
        claude_sub.read_token()
    assert ei.value.code == "too_large"
    assert claude_sub.token_state() == "bad"

    path.write_bytes(b"sk-ant-api03-" + b"B" * 40)
    with pytest.raises(claude_sub.AuthError) as ei:
        claude_sub.read_token()
    assert ei.value.code == "corrupt"
    assert "sk-ant-api03" not in str(ei.value)
    assert claude_sub.token_state() == "bad"


def test_undecryptable(monkeypatch):
    claude_sub.save_token(_TOKEN)

    def _boom(blob):
        raise OSError(13, "bad key")

    monkeypatch.setattr(claude_sub, "_unprotect", _boom)
    with pytest.raises(claude_sub.AuthError) as ei:
        claude_sub.read_token()
    assert ei.value.code == "undecryptable"
    assert _TOKEN not in str(ei.value)
    assert claude_sub.token_state() == "bad"


def test_delete_token():
    claude_sub.save_token(_TOKEN)
    claude_sub.delete_token()
    assert claude_sub.token_state() == "missing"
    claude_sub.delete_token()               # 無くても投げない


def test_real_token_path_is_under_home_and_outside_repo(monkeypatch, tmp_path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(claude_sub, "repo_root", lambda: repo)
    monkeypatch.setattr(claude_sub, "token_path", _REAL_TOKEN_PATH)
    assert claude_sub.token_path() == home / ".myanalysis" / "secrets" / "claude_sub.bin"
    claude_sub.save_token(_TOKEN)
    assert claude_sub.token_path().is_file()
    assert not any(repo.iterdir())


def test_save_refuses_path_inside_repo(monkeypatch, tmp_path):
    # 本物のリポジトリには書かない（判定が壊れていても tmp の中で済む）。
    repo = tmp_path / "repo"
    repo.mkdir()
    inside = repo / "data" / "claude_sub.bin"
    monkeypatch.setattr(claude_sub, "repo_root", lambda: repo)
    monkeypatch.setattr(claude_sub, "token_path", lambda: inside)
    with pytest.raises(claude_sub.AuthError) as ei:
        claude_sub.save_token(_TOKEN)
    assert ei.value.code == "inside_repo"
    assert not inside.exists() and not inside.parent.exists()


def test_write_failure_is_write_failed(monkeypatch):
    def _boom(path, data):
        raise OSError(28, "no space")

    monkeypatch.setattr(claude_sub, "atomic_write_bytes", _boom)
    with pytest.raises(claude_sub.AuthError) as ei:
        claude_sub.save_token(_TOKEN)
    assert ei.value.code == "write_failed"
    assert _TOKEN not in str(ei.value)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の権限")
def test_posix_permissions():
    claude_sub.save_token(_TOKEN)
    path = claude_sub.token_path()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_save_and_read_go_through_protect(monkeypatch):
    # 暗号化の層を差し替えて、書くのは _protect の結果・読むのは _unprotect の結果であることを見る（OS に依らない）。
    monkeypatch.setattr(claude_sub, "_protect", lambda d: b"P:" + d[::-1])
    monkeypatch.setattr(claude_sub, "_unprotect", lambda b: b[2:][::-1])
    claude_sub.save_token(_TOKEN)
    raw = claude_sub.token_path().read_bytes()
    assert raw == b"P:" + _TOKEN.encode()[::-1]
    assert claude_sub.read_token() == _TOKEN


def test_synced_location_is_refused(monkeypatch):
    monkeypatch.setattr(claude_sub, "is_fragile", lambda p: True)
    with pytest.raises(claude_sub.AuthError) as ei:
        claude_sub.save_token(_TOKEN)
    assert ei.value.code == "synced_location"
    assert not claude_sub.token_path().exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows は /proc/mounts を見ない")
def test_unverifiable_mount_is_refused(monkeypatch):
    # /proc/mounts の無い OS（macOS 等）では fs_kind が local を返すので、判定できないとして書かない。
    monkeypatch.setattr(claude_sub, "_mounts_readable", lambda: False)
    monkeypatch.setattr(claude_sub, "is_fragile", lambda p: False)
    with pytest.raises(claude_sub.AuthError) as ei:
        claude_sub.save_token(_TOKEN)
    assert ei.value.code == "synced_location"
    assert not claude_sub.token_path().exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlink を作るのに権限が要る")
def test_link_to_synced_mount_is_refused(monkeypatch, tmp_path):
    # ~/.myanalysis/secrets がローカルのリンクで、行き先が同期マウント。見かけのパスはローカルと判定される。
    synced = tmp_path / "synced"
    synced.mkdir()
    secrets = tmp_path / "home" / ".myanalysis" / "secrets"
    secrets.parent.mkdir(parents=True)
    secrets.symlink_to(synced)
    monkeypatch.setattr(claude_sub, "token_path", lambda: secrets / "claude_sub.bin")
    monkeypatch.setattr(claude_sub, "is_fragile", lambda p: str(p).startswith(str(synced)))
    with pytest.raises(claude_sub.AuthError) as ei:
        claude_sub.save_token(_TOKEN)
    assert ei.value.code == "synced_location"
    assert list(synced.iterdir()) == []


def test_unresolvable_dir_is_refused(monkeypatch):
    monkeypatch.setattr(claude_sub, "_real_dir", lambda d: None)
    with pytest.raises(claude_sub.AuthError) as ei:
        claude_sub.save_token(_TOKEN)
    assert ei.value.code == "synced_location"
    assert not claude_sub.token_path().exists()


def test_real_dir(tmp_path):
    assert claude_sub._real_dir(tmp_path) == tmp_path.resolve()
    assert claude_sub._real_dir(tmp_path / "missing") is None


def test_mounts_readable(monkeypatch, tmp_path):
    present = tmp_path / "mounts"
    present.write_text("/ / ext4 rw 0 0\n", encoding="utf-8")
    monkeypatch.setattr(claude_sub, "_PROC_MOUNTS", str(present))
    assert _REAL_MOUNTS_READABLE() is True
    monkeypatch.setattr(claude_sub, "_PROC_MOUNTS", str(tmp_path / "missing"))
    assert _REAL_MOUNTS_READABLE() is False


@pytest.mark.skipif(sys.platform != "win32" or not os.environ.get("MYANALYSIS_TEST_REAL_DPAPI"),
                    reason="本物の DPAPI（Windows の資格情報）は実機の確認で、MYANALYSIS_TEST_REAL_DPAPI=1 のときだけ")
def test_real_dpapi_roundtrip():
    claude_sub.save_token(_TOKEN)
    assert _TOKEN.encode() not in claude_sub.token_path().read_bytes()
    assert claude_sub.read_token() == _TOKEN


# ---- claude の設定の検査 ----


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


@pytest.fixture
def settings_tree(monkeypatch, tmp_path):
    """cwd = tmp/work/agent。上のフォルダは tmp/work までに限る（本物の上のフォルダを読まない）。"""
    cwd = tmp_path / "work" / "agent"
    cwd.mkdir(parents=True)
    monkeypatch.setattr(claude_sub, "_ancestor_dirs", lambda c: [c, c.parent])
    return cwd


def test_settings_override_none_when_clean(settings_tree, tmp_path):
    _write_json(tmp_path / "claude_config" / "settings.json", {"env": {"FOO": "1"}, "model": "opus"})
    assert claude_sub.settings_override(settings_tree) is None


@pytest.mark.parametrize("where", ["user", "project", "local", "parent", "managed"])
def test_settings_override_finds_api_key_helper(settings_tree, tmp_path, monkeypatch, where):
    managed = tmp_path / "managed" / "managed-settings.json"
    monkeypatch.setattr(claude_sub, "_managed_settings_files", lambda: [managed])
    path = {
        "user": tmp_path / "claude_config" / "settings.json",
        "project": settings_tree / ".claude" / "settings.json",
        "local": settings_tree / ".claude" / "settings.local.json",
        "parent": settings_tree.parent / ".claude" / "settings.json",
        "managed": managed,
    }[where]
    _write_json(path, {"apiKeyHelper": "/bin/echo SECRET-KEY"})
    found = claude_sub.settings_override(settings_tree)
    assert found == f"{path} (apiKeyHelper)"
    assert "SECRET" not in found


def test_settings_override_finds_auth_env_case_insensitively(settings_tree, tmp_path):
    _write_json(settings_tree / ".claude" / "settings.local.json",
                {"env": {"anthropic_api_key": "SECRET", "CLAUDE_CODE_OAUTH_TOKEN": "SECRET2", "X": "1"}})
    found = claude_sub.settings_override(settings_tree)
    assert found.endswith("(env: CLAUDE_CODE_OAUTH_TOKEN, anthropic_api_key)")
    assert "SECRET" not in found


@pytest.mark.parametrize("text,why", [("{not json", "not valid JSON"), ("[1, 2]", "not a JSON object")])
def test_settings_override_fails_closed_on_unparsable(settings_tree, tmp_path, text, why):
    _write_json(tmp_path / "claude_config" / "settings.json", text)
    assert claude_sub.settings_override(settings_tree).endswith(f"({why})")


def test_settings_override_policy_registry(settings_tree, monkeypatch):
    monkeypatch.setattr(claude_sub, "_policy_registry_present", lambda: True)
    assert "policy registry" in claude_sub.settings_override(settings_tree)


def test_settings_override_never_raises(settings_tree, monkeypatch):
    def _boom(cwd):
        raise RuntimeError("boom")
    monkeypatch.setattr(claude_sub, "_claude_config_dirs", _boom)
    assert claude_sub.settings_override(settings_tree) == "claude settings could not be checked (RuntimeError)"


def test_real_config_dirs_and_ancestors(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    cwd = tmp_path / "agent"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert _REAL_CONFIG_DIRS(cwd) == [tmp_path / "home" / ".claude"]
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert _REAL_CONFIG_DIRS(cwd) == [tmp_path / "cfg"]
    p = tmp_path / "a" / "b"
    assert _REAL_ANCESTORS(p) == [p, *p.parents]


def test_relative_cwd_checks_the_real_parents(monkeypatch, tmp_path):
    # [claude_code].cwd = "agent" で、MyAnalysis が work/repo から起動した。子の実 cwd は work/repo/agent。
    work = tmp_path / "work"
    (work / "repo" / "agent").mkdir(parents=True)
    monkeypatch.chdir(work / "repo")
    monkeypatch.setattr(claude_sub, "_ancestor_dirs", lambda c: [c, c.parent, c.parent.parent])
    _write_json(work / ".claude" / "settings.json", {"env": {"ANTHROPIC_AUTH_TOKEN": "SECRET"}})
    found = claude_sub.settings_override(Path("agent"))
    assert found == f"{work / '.claude' / 'settings.json'} (env: ANTHROPIC_AUTH_TOKEN)"


def test_relative_cwd_blocks_spawn(spawn, monkeypatch, tmp_path):
    work = tmp_path / "work"
    (work / "repo" / "agent").mkdir(parents=True)
    monkeypatch.chdir(work / "repo")
    monkeypatch.setattr(claude_sub, "_ancestor_dirs", lambda c: [c, c.parent, c.parent.parent])
    _write_json(work / ".claude" / "settings.json", {"apiKeyHelper": "/bin/echo k"})
    claude_sub.save_token(_TOKEN)
    b = ClaudeCodeBackend({"cwd": "agent"}, auth_env=claude_sub.auth_env)
    with pytest.raises(RuntimeError, match="apiKeyHelper"):
        _run_stream(b)
    with pytest.raises(RuntimeError, match="apiKeyHelper"):
        b.list_models()
    assert spawn == []


@pytest.mark.skipif(sys.platform == "win32", reason="symlink を作るのに権限が要る")
@pytest.mark.parametrize("where", ["links", "real"])
def test_symlinked_relative_cwd_checks_both_parents(monkeypatch, tmp_path, where):
    # cwd = "agent"（起動元は links/）。links/agent は real/agent へのリンク。
    # 見かけの親（links）も、リンクを辿った親（real）も見る。
    (tmp_path / "real" / "agent").mkdir(parents=True)
    (tmp_path / "links").mkdir()
    (tmp_path / "links" / "agent").symlink_to(tmp_path / "real" / "agent")
    monkeypatch.chdir(tmp_path / "links")
    monkeypatch.setattr(claude_sub, "_ancestor_dirs", lambda c: [c, c.parent])
    settings = tmp_path / where / ".claude" / "settings.json"
    _write_json(settings, {"apiKeyHelper": "x"})
    assert claude_sub.settings_override(Path("agent")) == f"{settings} (apiKeyHelper)"


def test_relative_config_dir_resolves_against_child_cwd(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_sub, "_claude_config_dirs", _REAL_CONFIG_DIRS)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "cfg")
    cwd = tmp_path / "agent"
    cwd.mkdir()
    monkeypatch.chdir(tmp_path)              # 起動元の cwd は子の cwd と違う
    _write_json(tmp_path / "cfg" / "settings.json", {"model": "opus"})   # 起動元基準の場所は見ない
    _write_json(cwd / "cfg" / "settings.json", {"apiKeyHelper": "x"})
    assert claude_sub.settings_override(cwd) == f"{cwd / 'cfg' / 'settings.json'} (apiKeyHelper)"


def test_tilde_config_dir_is_expanded(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_sub, "_claude_config_dirs", _REAL_CONFIG_DIRS)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/cc")
    _write_json(tmp_path / "home" / "cc" / "settings.json", {"apiKeyHelper": "x"})
    found = claude_sub.settings_override(tmp_path / "agent")
    assert found == f"{tmp_path / 'home' / 'cc' / 'settings.json'} (apiKeyHelper)"


def test_settings_override_blocks_spawn(spawn, monkeypatch):
    claude_sub.save_token(_TOKEN)
    monkeypatch.setattr(claude_sub, "settings_override", lambda cwd: "/x/settings.json (apiKeyHelper)")
    with pytest.raises(RuntimeError, match="apiKeyHelper"):
        _run_stream(_sub())
    with pytest.raises(RuntimeError, match="apiKeyHelper"):
        _sub().list_models()
    assert spawn == []


def test_settings_check_uses_child_cwd(spawn, monkeypatch, tmp_path):
    claude_sub.save_token(_TOKEN)
    seen = []
    monkeypatch.setattr(claude_sub, "settings_override", lambda cwd: seen.append(cwd))
    with pytest.raises(_Spawned):
        _sub().list_models()
    with pytest.raises(_Spawned):
        ClaudeCodeBackend({"cwd": str(tmp_path / "w")}, auth_env=claude_sub.auth_env).list_models()
    assert seen == [tmp_path, tmp_path / "w"]


# ---- 子の環境 ----


def test_build_env_drops_auth_vars_case_insensitively(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k1")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/cfg")
    env = os_environ_with_lower_key(monkeypatch)
    b = ClaudeCodeBackend({})
    out = b._build_env({claude_sub.TOKEN_ENV: _TOKEN})
    assert out[claude_sub.TOKEN_ENV] == _TOKEN
    assert "ANTHROPIC_API_KEY" not in out and "CLAUDE_CODE_USE_BEDROCK" not in out
    assert env not in out
    assert out["CLAUDE_CONFIG_DIR"] == "/cfg"


def os_environ_with_lower_key(monkeypatch) -> str:
    """os.environ に小文字の認証の変数を足す（POSIX では別の名前として残る）。その名前を返す。"""
    name = "anthropic_auth_token"
    monkeypatch.setenv(name, "lower")
    return name


def test_build_env_without_auth_is_unchanged(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k1")
    b = ClaudeCodeBackend({})
    assert b._build_env() == b._build_env(None)
    assert b._build_env()["ANTHROPIC_API_KEY"] == "k1"


def test_sub_stream_and_list_models_pass_token(spawn, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k1")
    claude_sub.save_token(_TOKEN)
    with pytest.raises(_Spawned):
        _run_stream(_sub())
    with pytest.raises(_Spawned):
        _sub().list_models()
    for env in spawn:
        assert env[claude_sub.TOKEN_ENV] == _TOKEN
        assert "ANTHROPIC_API_KEY" not in env


def test_main_does_not_get_the_token(spawn, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k1")
    claude_sub.save_token(_TOKEN)
    with pytest.raises(_Spawned):
        _run_stream(ClaudeCodeBackend({}))
    assert spawn[0].get(claude_sub.TOKEN_ENV) != _TOKEN
    assert spawn[0]["ANTHROPIC_API_KEY"] == "k1"


def test_missing_token_never_spawns(spawn):
    with pytest.raises(RuntimeError, match="not set"):
        _run_stream(_sub())
    with pytest.raises(RuntimeError, match="not set"):
        _sub().list_models()
    from llm_backend.ping import ping_backend
    r = ping_backend(_sub())
    assert not r.ok and "not set" in r.error
    assert spawn == []


# ---- factory・設定・エンジン ----


def _config(monkeypatch, config: dict, models: dict | None = None):
    monkeypatch.setattr(llm_backend, "backend_config", lambda: config)
    monkeypatch.setattr(engines, "backend_config", lambda: config)
    import llm_backend.model_settings as ms
    monkeypatch.setattr(ms, "model_config", lambda: models or {})


def test_factory_does_not_read_token(monkeypatch):
    _config(monkeypatch, {"claude_code": {"bin": "claude"}})
    b = llm_backend.build_backend("claude-sub")
    assert isinstance(b, ClaudeCodeBackend) and b.name == "claude-sub"
    assert ClaudeCodeBackend({}).name == "claude-code"


def test_claude_sub_settings_inherits_operational_keys(monkeypatch):
    _config(monkeypatch, {
        "claude_code": {"bin": "claude", "permission_mode": "plan", "allowed_tools": "Read",
                        "model": "opus", "effort": "max", "thinking": "enabled"},
        "claude_sub": {"permission_mode": "acceptEdits"},
    }, models={"claude_code": {"model": "sonnet"}, "claude_sub": {"model": "haiku"}})
    s = llm_backend.claude_sub_settings()
    assert s["bin"] == "claude" and s["allowed_tools"] == "Read"
    assert s["permission_mode"] == "acceptEdits"
    assert s["model"] == "haiku"
    assert "effort" not in s and "thinking" not in s
    assert llm_backend.claude_sub_settings({"model": "x"}) == {
        "bin": "claude", "permission_mode": "plan", "allowed_tools": "Read", "model": "x"}


def test_current_engine_id_and_session_roundtrip(monkeypatch):
    _config(monkeypatch, {"backend": {"name": "claude-sub"}, "claude_code": {"bin": ""}})
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    assert engines.current_engine_id() == "claude-sub"
    e = engines.engine_by_id("claude-sub")
    s = engines.session_settings(e, "claude-opus-5-5", "", effort="high")
    assert s["model"] == "claude-opus-5-5" and s["effort"] == "high"
    b = llm_backend.build_backend(e.backend_key, s)
    assert b.name == "claude-sub" and b.model == "claude-opus-5-5"
