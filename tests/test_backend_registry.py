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


def test_get_backend_env_precedence(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "mock")
    b = get_backend()
    assert b.name == "mock"
