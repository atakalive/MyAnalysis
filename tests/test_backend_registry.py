"""Tests for build_backend + settings injection in llm_backend.__init__."""
import llm_backend
from llm_backend import build_backend, get_backend


def test_build_backend_exported():
    assert "build_backend" in llm_backend.__all__


def test_build_mock_no_settings():
    b = build_backend("mock")
    assert b.name == "mock"


def test_build_mock_model_injected():
    b = build_backend("mock", {"model": "injected-mock"})
    assert b.model == "injected-mock"


def test_build_claude_settings_land_in_config():
    settings = {
        "model": "sonnet",
        "thinking": "enabled",
        "effort": "xhigh",
        "bin": "claude",
    }
    b = build_backend("claude", settings)
    assert b._config["model"] == "sonnet"
    assert b._config["thinking"] == "enabled"
    assert b._config["effort"] == "xhigh"
    assert b._config["bin"] == "claude"
    assert b.model == "sonnet"


def test_build_unknown_raises():
    import pytest

    with pytest.raises(RuntimeError):
        build_backend("nope")


def test_default_backend_name(monkeypatch):
    from llm_backend import default_backend_name

    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    assert default_backend_name() == "openai"
    monkeypatch.setenv("OPENAI_BASE_URL", " MOCK ")
    assert default_backend_name() == "mock"
    monkeypatch.setenv("OPENAI_BASE_URL", "http://x")
    assert default_backend_name() == "openai"


def test_get_backend_env_precedence(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "mock")
    b = get_backend()
    assert b.name == "mock"


def test_backend_config_corrupt_returns_empty(tmp_path, monkeypatch):
    # reviewer code P2: corrupt (hand-edited) config.toml must not crash at read time
    # — return {} so the dialog can still open and self-heal on apply.
    d = tmp_path / "llm_backend"
    d.mkdir()
    (d / "config.toml").write_text("[[[not valid toml", encoding="utf-8")
    monkeypatch.setattr(llm_backend, "repo_root", lambda: tmp_path)
    llm_backend.backend_config.cache_clear()
    try:
        assert llm_backend.backend_config() == {}
    finally:
        llm_backend.backend_config.cache_clear()
