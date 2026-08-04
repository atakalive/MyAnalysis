"""Declarative engine catalog + apply (Qt-independent).

An *engine* is the user-facing "利用方法" choice in the backend/model dialog. It
maps onto existing knobs: a backend registry key (claude/pi/openai/mock) plus the
one operational knob that distinguishes same-backend variants — claude's
``[claude_code].bin`` (``""`` = VS Code bundled engine, ``"claude"`` = PATH CLI).
The "model" is free text (no catalog), so the dialog offers an editable combo.

Adding a future engine = a backend module + a ``_BACKENDS`` entry
+ one ``Engine`` here; the dialog needs no change (codex was added exactly this way).
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
        # OpenAI (Codex サブスク / API キー) とローカルモデルのみ。他プロバイダは pi
        # 経由だと別課金になるので候補に出さない。"llama.cpp" は pi 側の provider id
        # そのもの (`/login llama.cpp`, LLAMA_BASE_URL)。
        provider_suggestions=("openai-codex", "openai", "llama.cpp"),
        # openai-codex の実在 ID (pi --list-models で確認)。ローカルモデルは
        # llama-server にロード済みのものしか catalog に出ないため静的な種は持てない
        # ＝ダイアログの追加/削除で models.toml に貯める運用が本筋。
        model_suggestions=(
            "gpt-5.6-sol",
            "gpt-5.6-luna",
            "gpt-5.6-terra",
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.4-mini",
        ),
    ),
    Engine(
        id="codex",
        label_key="backend.engine.codex",
        backend_key="codex",
        settings_key="codex",
        config_patch=(),
        fields=("model",),
        # ChatGPT サブスク側の Codex カタログ (pi の openai-codex provider と同じ
        # モデル群)。空欄 = codex 既定モデル。
        model_suggestions=(
            "gpt-5.6-sol",
            "gpt-5.6-luna",
            "gpt-5.6-terra",
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.4-mini",
        ),
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


def engine_label(engine: Engine) -> str:
    """Localised display name for ``engine``.

    Lives here rather than at the call sites because ``label_key`` is an attribute,
    and ``tests/test_i18n_catalog.py`` forbids non-literal ``tr()`` keys in the GUI
    files it scans (``gui/chat.py`` etc.). Keeping the lookup next to the catalog
    that owns the key is also simply where it belongs; the label keys themselves are
    covered by ``tests/test_engines.py::test_engine_label_keys_and_dialog_keys_resolve``.
    """
    from common.i18n import tr
    return tr(engine.label_key)


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


# ----- user-editable dropdown choices (persisted in models.toml) -----
#
# ``model_suggestions``/``provider_suggestions`` above are only the *seed*. The
# dialog lets the user add/remove entries, and the result is persisted to
# ``models.toml`` as ``[<settings_key>].model_choices`` / ``provider_choices``.
# Storing them there (rather than a separate file) keeps models.toml the single
# truth source for model settings; ``merged_settings`` overlays only bool and
# non-blank str, so a list value is ignored there and never reaches a backend.

_CHOICE_KEY = {"model": "model_choices", "provider": "provider_choices"}


def _clean(values) -> list[str]:
    """Trim, drop blanks, de-duplicate — preserving order."""
    out: list[str] = []
    for v in values:
        v = (v or "").strip()
        if v and v not in out:
            out.append(v)
    return out


def saved_choices(engine: Engine, field: str) -> tuple[str, ...] | None:
    """Persisted choices for ``field``; ``None`` when never customised.

    ``None`` (key absent) and ``()`` (user removed everything) are deliberately
    distinct: the former falls back to the seed, the latter is an empty list the
    user asked for and must not resurrect the seed.
    """
    key = _CHOICE_KEY.get(field)
    if key is None or not engine.settings_key:
        return None
    section = model_config().get(engine.settings_key)
    if not isinstance(section, dict):
        return None
    raw = section.get(key)
    if not isinstance(raw, list):
        return None
    return tuple(_clean(v for v in raw if isinstance(v, str)))


def seed_choices(engine: Engine, field: str) -> tuple[str, ...]:
    if field == "model":
        return engine.model_suggestions
    if field == "provider":
        return engine.provider_suggestions
    return ()


def combo_choices(engine: Engine, field: str) -> tuple[str, ...]:
    """What the dialog dropdown offers (excluding the current value)."""
    saved = saved_choices(engine, field)
    return saved if saved is not None else seed_choices(engine, field)


def save_choices(engine: Engine, field: str, values) -> None:
    """Persist ``values`` as the choice list for ``field`` and refresh the cache."""
    key = _CHOICE_KEY.get(field)
    if key is None or not engine.settings_key:
        return
    set_toml_keys(models_toml_path(), {engine.settings_key: {key: _clean(values)}})
    model_config.cache_clear()


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
    settings = _base_settings(engine)
    if engine_changed:
        for key, value in engine.config_patch:
            settings[key] = value
    if "model" in engine.fields:
        settings["model"] = model
    if "provider" in engine.fields:
        settings["provider"] = provider
    return settings


def _base_settings(engine: Engine) -> dict:
    """models.toml を config.toml セクションに重ねた、そのエンジンの現在の設定一式。"""
    return dict(merged_settings(
        engine.settings_key, backend_config().get(engine.settings_key, {})
    ))


def session_settings(engine: Engine, model: str, provider: str) -> dict:
    """Settings dict for a per-chat-session engine override.

    ``candidate_settings`` の ``engine_changed`` はここでは使えない。``False`` だと base の
    ``bin`` を引き継ぐので「セッションは claude-vscode 指定なのに全体が claude-cli だから
    PATH の CLI が走る」になり、``True`` だと手設定の絶対パス ``bin`` を潰す。さらに
    「全体が今どれか」で同じ上書きの解決結果が変わってしまい非決定的になる。

    代わりに ``config_patch`` を **base と真偽が食い違うときだけ** 当てる。これは
    ``current_engine_id`` が claude-vscode / claude-cli を判別している基準（``bin`` の真偽）
    そのものなので、解決側と構築側が構造的に一致する。claude-vscode には ``bin=""`` を強制し、
    claude-cli には base が空のときだけ ``"claude"`` を入れ、ユーザーの絶対パス ``bin`` は温存する。
    （``config_patch`` の値が真偽の判別子であることが前提。現行カタログで非空の
    ``config_patch`` を持つのは claude 2 種だけで、そこで成立している。）

    ``model``/``provider`` が空ならそのエンジンの設定済み既定を使う（base 由来のまま残す）ので、
    「エンジンだけ変えてモデルは既定」が自然に書ける。
    """
    if not engine.settings_key:
        return {"model": model}
    base = _base_settings(engine)
    settings = dict(base)
    for key, value in engine.config_patch:
        if bool(str(base.get(key, "") or "").strip()) != bool(value):
            settings[key] = value
    if "model" in engine.fields and model.strip():
        settings["model"] = model.strip()
    if "provider" in engine.fields and provider.strip():
        settings["provider"] = provider.strip()
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

    # 1. pre-apply snapshot of models.toml. newline="" keeps CRLF verbatim (Path.
    #    read_text would collapse CRLF→LF via universal newlines) so the rollback
    #    below is byte-exact — matching settings_store's CRLF-preserving contract,
    #    which is the whole reason atomic_write_text grew a newline parameter.
    try:
        with open(models_path, encoding="utf-8", newline="") as f:
            models_before: str | None = f.read()
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
