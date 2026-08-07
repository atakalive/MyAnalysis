"""Tests for llm_backend.engines — catalog, candidate settings, apply."""
import os
import tomllib
from types import SimpleNamespace

import pytest

from llm_backend import engines
from llm_backend.engines import (
    ENGINES,
    apply_selection,
    candidate_settings,
    combo_choices,
    current_engine_id,
    engine_by_id,
    save_choices,
    saved_choices,
    session_settings,
)
from llm_backend.settings_store import set_toml_keys as _real_set_toml_keys


class _Fake:
    """A drop-in for backend_config()/model_config() (callable + cache_clear)."""

    def __init__(self, data):
        self.data = data
        self.cleared = 0

    def __call__(self):
        return self.data

    def cache_clear(self):
        self.cleared += 1


def _cfg(data):
    return _Fake(data)


# --------------------------------------------------------------------------- #
# current_engine_id                                                           #
# --------------------------------------------------------------------------- #

def _no_env(monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


def test_current_engine_id_claude_vscode(monkeypatch):
    _no_env(monkeypatch)
    monkeypatch.setattr(
        engines, "backend_config",
        _cfg({"backend": {"name": "claude"}, "claude_code": {"bin": ""}}),
    )
    assert current_engine_id() == "claude-vscode"


def test_current_engine_id_claude_cli(monkeypatch):
    _no_env(monkeypatch)
    monkeypatch.setattr(
        engines, "backend_config",
        _cfg({"backend": {"name": "claude"}, "claude_code": {"bin": "claude"}}),
    )
    assert current_engine_id() == "claude-cli"


def test_current_engine_id_pi(monkeypatch):
    _no_env(monkeypatch)
    monkeypatch.setattr(engines, "backend_config", _cfg({"backend": {"name": "pi"}}))
    assert current_engine_id() == "pi"


def test_current_engine_id_env_wins(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("LLM_BACKEND", "mock")
    monkeypatch.setattr(engines, "backend_config", _cfg({"backend": {"name": "claude"}}))
    assert current_engine_id() == "mock"


def test_current_engine_id_unknown_maps_to_openai_http(monkeypatch):
    _no_env(monkeypatch)
    monkeypatch.setattr(engines, "backend_config", _cfg({"backend": {"name": "weird"}}))
    assert current_engine_id() == "openai-http"


# --------------------------------------------------------------------------- #
# candidate_settings                                                          #
# --------------------------------------------------------------------------- #

def test_candidate_settings_preserves_merged_keys(monkeypatch):
    merged = {"model": "opus", "thinking": "enabled", "effort": "xhigh", "bin": ""}
    monkeypatch.setattr(engines, "merged_settings", lambda k, base: dict(merged))
    monkeypatch.setattr(engines, "backend_config", _cfg({}))
    e = engine_by_id("claude-vscode")
    s = candidate_settings(e, "sonnet", "", engine_changed=False)
    assert s["thinking"] == "enabled"
    assert s["effort"] == "xhigh"
    assert s["model"] == "sonnet"


def test_candidate_settings_custom_bin_preserved_when_unchanged(monkeypatch):
    merged = {"model": "opus", "bin": "/custom/claude"}
    monkeypatch.setattr(engines, "merged_settings", lambda k, base: dict(merged))
    monkeypatch.setattr(engines, "backend_config", _cfg({}))
    e = engine_by_id("claude-cli")
    s = candidate_settings(e, "sonnet", "", engine_changed=False)
    assert s["bin"] == "/custom/claude"          # config_patch NOT applied


def test_candidate_settings_config_patch_applied_when_changed(monkeypatch):
    merged = {"model": "opus", "bin": "/custom/claude"}
    monkeypatch.setattr(engines, "merged_settings", lambda k, base: dict(merged))
    monkeypatch.setattr(engines, "backend_config", _cfg({}))
    e = engine_by_id("claude-cli")
    s = candidate_settings(e, "sonnet", "", engine_changed=True)
    assert s["bin"] == "claude"                  # config_patch applied


def test_candidate_settings_mock(monkeypatch):
    e = engine_by_id("mock")
    assert candidate_settings(e, "m", "", engine_changed=True) == {"model": "m"}


# --------------------------------------------------------------------------- #
# apply_selection                                                             #
# --------------------------------------------------------------------------- #

_CONFIG_SEED = (
    "[backend]\n"
    'name = "mock"\n'
    "\n"
    "[claude_code]\n"
    'bin = ""\n'
    'permission_mode = "bypassPermissions"\n'
)


@pytest.fixture
def apply_env(tmp_path, monkeypatch):
    cfg_p = tmp_path / "config.toml"
    mdl_p = tmp_path / "models.toml"
    monkeypatch.setattr(engines, "config_toml_path", lambda: cfg_p)
    monkeypatch.setattr(engines, "models_toml_path", lambda: mdl_p)
    bc = _cfg({})
    mc = _cfg({})
    monkeypatch.setattr(engines, "backend_config", bc)
    monkeypatch.setattr(engines, "model_config", mc)
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    return SimpleNamespace(cfg_p=cfg_p, mdl_p=mdl_p, bc=bc, mc=mc)


def test_apply_writes_backend_bin_and_model(apply_env):
    apply_env.cfg_p.write_text(_CONFIG_SEED, encoding="utf-8")
    apply_selection(engine_by_id("claude-cli"), "sonnet", "", engine_changed=True)
    cfgd = tomllib.loads(apply_env.cfg_p.read_text(encoding="utf-8"))
    assert cfgd["backend"]["name"] == "claude"
    assert cfgd["claude_code"]["bin"] == "claude"
    mdld = tomllib.loads(apply_env.mdl_p.read_text(encoding="utf-8"))
    assert mdld["claude_code"]["model"] == "sonnet"
    assert apply_env.bc.cleared >= 1
    assert apply_env.mc.cleared >= 1


def test_apply_engine_unchanged_skips_config_patch(apply_env):
    apply_env.cfg_p.write_text(
        '[backend]\nname = "claude"\n\n[claude_code]\nbin = "/custom/claude"\n',
        encoding="utf-8",
    )
    apply_selection(engine_by_id("claude-cli"), "sonnet", "", engine_changed=False)
    cfgd = tomllib.loads(apply_env.cfg_p.read_text(encoding="utf-8"))
    assert cfgd["claude_code"]["bin"] == "/custom/claude"   # untouched
    assert cfgd["backend"]["name"] == "claude"              # always written


def test_apply_clear_a_config_key_absent_noop(apply_env):
    # config.toml has no model key; models.toml gets "" → config no-op.
    apply_env.cfg_p.write_text(_CONFIG_SEED, encoding="utf-8")
    apply_env.bc.data = {"claude_code": {"bin": "", "permission_mode": "x"}}
    apply_selection(engine_by_id("claude-vscode"), "", "", engine_changed=False)
    cfgd = tomllib.loads(apply_env.cfg_p.read_text(encoding="utf-8"))
    assert "model" not in cfgd["claude_code"]
    mdld = tomllib.loads(apply_env.mdl_p.read_text(encoding="utf-8"))
    assert mdld["claude_code"]["model"] == ""


def test_apply_clear_b_config_key_cleared(apply_env):
    # config.toml holds a non-empty model → cleared to "" when model emptied.
    apply_env.cfg_p.write_text(
        '[backend]\nname = "claude"\n\n[claude_code]\nbin = ""\nmodel = "cfgmodel"\n',
        encoding="utf-8",
    )
    apply_env.bc.data = {"claude_code": {"bin": "", "model": "cfgmodel"}}
    apply_selection(engine_by_id("claude-vscode"), "", "", engine_changed=False)
    cfgd = tomllib.loads(apply_env.cfg_p.read_text(encoding="utf-8"))
    assert cfgd["claude_code"]["model"] == ""


def _fail_on_config(cfg_p):
    def _set(path, changes):
        if path == cfg_p:
            raise RuntimeError("config write boom")
        return _real_set_toml_keys(path, changes)
    return _set


def test_apply_config_fail_rolls_back_existing_models(apply_env, monkeypatch):
    apply_env.cfg_p.write_text(
        '[backend]\nname = "claude"\n\n[claude_code]\nbin = ""\n', encoding="utf-8"
    )
    apply_env.mdl_p.write_text(
        '[claude_code]\nmodel = "oldm"\n', encoding="utf-8"
    )
    monkeypatch.setattr(engines, "set_toml_keys", _fail_on_config(apply_env.cfg_p))
    with pytest.raises(RuntimeError):
        apply_selection(engine_by_id("claude-cli"), "newm", "", engine_changed=True)
    # config.toml untouched (old engine, old bin), models.toml rolled back.
    cfgd = tomllib.loads(apply_env.cfg_p.read_text(encoding="utf-8"))
    assert cfgd["claude_code"]["bin"] == ""
    mdld = tomllib.loads(apply_env.mdl_p.read_text(encoding="utf-8"))
    assert mdld["claude_code"]["model"] == "oldm"
    assert apply_env.bc.cleared == 0   # caches NOT refreshed on failure


def test_apply_config_fail_unlinks_absent_models(apply_env, monkeypatch):
    apply_env.cfg_p.write_text(
        '[backend]\nname = "claude"\n\n[claude_code]\nbin = ""\n', encoding="utf-8"
    )
    assert not apply_env.mdl_p.exists()
    monkeypatch.setattr(engines, "set_toml_keys", _fail_on_config(apply_env.cfg_p))
    with pytest.raises(RuntimeError):
        apply_selection(engine_by_id("claude-cli"), "newm", "", engine_changed=True)
    assert not apply_env.mdl_p.exists()   # created then rolled back (unlinked)


def test_apply_config_fail_rollback_preserves_crlf(apply_env, monkeypatch):
    # reviewer/reviewer code P2: the pre-apply snapshot must keep CRLF verbatim so the
    # rollback is byte-exact (Path.read_text would collapse CRLF→LF).
    apply_env.cfg_p.write_text(
        '[backend]\nname = "claude"\n\n[claude_code]\nbin = ""\n', encoding="utf-8"
    )
    crlf = b'[claude_code]\r\nmodel = "oldm"\r\n'
    apply_env.mdl_p.write_bytes(crlf)
    monkeypatch.setattr(engines, "set_toml_keys", _fail_on_config(apply_env.cfg_p))
    with pytest.raises(RuntimeError):
        apply_selection(engine_by_id("claude-cli"), "newm", "", engine_changed=True)
    assert apply_env.mdl_p.read_bytes() == crlf   # byte-exact, CRLF not LF-ified


def test_apply_env_rewrite_when_set_and_differs(apply_env, monkeypatch):
    apply_env.cfg_p.write_text(_CONFIG_SEED, encoding="utf-8")
    monkeypatch.setenv("LLM_BACKEND", "mock")
    apply_selection(engine_by_id("claude-vscode"), "opus", "", engine_changed=True)
    assert os.environ["LLM_BACKEND"] == "claude"


def test_apply_env_untouched_when_unset(apply_env, monkeypatch):
    apply_env.cfg_p.write_text(_CONFIG_SEED, encoding="utf-8")
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    apply_selection(engine_by_id("claude-vscode"), "opus", "", engine_changed=True)
    assert "LLM_BACKEND" not in os.environ


# --------------------------------------------------------------------------- #
# i18n resolution for catalog keys (reviewer P2-1)                            #
# --------------------------------------------------------------------------- #

def test_engine_label_keys_and_dialog_keys_resolve():
    from common.paths import i18n_dir
    cats = {}
    for lang in ("en", "ja"):
        with open(i18n_dir() / f"{lang}.toml", "rb") as f:
            cats[lang] = tomllib.load(f)
    keys = [e.label_key for e in ENGINES] + [
        "menu.settings.backend_selector",
        "backend.dialog.title",
        "backend.dialog.engine",
        "backend.dialog.model",
        "backend.dialog.provider",
        "backend.dialog.apply",
        "backend.dialog.test",
        "backend.dialog.testing",
        "backend.dialog.busy_warning",
        "backend.dialog.env_warning",
        "backend.dialog.env_bin_warning",
        "backend.dialog.test_ok",
        "backend.dialog.test_fail",
        "backend.dialog.choice_add",
        "backend.dialog.choice_add_tip",
        "backend.dialog.choice_remove",
        "backend.dialog.choice_remove_tip",
        "backend.dialog.choice_failed",
        "backend.dialog.session_title",
        "backend.dialog.follow_default",
        "menu.settings.backend_status",
        "backend.status.title",
        "backend.status.drivers",
        "backend.status.col.driver",
        "backend.status.col.prereq",
        "backend.status.col.install",
        "backend.status.col.auth",
        "backend.status.col.action",
        "backend.status.refresh",
        "backend.status.btn.install",
        "backend.status.btn.login",
        "backend.status.btn.ping",
        "backend.status.install_done",
        "backend.status.login_started",
        "backend.status.login_manual",
        "backend.status.ping_started",
        "backend.status.ping_ok",
        "backend.status.ping_fail",
        "backend.status.note.vscode_ext",
        "backend.applied",
        "backend.applied_config_only",
    ]
    for lang, cat in cats.items():
        for k in keys:
            assert k in cat, f"{k} missing from {lang}.toml"


# --------------------------------------------------------------------------- #
# user-editable dropdown choices (models.toml [<key>].{model,provider}_choices) #
# --------------------------------------------------------------------------- #

_PI = engine_by_id("pi")


def test_pi_provider_seed_is_openai_and_local_only():
    """pi は OpenAI とローカルモデルだけ。他プロバイダは別課金なので出さない。"""
    assert _PI.provider_suggestions == ("openai-codex", "openai", "llama.cpp")


def test_combo_choices_falls_back_to_seed_when_unset(apply_env):
    assert combo_choices(_PI, "model") == _PI.model_suggestions
    assert combo_choices(_PI, "provider") == _PI.provider_suggestions
    assert saved_choices(_PI, "model") is None


def test_saved_choices_overrides_seed(apply_env):
    apply_env.mc.data = {"pi": {"model_choices": ["qwen3-coder", "gpt-5.5"]}}
    assert combo_choices(_PI, "model") == ("qwen3-coder", "gpt-5.5")
    # provider untouched → still the seed
    assert combo_choices(_PI, "provider") == _PI.provider_suggestions


def test_empty_saved_list_is_honoured_not_reseeded(apply_env):
    """[] means 'the user removed everything' and must not resurrect the seed."""
    apply_env.mc.data = {"pi": {"model_choices": []}}
    assert saved_choices(_PI, "model") == ()
    assert combo_choices(_PI, "model") == ()


def test_saved_choices_ignores_non_list_and_blank_entries(apply_env):
    apply_env.mc.data = {"pi": {"model_choices": "not-a-list"}}
    assert saved_choices(_PI, "model") is None
    apply_env.mc.data = {"pi": {"model_choices": ["  a  ", "", "a", 7, "b"]}}
    assert saved_choices(_PI, "model") == ("a", "b")   # trimmed, deduped, ints dropped


def test_save_choices_writes_models_toml_and_clears_cache(apply_env):
    save_choices(_PI, "model", ["gpt-5.6-sol", " gpt-5.5 ", "gpt-5.6-sol", ""])
    data = tomllib.loads(apply_env.mdl_p.read_text(encoding="utf-8"))
    assert data["pi"]["model_choices"] == ["gpt-5.6-sol", "gpt-5.5"]
    assert apply_env.mc.cleared == 1


def test_save_choices_provider_uses_its_own_key(apply_env):
    save_choices(_PI, "provider", ["llama.cpp"])
    data = tomllib.loads(apply_env.mdl_p.read_text(encoding="utf-8"))
    assert data["pi"]["provider_choices"] == ["llama.cpp"]
    assert "model_choices" not in data["pi"]


def test_save_choices_preserves_existing_model_key(apply_env):
    apply_env.mdl_p.write_text('[pi]\nmodel = "gpt-5.5"   # keep\n', encoding="utf-8")
    save_choices(_PI, "model", ["a"])
    text = apply_env.mdl_p.read_text(encoding="utf-8")
    assert "# keep" in text
    data = tomllib.loads(text)
    assert data["pi"]["model"] == "gpt-5.5"
    assert data["pi"]["model_choices"] == ["a"]


def test_save_choices_noop_for_engine_without_settings_key(apply_env):
    save_choices(engine_by_id("mock"), "model", ["x"])
    assert not apply_env.mdl_p.exists()


def test_save_choices_ignores_unknown_field(apply_env):
    save_choices(_PI, "bogus", ["x"])
    assert not apply_env.mdl_p.exists()


# --------------------------------------------------------------------------- #
# session_settings — セッション個別のエンジン上書き用                          #
# --------------------------------------------------------------------------- #

def _base(monkeypatch, merged):
    """そのエンジンの現在設定 (models.toml ⊕ config.toml) を merged に固定する。"""
    monkeypatch.setattr(engines, "merged_settings", lambda k, b: dict(merged))
    monkeypatch.setattr(engines, "backend_config", _cfg({}))


def test_session_settings_forces_empty_bin_for_vscode(monkeypatch):
    """全体が claude-cli でも、セッションが vscode 指定なら bin="" を強制する。
    ここを取り違えると UI は「VS Code 同梱」と言いながら PATH の CLI が走る。"""
    _base(monkeypatch, {"model": "opus", "bin": "claude"})
    s = session_settings(engine_by_id("claude-vscode"), "", "")
    assert s["bin"] == ""


def test_session_settings_forces_named_bin_for_cli(monkeypatch):
    _base(monkeypatch, {"model": "opus", "bin": ""})
    s = session_settings(engine_by_id("claude-cli"), "", "")
    assert s["bin"] == "claude"


def test_session_settings_preserves_hand_set_absolute_bin(monkeypatch):
    """手設定の絶対パスは真偽が一致するので温存する（engine_changed=True だと潰れる）。"""
    _base(monkeypatch, {"model": "opus", "bin": "/custom/claude"})
    s = session_settings(engine_by_id("claude-cli"), "", "")
    assert s["bin"] == "/custom/claude"


def test_session_settings_leaves_empty_bin_alone_for_vscode(monkeypatch):
    _base(monkeypatch, {"model": "opus", "bin": ""})
    s = session_settings(engine_by_id("claude-vscode"), "", "")
    assert s["bin"] == ""


def test_session_settings_is_independent_of_the_global_engine(monkeypatch):
    """同じ上書きは「全体が何か」に関わらず同じ設定に解決されること（非決定性の回帰）。"""
    e = engine_by_id("claude-vscode")
    _base(monkeypatch, {"model": "opus", "bin": "claude"})     # 全体 = cli
    a = session_settings(e, "sonnet", "")
    _base(monkeypatch, {"model": "opus", "bin": ""})           # 全体 = vscode
    b = session_settings(e, "sonnet", "")
    assert a == b


def test_session_settings_keeps_other_knobs(monkeypatch):
    """thinking/effort 等は全体設定由来のまま残す（落とさない）。"""
    _base(monkeypatch, {"model": "opus", "thinking": "enabled", "effort": "xhigh"})
    s = session_settings(engine_by_id("claude-vscode"), "sonnet", "")
    assert s["thinking"] == "enabled" and s["effort"] == "xhigh"
    assert s["model"] == "sonnet"


def test_session_settings_empty_model_falls_back_to_engine_default(monkeypatch):
    """「エンジンだけ変えてモデルは既定」が書けること。"""
    _base(monkeypatch, {"model": "opus"})
    assert session_settings(engine_by_id("claude-vscode"), "", "")["model"] == "opus"
    assert session_settings(engine_by_id("claude-vscode"), "   ", "")["model"] == "opus"


def test_session_settings_empty_provider_falls_back(monkeypatch):
    _base(monkeypatch, {"model": "gpt-5.5", "provider": "openai-codex"})
    s = session_settings(engine_by_id("pi"), "", "")
    assert s["provider"] == "openai-codex"


def test_session_settings_overrides_provider_for_pi(monkeypatch):
    _base(monkeypatch, {"model": "gpt-5.5", "provider": "openai-codex"})
    s = session_settings(engine_by_id("pi"), "qwen3-coder", "llama.cpp")
    assert s["provider"] == "llama.cpp" and s["model"] == "qwen3-coder"


def test_session_settings_ignores_provider_for_engines_without_the_field(monkeypatch):
    _base(monkeypatch, {"model": "opus"})
    s = session_settings(engine_by_id("claude-vscode"), "", "llama.cpp")
    assert "provider" not in s


def test_session_settings_untouched_for_empty_config_patch(monkeypatch):
    """非空 config_patch を持つのは claude 2 種だけ。他は bin を生やさない。"""
    _base(monkeypatch, {"model": "gpt-5.5"})
    for eid in ("pi", "codex", "openai-http"):
        assert "bin" not in session_settings(engine_by_id(eid), "", "")


def test_session_settings_mock_has_no_settings_key():
    assert session_settings(engine_by_id("mock"), "m", "") == {"model": "m"}
