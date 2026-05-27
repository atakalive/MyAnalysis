"""Per-tab state read/write. State is JSON describing current selection."""
import json
from pathlib import Path
from collections.abc import Callable
from common.paths import state_dir


def writer(name: str) -> Callable[[dict], None]:
    """Return an atomic writer for data/analyses/<name>/state/current.json."""
    target = state_dir(name) / "current.json"
    def _write(d: dict) -> None:
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
    return _write


def read(name: str) -> dict:
    """Read state. Returns {} if file does not exist."""
    p = state_dir(name) / "current.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))
