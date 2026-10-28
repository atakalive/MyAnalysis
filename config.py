"""Path configuration: dataset registry with per-host full paths.

各 dataset を「ホスト名 → その PC でのフルパス」で登録する。
同じ同期フォルダ（クラウドストレージ等）が PC ごとに別ドライブにマウントされるため。

登録データは **このファイルではなく** リポジトリ直下の `datasets.local.json`
（Git 管理外・保存処理は [dataset_registry.py](dataset_registry.py)）にある。
このモジュールはその登録簿へのアクセス API（`DATASETS` / `get_dataset_dir` /
`register_dataset` / `unregister_dataset` / `reload_datasets`）を提供する。

新しい dataset → `python -m llm_bridge register-dataset <name> <path> [--host H]`
（または GUI の File → データセットを新規登録、あるいは `datasets.local.json` を直接編集）。
新しい PC → 使う各 dataset にそのホスト名のエントリを追加(キーは大文字)。
"""
from pathlib import Path

from common.host_key import HOST_RE, host_key, host_keys
from common.mount_compat import install as _install_mount_compat
from common.paths import validate_identifier_name
from dataset_registry import (  # noqa: F401 — config 経由で再公開する
    RegistryError,
    read_registry,
    registry_path,
    registry_transaction,
    write_registry,
)

_install_mount_compat()  # PIL/matplotlib の realpath(→WinError 1005) をマウント上で救う

_HOST_RE = HOST_RE   # 既存の参照のため（本体は common.host_key）

# module reload 越しに dict identity を保つ（`from config import DATASETS` の保持者が
# 古い dict を見続けないように）。中身は module 末尾の reload_datasets() が満たす。
DATASETS: dict[str, dict[str, str]] = globals().get("DATASETS", {})


def host_path(per_host: dict[str, str]) -> tuple[str, str] | None:
    """この PC のキー（``host_keys()`` の順。正規のキー → 別名）で引いた最初の (key, path)。無ければ None。"""
    for key in host_keys():
        if key in per_host:
            return key, per_host[key]
    return None


def get_dataset_dir(name: str) -> Path:
    try:
        per_host = DATASETS[name]
    except KeyError:
        known = ", ".join(DATASETS) or "(none)"
        raise KeyError(
            f"Unknown dataset {name!r}. Register it with "
            f"'python -m llm_bridge register-dataset {name} <path> [--host HOST]' "
            f"(or edit datasets.local.json). Known datasets: {known}"
        )
    hit = host_path(per_host)
    if hit is None:
        host = host_key()
        known = ", ".join(per_host) or "(none)"
        raise RuntimeError(
            f"Dataset {name!r} has no path for hostname {host!r}. Register it with "
            f"'python -m llm_bridge register-dataset {name} <path> --host {host}' "
            f"(or edit datasets.local.json). Known hosts: {known}"
        )
    return Path(hit[1])


def reload_datasets(config_path: Path | None = None) -> None:
    """登録簿を読み直し、module レベルの dict を in-place で更新する。

    In-place clear/update preserves dict identity — code that imported
    ``from config import DATASETS`` or holds a reference to the dict object
    sees the refreshed data without re-importing.

    読み込みに失敗した場合は `RegistryError` を伝播し、既存の内容は保持する
    （読み込み成功前に clear しない）。
    """
    registry = read_registry(config_path)
    DATASETS.clear()
    DATASETS.update(registry)


def register_dataset(
    name: str,
    path: str,
    host: str | None = None,
    *,
    config_path: Path | None = None,
) -> dict[str, str | bool]:
    """Register a dataset in the registry (file write only, no in-memory mutation).

    Returns {"name": str, "host": str, "path": str, "created": bool}.
    "created" is True if the dataset name was new, False if merged/updated.
    """
    from pathlib import PurePosixPath, PureWindowsPath

    validate_identifier_name(name, check_reserved=False)

    host = host.upper() if host else host_key()
    if not _HOST_RE.fullmatch(host):
        raise ValueError(f"invalid hostname: {host!r}")

    if not (PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()):
        raise ValueError(f"path must be absolute: {path!r}")

    import config_share  # lazy: config_share imports config

    created = False
    with registry_transaction(config_path=config_path) as (registry, writer):
        created = name not in registry
        registry.setdefault(name, {})[host] = path
        # R2 同期用に登録時刻を記録（他 PC の削除 tombstone より新しい再登録を残すため）。
        config_share.note_registered(name, host)
        writer(registry)

    return {"name": name, "host": host, "path": path, "created": created}


def unregister_dataset(name: str, *, config_path: Path | None = None) -> dict[str, str]:
    """Remove *name*'s registration (every host) from the registry.

    File write only, no in-memory mutation (like register_dataset). Only the
    registry entry goes — the dataset directory and everything in it stay on
    disk, so re-registering the same folder restores it. R2-sync tombstones are
    recorded under the registry lock *before* the write, so the deletion
    propagates instead of the remote copy resurrecting it (a failed write leaves
    the entry live locally, which the sync treats as a re-add and drops the
    tombstone). Returns the removed {HOST: path}; KeyError if not registered.
    """
    import config_share  # lazy: config_share imports config

    with registry_transaction(config_path=config_path) as (registry, writer):
        if name not in registry:
            raise KeyError(f"Unknown dataset {name!r}")
        removed = registry.pop(name)
        config_share.note_deleted(name, removed)
        writer(registry)
    return dict(removed)


def rename_host(
    old: str,
    new: str | None = None,
    *,
    dry_run: bool = False,
    config_path: Path | None = None,
) -> dict:
    """登録簿の OLD のキーのパスを NEW（既定はこの PC の ``host_key()``）へ移し、OLD を消す。

    データセットごとに、NEW が無ければ写して OLD を消す（moved）、NEW が同じパスなら OLD を消すだけ（removed）、
    NEW が違うパスならそのデータセットは触らない（conflicts）。消した OLD は R2 同期の tombstone に記録する
    （和集合のマージで復活させないため）。``dry_run`` は結果を計算するだけで書かない。
    """
    old_key = old.upper()
    new_key = new.upper() if new else host_key()
    for key in (old_key, new_key):
        if not HOST_RE.fullmatch(key):
            raise ValueError(f"invalid hostname: {key!r}")
    if old_key == new_key:
        raise ValueError("OLD and NEW are the same host key")

    def _plan(registry: dict) -> tuple[list[str], list[str], list[dict]]:
        moved: list[str] = []
        removed: list[str] = []
        conflicts: list[dict] = []
        for ds in sorted(registry):
            per_host = registry[ds]
            if old_key not in per_host:
                continue
            if new_key not in per_host:
                moved.append(ds)
            elif per_host[new_key] == per_host[old_key]:
                removed.append(ds)
            else:
                conflicts.append({"dataset": ds, "old_path": per_host[old_key],
                                  "new_path": per_host[new_key]})
        return moved, removed, conflicts

    if dry_run:
        moved, removed, conflicts = _plan(read_registry(config_path))
    else:
        import config_share  # lazy: config_share imports config

        with registry_transaction(config_path=config_path) as (registry, writer):
            moved, removed, conflicts = _plan(registry)
            for ds in moved:
                registry[ds][new_key] = registry[ds][old_key]
                config_share.note_registered(ds, new_key)
            for ds in moved + removed:
                registry[ds].pop(old_key)
                config_share.note_deleted(ds, [old_key])
            if moved or removed:
                writer(registry)
    return {"old": old_key, "new": new_key, "dry_run": dry_run,
            "moved": moved, "removed": removed, "conflicts": conflicts}


reload_datasets()   # 初回 import で登録簿を読み込む（破損は RegistryError で fail-fast）
