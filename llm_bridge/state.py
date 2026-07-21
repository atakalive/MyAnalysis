"""Per-tab state read/write. State is JSON describing current selection."""
import json
from collections.abc import Callable
from common.paths import atomic_write_text, read_json_classified
import dataset_config


def writer(dataset: str, name: str) -> Callable[[dict], None]:
    """Return an atomic writer for <work_dir>/analyses/<name>/state/current.json."""
    target = dataset_config.state_dir(dataset, name, create=True) / "current.json"
    def _write(d: dict) -> None:
        atomic_write_text(target, json.dumps(d, ensure_ascii=False, indent=2))
    return _write


def read_status(dataset: str, name: str) -> tuple[str, dict]:
    """Classified read of current.json. Returns (status, dict) where status ∈
    {'ok','absent','unreadable'} and dict is the parsed state on 'ok', else {}.

    Callers that write the result back (restore paths) MUST branch on status: on
    the synced mount a transient 0-byte/未同期 read classifies as 'unreadable',
    and persisting the {} returned here would erase real on-disk tab state.
    No mkdir / toml side effect (create=False)."""
    p = dataset_config.state_dir(dataset, name, create=False) / "current.json"
    status, data = read_json_classified(p)
    return (status, data if status == "ok" else {})


def read(dataset: str, name: str) -> dict:
    """Read state. Returns {} if absent or transiently unreadable (display default;
    do not persist a {} obtained here — use read_status to distinguish)."""
    return read_status(dataset, name)[1]
