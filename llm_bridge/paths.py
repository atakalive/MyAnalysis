"""LLM bridge specific paths. Lives parallel to common.paths but owns its own dir."""
from pathlib import Path
from common.paths import repo_root


def global_state_dir() -> Path:
    """Return data/llm_state/, creating it if missing."""
    p = repo_root() / "data" / "llm_state"
    p.mkdir(parents=True, exist_ok=True)
    return p


def commands_queue_dir() -> Path:
    """Return data/llm_state/commands/, creating it if missing."""
    p = global_state_dir() / "commands"
    p.mkdir(parents=True, exist_ok=True)
    return p


def command_log_path() -> Path:
    """Return data/llm_state/command_log.jsonl (file may not exist yet)."""
    return global_state_dir() / "command_log.jsonl"


def active_state_path() -> Path:
    """Return data/llm_state/active.json (file may not exist yet)."""
    return global_state_dir() / "active.json"


def ui_prefs_path() -> Path:
    """Return data/llm_state/ui_prefs.json (file may not exist yet)."""
    return global_state_dir() / "ui_prefs.json"


def reload_manifest_path() -> Path:
    """Return data/llm_state/reload_manifest.json (file may not exist yet).

    Written by hot-reload Tier 3/4 and consumed (then deleted) by the rebuilt
    window / restarted process.
    """
    return global_state_dir() / "reload_manifest.json"
