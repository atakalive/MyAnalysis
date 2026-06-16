"""LLM bridge specific paths. Lives parallel to common.paths but owns its own dir."""
import json
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


def read_ui_pref(key: str, default=None):
    """ui_prefs.json から key を読む。欠損/破損/型不一致/非 dict は default。never raise。"""
    try:
        data = json.loads(ui_prefs_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return default
    if not isinstance(data, dict):
        return default
    return data.get(key, default)


def update_ui_pref(key: str, value) -> None:
    """ui_prefs.json の key を value に更新。兄弟キー保持・atomic(temp+replace)・never raise。"""
    try:
        path = ui_prefs_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            data = {}                              # ValueError=UnicodeDecodeError（非 UTF-8）等も吸収
        data[key] = value
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except (OSError, ValueError, TypeError):        # 非 JSON-serializable value の TypeError も吸収（never-raise 厳守）
        pass


def reload_manifest_path() -> Path:
    """Return data/llm_state/reload_manifest.json (file may not exist yet).

    Written by hot-reload Tier 3/4 and consumed (then deleted) by the rebuilt
    window / restarted process.
    """
    return global_state_dir() / "reload_manifest.json"
