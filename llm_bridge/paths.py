"""LLM bridge specific paths. Lives parallel to common.paths but owns its own dir."""
import json
import time
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


def recent_datasets_path() -> Path:
    """Return data/llm_state/recent_datasets.json (file may not exist yet).

    PC-local MRU (most-recently-opened) order for the dataset picker. Kept out of
    the synced meta.json so open-order does not mix across PCs.
    """
    return global_state_dir() / "recent_datasets.json"


def read_recent_datasets() -> dict:
    """Read the MRU map {name: epoch_float}. never raise; corrupt → fresh {}.

    Values are type-normalized: only str keys mapped to non-bool int/float epochs
    survive, so a hand-edited/corrupt entry can't poison the picker's sort.
    """
    try:
        data = json.loads(recent_datasets_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in data.items()
        if isinstance(k, str) and isinstance(v, (int, float))
        and not isinstance(v, bool)
    }


def note_recent_dataset(name: str) -> None:
    """Record that `name` was just opened (MRU). atomic(temp+replace)·never raise.

    Distinct from session.note_dataset (tracks empty-tabs persistence) and
    window.note_current_dataset (pushes the current dataset to chat) — this only
    stamps the PC-local recent_datasets.json used for the picker's default sort.
    """
    try:
        data = read_recent_datasets()
        data[name] = time.time()
        path = recent_datasets_path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except (OSError, ValueError, TypeError):
        pass


def last_window_path() -> Path:
    """Return data/llm_state/last_window.json (file may not exist yet).

    PC-local workspace record: which datasets were open together + the active
    one, so File → "前回のセッションを復元" can re-open them. A second PC-local
    record next to recent_datasets.json (MRU) — MRU is open-order history,
    last_window is the co-open set. Not synced across PCs (per-dataset tab
    contents in session.json carry the syncable state).
    """
    return global_state_dir() / "last_window.json"


def reload_manifest_path() -> Path:
    """Return data/llm_state/reload_manifest.json (file may not exist yet).

    Written by hot-reload Tier 3/4 and consumed (then deleted) by the rebuilt
    window / restarted process.
    """
    return global_state_dir() / "reload_manifest.json"
