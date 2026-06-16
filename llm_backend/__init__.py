"""LLM backend abstraction + registry.

Backends implement the LLMBackend Protocol (see base.py). get_backend()
selects one by name from env / config.toml. To add a backend: implement
LLMBackend in a new module and register it in _BACKENDS.
"""

from __future__ import annotations

import functools
import os
import tomllib
from collections.abc import Callable

from common.paths import repo_root
from llm_backend.base import LLMBackend, Message, TextDelta, ToolCallRequest
from llm_backend.model_settings import merged_settings

__all__ = [
    "TextDelta",
    "ToolCallRequest",
    "Message",
    "LLMBackend",
    "get_backend",
    "backend_config",
]


@functools.lru_cache(maxsize=1)
def backend_config() -> dict:
    """Load llm_backend/config.toml once. Process must restart to pick up edits."""
    p = repo_root() / "llm_backend" / "config.toml"
    try:
        with open(p, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def _make_openai() -> LLMBackend:
    from llm_backend.openai_compat import OpenAICompatBackend

    settings = merged_settings("openai", backend_config().get("openai", {}))
    # models.toml [openai].model is canonical; OPENAI_MODEL env is a back-compat
    # fallback. base_url / api_key are endpoint/secret → stay in env.
    model = settings.get("model") or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
    base_url = os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    return OpenAICompatBackend(
        base_url=base_url,
        api_key=os.environ.get("OPENAI_API_KEY") or "not-needed",
        model=model,
    )


def _make_mock() -> LLMBackend:
    from llm_backend.mock import MockBackend

    return MockBackend(model=os.environ.get("OPENAI_MODEL") or "mock-omni")


def _make_pi() -> LLMBackend:
    from llm_backend.pi import PiCodingAgentBackend

    return PiCodingAgentBackend(
        merged_settings("pi", backend_config().get("pi", {}))
    )


def _make_claude() -> LLMBackend:
    from llm_backend.claude_code import ClaudeCodeBackend

    return ClaudeCodeBackend(
        merged_settings("claude_code", backend_config().get("claude_code", {}))
    )


_BACKENDS: dict[str, Callable[[], LLMBackend]] = {
    "openai": _make_openai,
    "mock": _make_mock,
    "pi": _make_pi,
    "claude": _make_claude,
}


def get_backend() -> LLMBackend:
    """Construct the configured backend.

    Selection order:
      1. env LLM_BACKEND
      2. config.toml [backend].name
      3. OPENAI_BASE_URL back-compat ("mock" → mock, else openai)
    """
    name = os.environ.get("LLM_BACKEND")
    if not name:
        name = backend_config().get("backend", {}).get("name")
    if not name:
        base_url = os.environ.get("OPENAI_BASE_URL") or ""
        name = "mock" if base_url.strip().lower() == "mock" else "openai"
    factory = _BACKENDS.get(name)
    if factory is None:
        raise RuntimeError(
            f"unknown backend: {name!r}. Known: {sorted(_BACKENDS)}"
        )
    return factory()
