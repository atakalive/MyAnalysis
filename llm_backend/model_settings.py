"""Per-backend model settings, loaded from ``llm_backend/models.toml``.

Model-selection knobs (which model / how much thinking / effort / provider) live
here, separate from the operational/transport config in ``config.toml`` (bin,
cwd, permission_mode, tools, ...). ``models.toml`` has one section per backend
key (``[claude_code]``, ``[pi]``, ``[openai]``, ...); each backend maps the
conventional keys (model / thinking / effort / provider) to its own CLI/API.

``merged_settings`` overlays the ``models.toml`` section onto the backend's
``config.toml`` section, so ``models.toml`` is the canonical place while any
legacy value left in ``config.toml`` keeps working as a fallback.
"""

from __future__ import annotations

import functools
import tomllib

from common.paths import repo_root


@functools.lru_cache(maxsize=1)
def model_config() -> dict:
    """Load ``llm_backend/models.toml`` once. Restart to pick up edits."""
    p = repo_root() / "llm_backend" / "models.toml"
    try:
        with open(p, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def merged_settings(backend_key: str, base: dict) -> dict:
    """Overlay ``models.toml[backend_key]`` onto ``base`` (config.toml section).

    Returns a new dict — ``base`` is never mutated. A key from ``models.toml``
    overrides ``base`` only when its value is a bool or a non-blank string; None,
    empty, and whitespace-only strings are ignored so an empty placeholder in
    ``models.toml`` does not clobber a real value in ``config.toml``. (A plain
    ``if value:`` would wrongly drop ``thinking = false``.) No key allow-list, so
    a new backend can introduce its own knobs without touching this helper.
    """
    merged = dict(base)
    section = model_config().get(backend_key)
    if isinstance(section, dict):
        for key, value in section.items():
            if isinstance(value, bool):
                merged[key] = value
            elif isinstance(value, str) and value.strip():
                merged[key] = value.strip()
    return merged
