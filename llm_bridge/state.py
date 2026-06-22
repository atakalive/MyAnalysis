"""Per-tab state read/write. State is JSON describing current selection."""
import json
from collections.abc import Callable
import dataset_config


def writer(dataset: str, name: str) -> Callable[[dict], None]:
    """Return an atomic writer for <work_dir>/analyses/<name>/state/current.json."""
    target = dataset_config.state_dir(dataset, name, create=True) / "current.json"
    def _write(d: dict) -> None:
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
    return _write


def read(dataset: str, name: str) -> dict:
    """Read state. Returns {} if file does not exist (no mkdir / toml side effect)."""
    p = dataset_config.state_dir(dataset, name, create=False) / "current.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))
