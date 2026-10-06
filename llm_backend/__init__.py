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
from llm_backend.unconfigured import NoEngineConfigured

__all__ = [
    "TextDelta",
    "ToolCallRequest",
    "Message",
    "LLMBackend",
    "get_backend",
    "build_backend",
    "backend_config",
    "default_backend_name",
    "resolve_backend_name",
    "NoEngineConfigured",
]


@functools.lru_cache(maxsize=1)
def backend_config() -> dict:
    """Load llm_backend/config.toml once.

    Cached; pick up edits via ``backend_config.cache_clear()`` (the Settings →
    Backend / model settings dialog does this on apply) or a process restart.
    """
    p = repo_root() / "llm_backend" / "config.toml"
    try:
        with open(p, "rb") as f:
            return tomllib.load(f)
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        # Corrupt (hand-edited) TOML must not crash the app at read time — return
        # {} so the app falls back to defaults and the Settings → Backend / model settings dialog
        # can still open and self-heal it (settings_store .bak + regenerate).
        return {}


def _make_openai(settings: dict | None = None) -> LLMBackend:
    from llm_backend.openai_compat import OpenAICompatBackend

    if settings is None:
        settings = merged_settings(
            "openai-compat", backend_config().get("openai-compat", {})
        )
    # models.toml [openai-compat].model is canonical; OPENAI_MODEL env is a
    # back-compat fallback. base_url / api_key are endpoint/secret → stay in env.
    model = settings.get("model") or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
    base_url = os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    return OpenAICompatBackend(
        base_url=base_url,
        api_key=os.environ.get("OPENAI_API_KEY") or "not-needed",
        model=model,
    )


def _make_mock(settings: dict | None = None) -> LLMBackend:
    from llm_backend.mock import MockBackend

    model = (settings or {}).get("model") or os.environ.get("OPENAI_MODEL") or "mock-omni"
    return MockBackend(model=model)


def _make_pi(settings: dict | None = None) -> LLMBackend:
    from llm_backend.pi import PiCodingAgentBackend

    if settings is None:
        settings = merged_settings("pi", backend_config().get("pi", {}))
    return PiCodingAgentBackend(settings)


def _make_claude(settings: dict | None = None) -> LLMBackend:
    from llm_backend.claude_code import ClaudeCodeBackend

    if settings is None:
        settings = merged_settings(
            "claude_code", backend_config().get("claude_code", {})
        )
    return ClaudeCodeBackend(settings)


def _make_codex(settings: dict | None = None) -> LLMBackend:
    from llm_backend.codex import CodexBackend

    if settings is None:
        settings = merged_settings("codex", backend_config().get("codex", {}))
    return CodexBackend(settings)


_BACKENDS: dict[str, Callable[[dict | None], LLMBackend]] = {
    "openai": _make_openai,
    "mock": _make_mock,
    "pi": _make_pi,
    "claude": _make_claude,
    "codex": _make_codex,
}


def build_backend(name: str, settings: dict | None = None) -> LLMBackend:
    """Construct backend *name*, optionally overriding its settings dict.

    ``settings=None`` reproduces the current config-derived construction (behaviour
    unchanged). A non-None ``settings`` builds a candidate backend from the given
    settings without touching global config/caches — used by the connectivity check
    (see llm_backend.ping) to probe a not-yet-applied configuration.
    """
    factory = _BACKENDS.get(name)
    if factory is None:
        raise RuntimeError(
            f"unknown backend: {name!r}. Known: {sorted(_BACKENDS)}"
        )
    return factory(settings)


def default_backend_name() -> str | None:
    """LLM_BACKEND も [backend].name も無いときの既定（OPENAI_BASE_URL 後方互換）。

    OPENAI_BASE_URL が無い・空白だけなら None（エンジン未設定。Issue #115）。
    "mock"（大小文字・前後の空白は無視）ならモック、それ以外は OpenAI 互換。
    """
    base_url = (os.environ.get("OPENAI_BASE_URL") or "").strip()
    if not base_url:
        return None
    return "mock" if base_url.lower() == "mock" else "openai"


def resolve_backend_name(config: dict) -> str | None:
    """使うバックエンドの名前。None = エンジン未設定。

    env LLM_BACKEND → config["backend"]["name"] → default_backend_name() の順。
    空文字は「無い」扱い（strip はしない。空白だけの値は未知の名前として build_backend が
    弾く — 従来どおり）。[backend] が表でない・name が str でないときは名前無し扱い。
    config を引数で受けるのは、llm_backend.engines が自分の名前空間の backend_config を
    渡す（tests/test_engines.py がそれを差し替える）ため。
    """
    name = os.environ.get("LLM_BACKEND")
    if name:
        return name
    section = config.get("backend")
    if isinstance(section, dict):
        name = section.get("name")
        if isinstance(name, str) and name:
            return name
    return default_backend_name()


def get_backend() -> LLMBackend:
    """Construct the configured backend.

    Selection order (resolve_backend_name):
      1. env LLM_BACKEND
      2. config.toml [backend].name
      3. OPENAI_BASE_URL back-compat ("mock" → mock, else openai)
    None of them → NoEngineConfigured (Issue #115: never default to api.openai.com).
    An unknown name raises RuntimeError from build_backend.
    """
    name = resolve_backend_name(backend_config())
    if name is None:
        raise NoEngineConfigured()
    return build_backend(name)
