"""Declarative engine catalog + apply (Qt-independent).

An *engine* is the user-facing "利用方法" choice in the backend/model dialog. It
maps onto existing knobs: a backend registry key (claude/pi/openai/mock) plus the
one operational knob that distinguishes same-backend variants — claude's
``[claude_code].bin`` (``""`` = VS Code bundled engine, ``"claude"`` = PATH CLI).
The "model" is free text (no catalog), so the dialog offers an editable combo.

Adding a future engine (e.g. codex CLI) = a backend module + a ``_BACKENDS`` entry
+ one ``Engine`` here; the dialog needs no change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from common.paths import atomic_write_text
from llm_backend import backend_config
from llm_backend.model_settings import merged_settings, model_config
from llm_backend.settings_store import (
    config_toml_path,
    models_toml_path,
    set_toml_keys,
)


@dataclass(frozen=True)
class Engine:
    id: str
    label_key: str
    backend_key: str      # "claude"|"pi"|"openai"|"mock" (_BACKENDS key)
    settings_key: str     # models.toml section; "" = no model settings
    config_patch: tuple[tuple[str, str | bool], ...]  # written to config.toml [settings_key] on switch
    fields: tuple[str, ...]                            # dialog knobs ("model", "provider")
    model_suggestions: tuple[str, ...] = ()
    provider_suggestions: tuple[str, ...] = ()


ENGINES: tuple[Engine, ...] = (
    Engine(
        id="claude-vscode",
        label_key="backend.engine.claude_vscode",
        backend_key="claude",
        settings_key="claude_code",
        config_patch=(("bin", ""),),
        fields=("model",),
    ),
    Engine(
        id="claude-cli",
        label_key="backend.engine.claude_cli",
        backend_key="claude",
        settings_key="claude_code",
        config_patch=(("bin", "claude"),),
        fields=("model",),
    ),
    Engine(
        id="pi",
        label_key="backend.engine.pi",
        backend_key="pi",
        settings_key="pi",
        config_patch=(),
        fields=("model", "provider"),
        provider_suggestions=("", "openai-codex"),
    ),
    Engine(
        id="openai-http",
        label_key="backend.engine.openai_http",
        backend_key="openai",
        settings_key="openai-compat",
        config_patch=(),
        fields=("model",),
    ),
    Engine(
        id="mock",
        label_key="backend.engine.mock",
        backend_key="mock",
        settings_key="",
        config_patch=(),
        fields=(),
    ),
)


def engine_by_id(engine_id: str) -> Engine | None:
    for e in ENGINES:
        if e.id == engine_id:
            return e
    return None


def _resolved_backend_name() -> str:
    """Backend key with get_backend's precedence (env → config → OPENAI_BASE_URL)."""
    name = os.environ.get("LLM_BACKEND")
    if not name:
        name = backend_config().get("backend", {}).get("name")
    if not name:
        base_url = os.environ.get("OPENAI_BASE_URL") or ""
        name = "mock" if base_url.strip().lower() == "mock" else "openai"
    return name


def current_engine_id() -> str:
    """Resolve the currently-configured engine id (matches get_backend selection).

    claude splits on ``[claude_code].bin`` truthiness (empty/unset = claude-vscode,
    non-empty = claude-cli). Unknown backend keys map to ``openai-http``.
    """
    name = _resolved_backend_name()
    if name == "claude":
        bin_val = backend_config().get("claude_code", {}).get("bin", "")
        return "claude-cli" if str(bin_val).strip() else "claude-vscode"
    for e in ENGINES:
        if e.backend_key == name:
            return e.id
    return "openai-http"


def current_model(engine: Engine) -> str:
    if not engine.settings_key:
        return ""
    merged = merged_settings(
        engine.settings_key, backend_config().get(engine.settings_key, {})
    )
    return merged.get("model", "") or ""


def current_provider(engine: Engine) -> str:
    if not engine.settings_key:
        return ""
    merged = merged_settings(
        engine.settings_key, backend_config().get(engine.settings_key, {})
    )
    return merged.get("provider", "") or ""


def candidate_settings(
    engine: Engine, model: str, provider: str, *, engine_changed: bool
) -> dict:
    """Settings dict for a *candidate* backend (for the connectivity check).

    Starts from the whole current merged settings (so thinking/effort/etc. are not
    dropped), applies ``config_patch`` ONLY when ``engine_changed`` (matching
    ``apply_selection`` exactly — so a model-only change keeps a hand-set custom
    ``bin``), then overrides model/provider. Never touches global config/caches.
    """
    if not engine.settings_key:
        return {"model": model}
    base = merged_settings(
        engine.settings_key, backend_config().get(engine.settings_key, {})
    )
    settings = dict(base)
    if engine_changed:
        for key, value in engine.config_patch:
            settings[key] = value
    if "model" in engine.fields:
        settings["model"] = model
    if "provider" in engine.fields:
        settings["provider"] = provider
    return settings


def apply_selection(
    engine: Engine, model: str, provider: str, *, engine_changed: bool
) -> None:
    """Persist the selection to the truth sources and refresh caches (no restart).

    All-or-nothing across the two files: if config.toml write fails, models.toml is
    rolled back to its pre-apply state, so the "old engine + new model" middle state
    (which the same ``settings_key`` for claude-vscode ⇔ claude-cli makes reachable)
    never survives on disk.
    """
    models_path = models_toml_path()
    config_path = config_toml_path()

    # 1. pre-apply snapshot of models.toml.
    try:
        models_before: str | None = models_path.read_text(encoding="utf-8")
        models_existed = True
    except FileNotFoundError:
        models_before = None
        models_existed = False

    # 2. models.toml (model/provider knobs). May create the file.
    if engine.settings_key:
        model_changes: dict[str, str | bool] = {"model": model}
        if "provider" in engine.fields:
            model_changes["provider"] = provider
        set_toml_keys(models_path, {engine.settings_key: model_changes})

    # 3. config.toml: [backend].name always (idempotent) + config_patch (only on
    #    engine change) + empty-value clear rule, in one write.
    config_changes: dict[str, dict[str, str | bool]] = {
        "backend": {"name": engine.backend_key}
    }
    sec: dict[str, str | bool] = {}
    if engine_changed:
        for key, value in engine.config_patch:
            sec[key] = value
    # Empty model/provider means "reset to backend/CLI default": models.toml gets
    # "" (skipped by merged_settings → falls back to default). If config.toml holds
    # a non-empty active value for that key it would keep winning, so clear it too.
    for field in engine.fields:
        val = model if field == "model" else provider
        if not val.strip():
            existing = backend_config().get(engine.settings_key, {}).get(field)
            if isinstance(existing, str) and existing.strip():
                sec[field] = ""
    if sec:
        config_changes[engine.settings_key] = sec

    try:
        set_toml_keys(config_path, config_changes)
    except Exception as e:
        # Roll models.toml back to fully-old so no "old engine + new model" remains.
        if engine.settings_key:
            try:
                if models_existed:
                    atomic_write_text(models_path, models_before, newline="")
                else:
                    models_path.unlink(missing_ok=True)
            except Exception as rb:
                raise RuntimeError(
                    "backend settings apply failed and rolling models.toml back "
                    f"also failed ({rb}); verify models.toml manually"
                ) from e
        raise

    # 4. refresh caches so the new values are live without a restart.
    backend_config.cache_clear()
    model_config.cache_clear()

    # 5. env LLM_BACKEND, if set, wins over config.toml → keep it in sync in-process.
    cur_env = os.environ.get("LLM_BACKEND")
    if cur_env and cur_env != engine.backend_key:
        os.environ["LLM_BACKEND"] = engine.backend_key
