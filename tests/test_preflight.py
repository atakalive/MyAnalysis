"""llm_backend.preflight — 対応ドライバの導入状況の棚卸し（Qt 非依存）。

実プロセスは一切起動しない: `_run` / `_which` を差し替えて判定だけを検証する。
"""
from __future__ import annotations

import json

import pytest

from llm_backend import preflight
from llm_backend.preflight import EngineStatus, check_engine


@pytest.fixture(autouse=True)
def _no_real_processes(monkeypatch):
    """既定では「何も入っていない」状態にしておく。各テストが必要な分だけ生やす。"""
    monkeypatch.setattr(preflight, "_which", lambda name: None)
    monkeypatch.setattr(preflight, "_run", lambda cmd, timeout: None)
    # detail は中立な事実だけ（説明文は notes の i18n キー）。実物と同じ形にしておく。
    monkeypatch.setattr(preflight, "_claude_auth", lambda: ("missing", ""))
    monkeypatch.setattr(preflight, "_pi_providers_from_authfile", lambda: None)


def _tools(monkeypatch, *, node="v24.18.0", npm="11.16.0", present=("node", "npm")):
    """node/npm の有無とバージョンを仕込む。"""
    monkeypatch.setattr(
        preflight, "_which", lambda n: (f"/usr/bin/{n}" if n in present else None)
    )
    table = {"node": node, "npm": npm}

    def _run(cmd, timeout):
        base = cmd[0].rsplit("/", 1)[-1]
        if base in table and "--version" in cmd:
            return (0, table[base])
        return None
    monkeypatch.setattr(preflight, "_run", _run)


# ---- 0 段目: node と npm は別実体 ----


def test_npm_missing_blocks_install_even_with_node(monkeypatch):
    """インストールで叩くのは npm。node があっても npm が無ければ導入不可。"""
    _tools(monkeypatch, present=("node",))
    s = check_engine("pi")
    assert s.prereq_state == "missing"
    assert s.install is None
    assert s.blocking_stage == "prereq"
    keys = dict(s.notes)
    assert "backend.status.note.missing_tools" in keys
    assert keys["backend.status.note.missing_tools"]["tools"] == "npm"


def test_node_missing_blocks_install(monkeypatch):
    _tools(monkeypatch, present=("npm",))
    s = check_engine("pi")
    assert s.prereq_state == "missing" and s.install is None


def test_node_too_old_blocks_pi_install(monkeypatch):
    """pi の engines は node>=22.19。満たさないなら押させない。"""
    _tools(monkeypatch, node="v20.11.0")
    s = check_engine("pi")
    assert s.prereq_state == "missing"
    assert s.install is None
    assert dict(s.notes)["backend.status.note.node_too_old"]["need"] == "22.19"


def test_node_new_enough_allows_pi_install(monkeypatch):
    _tools(monkeypatch)
    s = check_engine("pi")
    assert s.prereq_state == "ok"
    assert s.install == ("npm", "i", "-g", "@earendil-works/pi-coding-agent")


# ---- 1 段目: missing と unknown を混ぜない ----


def test_timeout_is_unknown_not_missing_and_offers_no_install(monkeypatch):
    """遅いだけ／固まっただけで「入っていません」と言って再インストールを勧めない。"""
    _tools(monkeypatch, present=("node", "npm", "pi"))

    def _run(cmd, timeout):
        base = cmd[0].rsplit("/", 1)[-1]
        if base in ("node", "npm"):
            return (0, "v24.18.0" if base == "node" else "11.16.0")
        return None                      # pi --version がタイムアウト
    monkeypatch.setattr(preflight, "_run", _run)

    s = check_engine("pi")
    assert s.binary_state == "unknown"
    assert s.install is None             # ここが missing だと誤誘導になる


def test_binary_absent_is_missing_with_install(monkeypatch):
    _tools(monkeypatch)                  # pi は present に無い
    s = check_engine("pi")
    assert s.binary_state == "missing"
    assert s.install is not None
    assert s.blocking_stage == "binary"


def test_claude_cli_not_on_path_is_missing_not_unknown(monkeypatch):
    """_resolve_bin は PATH に無くても裸の名前を返す。ok 扱いすると導入ボタンが消える。"""
    _tools(monkeypatch)
    monkeypatch.setattr(
        preflight, "_claude_binary",
        lambda b: ("missing", None) if b == "claude" else ("ok", "/ext/claude"),
    )
    s = check_engine("claude-cli")
    assert s.binary_state == "missing"
    assert s.install == ("npm", "i", "-g", "@anthropic-ai/claude-code")


# ---- バージョン文字列の形はエンジンごとに違う ----


@pytest.mark.parametrize("raw,want", [
    ("0.83.0", "0.83.0"),                    # pi
    ("codex-cli 0.146.0", "0.146.0"),        # codex
    ("v24.18.0", "24.18.0"),                 # node
    ("2.1.220 (Claude Code)", "2.1.220"),     # claude
])
def test_version_parsing_handles_each_shape(raw, want):
    assert preflight._parse_version(raw) == want


def test_version_parsing_falls_back_to_raw_line():
    assert preflight._parse_version("weird build") == "weird build"


def test_version_parsing_empty_is_none():
    assert preflight._parse_version("") is None
    assert preflight._parse_version(None) is None


# ---- 2 段目: 認証は合否ではなく情報 ----


def test_pi_providers_enumerated_from_list_models(monkeypatch):
    """返るのは実際に使える provider だけ。失効したものは出てこない。"""
    _tools(monkeypatch, present=("node", "npm", "pi"))
    listing = (
        "provider        model            context\n"
        "anthropic       claude-opus-5    1M\n"
        "anthropic       claude-sonnet-5  1M\n"
        "github-copilot  gpt-5.4          1M\n"
        "openai-codex    gpt-5.5          272K\n"
    )

    def _run(cmd, timeout):
        base = cmd[0].rsplit("/", 1)[-1]
        if base == "node":
            return (0, "v24.18.0")
        if base == "npm":
            return (0, "11.16.0")
        if "--list-models" in cmd:
            return (0, listing)
        if "--version" in cmd:
            return (0, "0.83.0")
        return None
    monkeypatch.setattr(preflight, "_run", _run)

    s = check_engine("pi")
    assert s.auth_state == "ok"
    assert s.authed_providers == (
        "anthropic", "github-copilot", "openai-codex",     # 重複なし・ヘッダ除去
    )


def test_pi_falls_back_to_authfile_when_offline(monkeypatch):
    """オフラインでも「トークンはある」ことは言える。ただし失効は判定不可なので unknown。"""
    _tools(monkeypatch, present=("node", "npm", "pi"))

    def _run(cmd, timeout):
        base = cmd[0].rsplit("/", 1)[-1]
        if base == "node":
            return (0, "v24.18.0")
        if base == "npm":
            return (0, "11.16.0")
        if "--version" in cmd:
            return (0, "0.83.0")
        return None                      # --list-models は失敗（オフライン）
    monkeypatch.setattr(preflight, "_run", _run)
    monkeypatch.setattr(
        preflight, "_pi_providers_from_authfile", lambda: ("openai-codex",)
    )
    s = check_engine("pi")
    assert s.auth_state == "unknown"
    assert s.authed_providers == ("openai-codex",)


def test_openai_http_auth_is_never_a_failure(monkeypatch):
    """キー無しはローカル endpoint では正常。✗ にしてはいけない。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    s = check_engine("openai-http")
    assert s.auth_state == "n/a"
    assert s.blocking_stage is None
    assert "backend.status.note.openai_env_unset" in dict(s.notes)


def test_mock_needs_nothing():
    s = check_engine("mock")
    assert s.blocking_stage is None
    assert s.install is None and s.login is None


# ---- never raise ----


def test_never_raises_on_broken_authfile(monkeypatch, tmp_path):
    _tools(monkeypatch, present=("node", "npm", "pi"))
    monkeypatch.setattr(preflight.Path, "home", staticmethod(lambda: tmp_path))
    p = tmp_path / ".pi" / "agent"
    p.mkdir(parents=True)
    (p / "auth.json").write_text("{ broken", encoding="utf-8")
    monkeypatch.setattr(
        preflight, "_pi_providers_from_authfile",
        preflight._pi_providers_from_authfile,      # 実物を使う
    )
    assert isinstance(check_engine("pi"), EngineStatus)


def test_never_raises_when_discovery_explodes(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(preflight, "_toolchain", _boom)
    s = check_engine("codex")
    assert isinstance(s, EngineStatus) and s.binary_state == "unknown"


def test_authfile_reader_handles_non_dict(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight.Path, "home", staticmethod(lambda: tmp_path))
    p = tmp_path / ".pi" / "agent"
    p.mkdir(parents=True)
    (p / "auth.json").write_text(json.dumps([1, 2]), encoding="utf-8")
    assert preflight._pi_providers_from_authfile() is None


# ---- 一覧は現在の設定に依存しない ----


def test_claude_rows_probe_fixed_acquisition_paths(monkeypatch):
    """claude-vscode は常に VS Code 拡張、claude-cli は常に PATH を見る。
    [claude_code].bin が何であっても各行の判定は変わらない。"""
    seen = []

    def _fake(bin_value):
        seen.append(bin_value)
        return "missing", None
    monkeypatch.setattr(preflight, "_claude_binary", _fake)
    _tools(monkeypatch)
    check_engine("claude-vscode")
    check_engine("claude-cli")
    assert seen == ["", "claude"]        # 現在設定ではなく固定値


def test_check_all_covers_every_catalog_engine(monkeypatch):
    _tools(monkeypatch)
    from llm_backend.engines import ENGINES
    got = {s.engine_id for s in preflight.check_all()}
    assert got == {e.id for e in ENGINES}


# ---- 課金・トークン回転を招く呼び出しをしない ----


def test_never_invokes_pi_auth_or_a_model_turn(monkeypatch):
    """`pi auth print-bearer-token` は 30 分以内に切れるトークンをリフレッシュする。
    pi(WSL)/pi(Windows)/codex で refresh token を共有していると他がログアウトする。"""
    calls: list[list[str]] = []

    def _run(cmd, timeout):
        calls.append(list(cmd))
        return None
    monkeypatch.setattr(preflight, "_which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(preflight, "_run", _run)
    preflight.check_all()
    flat = [" ".join(c) for c in calls]
    assert not any("auth" in c for c in flat), flat
    assert not any("--print" in c or "exec" in c for c in flat), flat


# ---- i18n: preflight は人間向けの文を作らない ----


def test_preflight_returns_no_localised_prose(monkeypatch):
    """preflight は CLI からも GUI からも使うので i18n を知らない。日本語を埋め込むと
    en 表示に混ざる（実際に「Install」ボタンの隣に日本語が出て気づいた）。
    説明文は (i18n キー, params) を notes で返し、detail は中立な事実だけにする。"""
    _tools(monkeypatch, present=("node", "npm"))
    cjk = lambda t: any("぀" <= c <= "ヿ" or "一" <= c <= "鿿" for c in t or "")
    for st in preflight.check_all():
        for fld in (st.prereq_detail, st.auth_detail, st.version or ""):
            assert not cjk(fld), (st.engine_id, fld)
        for key, _params in st.notes:
            assert key.startswith("backend.status."), key


def test_note_keys_exist_in_every_catalog():
    """notes の i18n キーが実在すること（この GUI ファイルは IN_FILES 外で守られない）。"""
    import tomllib
    from common.paths import i18n_dir
    used = {
        "backend.status.note.missing_tools",
        "backend.status.note.node_too_old",
        "backend.status.note.offline_auth",
        "backend.status.note.openai_env_unset",
        "backend.status.note.vscode_ext",
    }
    for path in sorted(i18n_dir().glob("*.toml")):
        with open(path, "rb") as f:
            cat = tomllib.load(f)
        assert used <= set(cat), (path.name, used - set(cat))
