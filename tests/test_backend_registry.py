"""Tests for build_backend + settings injection in llm_backend.__init__."""
import pytest

import llm_backend
from llm_backend import NoEngineConfigured, build_backend, get_backend
from llm_backend.base import Message
from llm_backend.unconfigured import UnconfiguredBackend


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
    assert default_backend_name() is None
    monkeypatch.setenv("OPENAI_BASE_URL", "   ")
    assert default_backend_name() is None
    monkeypatch.setenv("OPENAI_BASE_URL", " MOCK ")
    assert default_backend_name() == "mock"
    monkeypatch.setenv("OPENAI_BASE_URL", "http://x")
    assert default_backend_name() == "openai"


def test_get_backend_env_precedence(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "mock")
    b = get_backend()
    assert b.name == "mock"


def test_backend_config_corrupt_returns_empty(tmp_path, monkeypatch):
    # Corrupt (hand-edited) config.toml must not crash at read time
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


@pytest.fixture()
def _no_config(monkeypatch, tmp_path):
    import llm_backend.model_settings as model_settings
    monkeypatch.setattr(llm_backend, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(model_settings, "repo_root", lambda: tmp_path)
    for key in ("LLM_BACKEND", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(key, raising=False)
    llm_backend.backend_config.cache_clear()
    model_settings.model_config.cache_clear()
    yield
    llm_backend.backend_config.cache_clear()
    model_settings.model_config.cache_clear()


def test_get_backend_unconfigured_raises(_no_config):
    with pytest.raises(NoEngineConfigured):
        get_backend()


def test_get_backend_base_url_backcompat(_no_config, monkeypatch):
    from llm_backend.mock import MockBackend
    from llm_backend.openai_compat import OpenAICompatBackend

    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:9/v1")
    assert isinstance(get_backend(), OpenAICompatBackend)
    monkeypatch.setenv("OPENAI_BASE_URL", "mock")
    assert isinstance(get_backend(), MockBackend)


def test_resolve_backend_name_order(monkeypatch):
    from llm_backend import resolve_backend_name

    monkeypatch.delenv("LLM_BACKEND", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    cfg = {"backend": {"name": "pi"}}
    assert resolve_backend_name(cfg) == "pi"
    monkeypatch.setenv("LLM_BACKEND", "codex")
    assert resolve_backend_name(cfg) == "codex"
    monkeypatch.delenv("LLM_BACKEND")
    assert resolve_backend_name({"backend": "claude"}) is None
    assert resolve_backend_name({"backend": {"name": ""}}) is None
    assert resolve_backend_name({"backend": {"name": 1}}) is None
    monkeypatch.setenv("OPENAI_BASE_URL", "mock")
    assert resolve_backend_name({}) == "mock"


def test_unconfigured_backend_never_streams():
    b = UnconfiguredBackend()
    assert b.name == "unconfigured"
    assert b.set_persona("x") is None
    assert b.set_chat_dataset("ds") is None
    with pytest.raises(NoEngineConfigured):
        list(UnconfiguredBackend().stream([Message(role="user", content="hi")]))


def test_exports():
    assert "resolve_backend_name" in llm_backend.__all__
    assert "NoEngineConfigured" in llm_backend.__all__
