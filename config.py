"""Path configuration: per-host data root + named dataset registry.

Add a new PC → append to DATA_ROOTS.
Add a new dataset → append to DATASETS (relative path under the root).
"""
import socket
from pathlib import Path

DATA_ROOTS: dict[str, str] = {
    "HOST_A": r"G:\同期\測定",
    "HOST_B": r"H:\同期\測定",
}

DATASETS: dict[str, str] = {
    "dataset_a": "000000/example",
}


def get_data_root() -> Path:
    host = socket.gethostname()
    try:
        return Path(DATA_ROOTS[host])
    except KeyError:
        known = ", ".join(DATA_ROOTS) or "(none)"
        raise RuntimeError(
            f"No DATA_ROOT registered for hostname {host!r}. "
            f"Add it to DATA_ROOTS in config.py. Known hosts: {known}"
        )


def get_dataset_dir(name: str) -> Path:
    try:
        rel = DATASETS[name]
    except KeyError:
        known = ", ".join(DATASETS) or "(none)"
        raise KeyError(
            f"Unknown dataset {name!r}. "
            f"Add it to DATASETS in config.py. Known datasets: {known}"
        )
    return get_data_root() / rel
