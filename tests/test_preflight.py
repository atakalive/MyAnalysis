"""llm_backend.preflight — 対応ドライバの導入状況の棚卸し（Qt 非依存）。

実プロセスは一切起動しない: `_run` / `_which` を差し替えて判定だけを検証する。
"""
from __future__ import annotations

import json

import pytest

from llm_backend import preflight
from llm_backend.preflight import EngineStatus, check_engine

# autouse fixture が差し替える前の本物（C-6 のテストで使う）。
_REAL_CLAUDE_AUTH = preflight._claude_auth


@pytest.fixture(autouse=True)
def _no_real_processes(monkeypatch):
    """既定では「何も入っていない」状態にしておく。各テストが必要な分だけ生やす。"""
    monkeypatch.setattr(preflight, "_which", lambda name: None)
    monkeypatch.setattr(preflight, "_run", lambda cmd, timeout: None)
    # detail は中立な事実だけ（説明文は notes の i18n キー）。実物と同じ形にしておく。
    monkeypatch.setattr(preflight, "_claude_auth", lambda *a, **k: ("missing", ""))
    monkeypatch.setattr(preflight, "_pi_providers_from_authfile", lambda: None)
    # 実リポジトリの llm_backend/config.toml / models.toml を読まない。
    monkeypatch.setattr(preflight, "_claude_config", lambda: {})


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
    # ヘッダ除去・重複なし。anthropic は「使う想定外」なので落ちる（順は白名単側）。
    assert s.authed_providers == ("openai-codex", "github-copilot")


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
    def cjk(t):
        return any("぀" <= c <= "ヿ" or "一" <= c <= "鿿" for c in t or "")
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
        "backend.status.note.permission_mode_empty",
    }
    for path in sorted(i18n_dir().glob("*.toml")):
        with open(path, "rb") as f:
            cat = tomllib.load(f)
        assert used <= set(cat), (path.name, used - set(cat))


def test_installed_engine_still_offers_the_command_for_updating(monkeypatch):
    """導入済みでも npm コマンドは返す（GUI が「更新」ラベルで使う）。"""
    _tools(monkeypatch, present=("node", "npm", "pi"))

    def _run(cmd, timeout):
        base = cmd[0].rsplit("/", 1)[-1]
        if base == "node":
            return (0, "v24.18.0")
        if base == "npm":
            return (0, "11.16.0")
        if "--version" in cmd:
            return (0, "0.83.0")
        return None
    monkeypatch.setattr(preflight, "_run", _run)
    s = check_engine("pi")
    assert s.binary_state == "ok"
    assert s.install == ("npm", "i", "-g", "@earendil-works/pi-coding-agent")


# ---- pi の provider は「使う想定のもの」だけ見せる ----


def _pi_with_providers(monkeypatch, listing_providers):
    _tools(monkeypatch, present=("node", "npm", "pi"))
    rows = "provider  model  ctx\n" + "".join(
        f"{p}  m  1M\n" for p in listing_providers
    )

    def _run(cmd, timeout):
        base = cmd[0].rsplit("/", 1)[-1]
        if base == "node":
            return (0, "v24.18.0")
        if base == "npm":
            return (0, "11.16.0")
        if "--list-models" in cmd:
            return (0, rows)
        if "--version" in cmd:
            return (0, "0.83.0")
        return None
    monkeypatch.setattr(preflight, "_run", _run)
    return check_engine("pi")


def test_out_of_scope_providers_are_not_shown(monkeypatch):
    """anthropic 等を並べると「そこからも使える」と誤認させる（別課金）。"""
    s = _pi_with_providers(
        monkeypatch, ["anthropic", "openai-codex", "github-copilot", "google"]
    )
    assert s.authed_providers == ("openai-codex", "github-copilot")
    assert "anthropic" not in s.authed_providers


def test_only_out_of_scope_providers_counts_as_no_usable_auth(monkeypatch):
    """✓ なのに一覧が空、では意味が分からない。使える認証が無い扱いにする。"""
    s = _pi_with_providers(monkeypatch, ["anthropic", "google"])
    assert s.authed_providers == ()
    assert s.auth_state == "missing"


def test_visible_providers_match_the_dropdown_seed():
    """表示フィルタとドロップダウンの種は同じ定義を参照すること（別々だと必ずズレる）。"""
    from llm_backend.engines import PI_PROVIDERS, engine_by_id
    assert engine_by_id("pi").provider_suggestions == PI_PROVIDERS
    assert preflight._pi_visible_providers() == PI_PROVIDERS


def test_openai_is_not_listed_separately_from_openai_codex():
    """素の `openai` は `openai-codex` と同じ用途なので並べない。"""
    from llm_backend.engines import PI_PROVIDERS
    assert "openai" not in PI_PROVIDERS
    assert "openai-codex" in PI_PROVIDERS


# ---- never raise: 壊れた argv でも落ちない ----


def test_degenerate_argv_never_raises():
    """契約は never raise。_run は subprocess の TypeError まで握る必要がある
    （argv に None が混じると list2cmdline が投げる）。"""
    assert preflight._which(None) is None
    assert preflight._which("") is None
    assert preflight._which(123) is None
    assert preflight._wrap([]) == []
    assert preflight._wrap([None]) == [None]
    assert preflight._run([None], 1.0) is None
    assert preflight._run([], 1.0) is None
    assert preflight._pi_providers_from_list_models(None) is None


# ----- permission_mode の注意（Issue #103 G-7） -----

_PERM_NOTE = "backend.status.note.permission_mode_empty"


def _note_keys(st):
    return [k for k, _ in st.notes]


@pytest.mark.parametrize("engine_id", ["claude-vscode", "claude-cli"])
def test_empty_permission_mode_adds_note(monkeypatch, engine_id):
    # 実ユーザーの home / PATH を探索しない。
    monkeypatch.setattr(preflight, "_claude_binary", lambda bin_value: ("missing", None))
    monkeypatch.setattr(preflight, "_claude_config", lambda: {"permission_mode": ""})
    assert _PERM_NOTE in _note_keys(preflight.check_engine(engine_id))


@pytest.mark.parametrize("engine_id", ["claude-vscode", "claude-cli"])
@pytest.mark.parametrize("cfg", [{"permission_mode": "plan"}, {}])
def test_valid_or_unset_permission_mode_has_no_note(monkeypatch, engine_id, cfg):
    monkeypatch.setattr(preflight, "_claude_binary", lambda bin_value: ("missing", None))
    monkeypatch.setattr(preflight, "_claude_config", lambda: cfg)
    assert _PERM_NOTE not in _note_keys(preflight.check_engine(engine_id))


@pytest.mark.parametrize("engine_id", ["claude-vscode", "claude-cli"])
def test_claude_config_failure_adds_no_note(monkeypatch, engine_id):
    monkeypatch.setattr(preflight, "_claude_binary", lambda bin_value: ("missing", None))

    def boom():
        raise RuntimeError("broken config")

    monkeypatch.setattr(preflight, "_claude_config", boom)
    st = preflight.check_engine(engine_id)
    assert _PERM_NOTE not in _note_keys(st)
    assert st.binary_state == "missing"   # 保険の except に落ちていない


# ---- pi の最低版（--no-context-files） ----


# 境界は直下・ちょうど・直上を固定する（0.67.4 ちょうどの利用者に注意を出さない）。
@pytest.mark.parametrize("ver,expect", [
    ("0.66.0", True), ("0.67.3", True), ("0.67.4", False), ("0.67.5", False),
    ("0.71.1", False),
])
def test_pi_too_old_note(monkeypatch, ver, expect):
    monkeypatch.delenv("PI_API_KEY", raising=False)
    monkeypatch.setattr(
        preflight, "_which",
        lambda n: f"/usr/bin/{n}" if n in ("pi", "node", "npm") else None,
    )

    def _run(cmd, timeout):
        if cmd == ["/usr/bin/pi", "--version"]:
            return (0, ver)
        return None
    monkeypatch.setattr(preflight, "_run", _run)
    s = check_engine("pi")
    note = ("backend.status.note.pi_too_old", {"need": "0.67.4"})
    assert (note in s.notes) is expect


# ---- PI_API_KEY は使われない ----


@pytest.mark.parametrize("value,expect", [("secret", True), (None, False)])
def test_pi_api_key_note(monkeypatch, value, expect):
    if value is None:
        monkeypatch.delenv("PI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("PI_API_KEY", value)
    _tools(monkeypatch, present=("node", "npm", "pi"))
    s = check_engine("pi")
    note = ("backend.status.note.pi_api_key_unused", {})
    assert (note in s.notes) is expect


# ---- claude の認証: auth status を優先、取れなければ静的推定 ----


@pytest.fixture
def _claude_home(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight.Path, "home", lambda: tmp_path)
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def _write_credentials(home):
    d = home / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    (d / ".credentials.json").write_text('{"x": 1}', encoding="utf-8")


def test_claude_auth_uses_auth_status(monkeypatch, _claude_home):
    calls = []

    def _run(cmd, timeout):
        calls.append(list(cmd))
        return (0, 'warn\n{"loggedIn": true}')
    monkeypatch.setattr(preflight, "_run", _run)
    assert _REAL_CLAUDE_AUTH("/x/claude") == ("ok", "")
    assert calls == [["/x/claude", "auth", "status", "--json"]]


def test_claude_auth_logged_out(monkeypatch, _claude_home):
    _write_credentials(_claude_home)
    monkeypatch.setattr(preflight, "_run", lambda cmd, timeout: (1, '{"loggedIn": false}'))
    assert _REAL_CLAUDE_AUTH("/x/claude") == ("missing", "")


def test_claude_auth_cli_logged_out_wins_over_env_token(monkeypatch, _claude_home):
    # CLI の判定を優先する契約: CLI が loggedIn=false と言えば、env にトークンがあっても missing。
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setattr(preflight, "_run", lambda cmd, timeout: (1, '{"loggedIn": false}'))
    assert _REAL_CLAUDE_AUTH("/x/claude") == ("missing", "")


def test_claude_auth_falls_back_when_probe_fails(monkeypatch, _claude_home):
    monkeypatch.setattr(preflight, "_run", lambda cmd, timeout: None)
    assert _REAL_CLAUDE_AUTH("/x/claude")[0] == "missing"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    assert _REAL_CLAUDE_AUTH("/x/claude")[0] == "ok"
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    _write_credentials(_claude_home)
    assert _REAL_CLAUDE_AUTH("/x/claude")[0] == "ok"


def test_claude_auth_without_binary_does_not_probe(monkeypatch, _claude_home):
    calls = []

    def _run(cmd, timeout):
        calls.append(cmd)
        return (0, '{"loggedIn": true}')
    monkeypatch.setattr(preflight, "_run", _run)
    assert _REAL_CLAUDE_AUTH(None) == ("missing", "")
    assert calls == []


def test_check_engine_passes_claude_binary_to_auth(monkeypatch):
    monkeypatch.setattr(preflight, "_claude_binary", lambda bin_value: ("ok", "/x/claude"))
    monkeypatch.setattr(
        preflight, "_run",
        lambda cmd, timeout: (0, "2.1.282 (Claude Code)") if "--version" in cmd else None,
    )
    seen = []

    def _auth(bin_path):
        seen.append(bin_path)
        return "ok", ""
    monkeypatch.setattr(preflight, "_claude_auth", _auth)
    check_engine("claude-cli")
    assert seen == ["/x/claude"]
