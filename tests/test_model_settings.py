"""Tests for per-backend model settings (llm_backend/models.toml).

Covers merged_settings() overlay precedence, the claude flag mapping
(_generation_flags), and openai model resolution — all without touching the
filesystem or spawning a real engine.
"""

from __future__ import annotations

import pytest

import llm_backend
import llm_backend.model_settings as ms
from llm_backend.claude_code import _generation_flags


@pytest.fixture
def fake_models(monkeypatch):
    """Replace model_config() with an in-memory dict (no models.toml file)."""

    def _set(data: dict) -> None:
        monkeypatch.setattr(ms, "model_config", lambda: data)
        # __init__.py imported merged_settings (which reads ms.model_config at
        # call time), so patching the function object on the module suffices.

    return _set


# ---------------------------------------------------------------------------
# merged_settings — overlay precedence
# ---------------------------------------------------------------------------


class TestMergedSettings:
    def test_models_toml_overrides_config_toml(self, fake_models):
        fake_models({"claude_code": {"model": "sonnet"}})
        out = ms.merged_settings("claude_code", {"model": "opus", "bin": "/x"})
        assert out["model"] == "sonnet"
        assert out["bin"] == "/x"  # operational key from config.toml preserved

    def test_empty_string_does_not_clobber_config_toml(self, fake_models):
        fake_models({"claude_code": {"model": "   "}})
        out = ms.merged_settings("claude_code", {"model": "opus"})
        assert out["model"] == "opus"  # blank placeholder ignored

    def test_missing_section_falls_back_to_config_toml(self, fake_models):
        fake_models({})
        out = ms.merged_settings("claude_code", {"model": "opus"})
        assert out["model"] == "opus"

    def test_bool_false_overrides(self, fake_models):
        # `thinking = false` must win over config.toml, not be dropped as falsy.
        fake_models({"claude_code": {"thinking": False}})
        out = ms.merged_settings("claude_code", {"thinking": "enabled"})
        assert out["thinking"] is False

    def test_base_is_not_mutated(self, fake_models):
        fake_models({"pi": {"model": "x"}})
        base = {"model": "y"}
        ms.merged_settings("pi", base)
        assert base == {"model": "y"}

    def test_string_values_stripped(self, fake_models):
        fake_models({"pi": {"model": "  gpt  "}})
        out = ms.merged_settings("pi", {})
        assert out["model"] == "gpt"


# ---------------------------------------------------------------------------
# _generation_flags — claude CLI mapping
# ---------------------------------------------------------------------------


class TestGenerationFlags:
    def test_empty(self):
        assert _generation_flags({}) == []

    def test_model_only(self):
        assert _generation_flags({"model": "opus"}) == ["--model", "opus"]

    def test_thinking_valid(self):
        assert _generation_flags({"thinking": "adaptive"}) == [
            "--thinking", "adaptive",
        ]

    def test_thinking_bool_true(self):
        assert _generation_flags({"thinking": True}) == ["--thinking", "enabled"]

    def test_thinking_bool_false(self):
        assert _generation_flags({"thinking": False}) == ["--thinking", "disabled"]

    def test_thinking_invalid_skipped(self):
        assert _generation_flags({"thinking": "bogus"}) == []

    def test_effort_plain(self):
        assert _generation_flags({"effort": "high"}) == ["--effort", "high"]

    def test_effort_ultracode(self):
        # `--settings` は _settings_payload が hooks とまとめて 1 回だけ渡す（Issue #96）。
        # _generation_flags は effort の写像だけを担う。
        assert _generation_flags({"effort": "ultracode"}) == ["--effort", "xhigh"]


class TestSettingsPayload:
    """`--settings` は 1 個しか渡せないので ultracode と hooks を必ず 1 dict にまとめる。"""

    def test_ultracode_and_hooks_merged(self):
        from llm_backend.claude_code import _settings_payload
        p = _settings_payload({"effort": "ultracode"})
        assert p["ultracode"] is True
        assert "PreToolUse" in p["hooks"]

    def test_guard_hook_present_by_default(self):
        from llm_backend.claude_code import _settings_payload
        p = _settings_payload({})
        assert "ultracode" not in p
        hook = p["hooks"]["PreToolUse"][0]
        assert "Write" in hook["matcher"] and "Edit" in hook["matcher"]
        assert "guard-write" in hook["hooks"][0]["command"]

    def test_guard_hook_can_be_disabled(self):
        from llm_backend.claude_code import _settings_payload
        assert _settings_payload({"guard_mount_writes": False}) == {}

    def test_payload_is_json_serialisable(self):
        import json

        from llm_backend.claude_code import _settings_payload
        json.dumps(_settings_payload({"effort": "ultracode"}))

    def test_effort_blank_skipped(self):
        assert _generation_flags({"effort": "   "}) == []

    def test_all_combined(self):
        flags = _generation_flags(
            {"model": "sonnet", "thinking": "enabled", "effort": "max"}
        )
        assert flags == [
            "--model", "sonnet",
            "--thinking", "enabled",
            "--effort", "max",
        ]


# ---------------------------------------------------------------------------
# openai model resolution (registry factory)
# ---------------------------------------------------------------------------


class TestOpenAIResolution:
    def test_models_toml_wins(self, fake_models, monkeypatch):
        fake_models({"openai-compat": {"model": "from-toml"}})
        monkeypatch.setattr(llm_backend, "backend_config", lambda: {})
        monkeypatch.setenv("OPENAI_MODEL", "from-env")
        backend = llm_backend._make_openai()
        assert backend.model == "from-toml"

    def test_env_fallback(self, fake_models, monkeypatch):
        fake_models({})
        monkeypatch.setattr(llm_backend, "backend_config", lambda: {})
        monkeypatch.setenv("OPENAI_MODEL", "from-env")
        backend = llm_backend._make_openai()
        assert backend.model == "from-env"

    def test_default_when_unset(self, fake_models, monkeypatch):
        fake_models({})
        monkeypatch.setattr(llm_backend, "backend_config", lambda: {})
        monkeypatch.delenv("OPENAI_MODEL", raising=False)
        backend = llm_backend._make_openai()
        assert backend.model == "gpt-4o-mini"


def test_model_config_corrupt_returns_empty(tmp_path, monkeypatch):
    # Corrupt (hand-edited) models.toml must not crash at read time.
    (tmp_path / "models.toml").write_text("[[[not valid toml", encoding="utf-8")
    monkeypatch.setattr(ms, "repo_root", lambda: tmp_path)
    ms.model_config.cache_clear()
    try:
        assert ms.model_config() == {}
    finally:
        ms.model_config.cache_clear()
