"""Path configuration: dataset registry with per-host full paths.

各 dataset を「ホスト名 → その PC でのフルパス」で登録する。
同じ cloud drive フォルダが PC ごとに別ドライブにマウントされるため。

新しい dataset → DATASETS に追加。
新しい PC → 使う各 dataset にそのホスト名のエントリを追加(キーは大文字)。
"""
import socket
from pathlib import Path

DATASETS: dict[str, dict[str, str]] = {
    "dataset_a": {
        "HOST_A": r"G:\同期\測定\000000\example",
        "HOST_B": r"H:\同期\測定\000000\example",
    },
}


def get_dataset_dir(name: str) -> Path:
    try:
        per_host = DATASETS[name]
    except KeyError:
        known = ", ".join(DATASETS) or "(none)"
        raise KeyError(
            f"Unknown dataset {name!r}. "
            f"Add it to DATASETS in config.py. Known datasets: {known}"
        )
    host = socket.gethostname().upper()
    try:
        return Path(per_host[host])
    except KeyError:
        known = ", ".join(per_host) or "(none)"
        raise RuntimeError(
            f"Dataset {name!r} has no path for hostname {host!r}. "
            f"Add it to DATASETS[{name!r}] in config.py. Known hosts: {known}"
        )
