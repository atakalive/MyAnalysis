"""Annotation read/write + GUI-side file watcher."""
import contextlib
import json
from common.filelock import exclusive_lock
from common.paths import durable_read_json, durable_write_json
import dataset_config


def _empty() -> dict:
    """Return a fresh empty annotations dict. Always creates new lists."""
    return {"markers": [], "notes": []}


def _path(dataset: str, name: str, *, create: bool = True):
    return dataset_config.state_dir(dataset, name, create=create) / "annotations.json"


@contextlib.contextmanager
def _lock(dataset: str, name: str):
    """Advisory file lock for annotations read-modify-write serialization.

    Prevents data loss when multiple CLI processes (e.g. parallel LLM tool
    calls) concurrently submit/clear annotations for the same tab.
    """
    lock_path = _path(dataset, name, create=True).with_suffix(".json.lock")
    with exclusive_lock(lock_path):
        yield


def read(dataset: str, name: str) -> dict:
    """Read annotations (primary→.bak). Display-only default: 'absent'/'unreadable'
    → _empty() (NOT written back). 'ok'/'recovered' → the stored dict. Never raises.

    Unlocked callers use this; it never writes, so it cannot race the locked
    submit/clear writers (self-heal is left to the next locked write)."""
    status, data = durable_read_json(_path(dataset, name, create=False))
    return data if status in ("ok", "recovered") else _empty()


def _write(dataset: str, name: str, data: dict) -> None:
    """Durably write annotations.json (+ .bak). Caller must hold _lock."""
    durable_write_json(_path(dataset, name, create=True), data)


def submit(dataset: str, name: str, kind: str, **fields) -> None:
    """Append a marker or note. kind: 'marker' | 'note'."""
    if kind not in ("marker", "note"):
        raise ValueError(f"unknown annotation kind: {kind!r}")
    bucket = "markers" if kind == "marker" else "notes"
    with _lock(dataset, name):
        status, data = durable_read_json(_path(dataset, name, create=True))
        if status == "unreadable":
            return   # primary も .bak も読めない → 全 marker/notes 消失を避け mutation 中止
        data = data if status in ("ok", "recovered") else _empty()
        data.setdefault("markers", [])
        data.setdefault("notes", [])
        data[bucket].append(fields)
        _write(dataset, name, data)


def clear(dataset: str, name: str, kind: str | None = None) -> None:
    """Clear annotations. kind=None clears both; else clears only that bucket."""
    if kind is None:
        with _lock(dataset, name):
            _write(dataset, name, _empty())
        return
    if kind not in ("marker", "note"):
        raise ValueError(f"unknown annotation kind: {kind!r}")
    bucket = "markers" if kind == "marker" else "notes"
    with _lock(dataset, name):
        status, data = durable_read_json(_path(dataset, name, create=True))
        if status == "unreadable":
            return   # 読めない → 反対バケツを巻き添えで消さないため中止
        data = data if status in ("ok", "recovered") else _empty()
        data.setdefault("markers", [])
        data.setdefault("notes", [])
        data[bucket] = []
        _write(dataset, name, data)


def start_watcher(tab, dataset: str) -> object:
    """Watch annotations.json via parent directory; call tab.apply_annotations(dict) on change.

    Watches state_dir (the parent directory) instead of annotations.json directly.
    Atomic replace (tmp.replace(target)) changes the directory listing, so
    directoryChanged fires reliably. File-based watching loses the path on
    atomic replace and has a re-add race window where signals can be missed.

    Other file changes in state_dir (current.json, current_view.png) also trigger
    directoryChanged, so we filter by checking annotations.json's mtime_ns to
    avoid unnecessary apply_annotations calls.

    Returns the QFileSystemWatcher (caller must hold reference to keep it alive).
    """
    from PySide6.QtCore import QFileSystemWatcher
    p = _path(dataset, tab.name, create=True)
    # 一瞬の同期ラグで既存 annotations.json を空に潰さない: 本当に absent の時だけ、
    # ロック下で空を作る（unreadable/recovered/ok では触らない）。
    with _lock(dataset, tab.name):
        status, _data = durable_read_json(p)
        if status == "absent":
            _write(dataset, tab.name, _empty())
    parent_dir = str(p.parent)
    # Parent the watcher to `tab` so it is destroyed when the tab is (Tier 2/3
    # teardown + close_tab). Unparented, the closure keeps `tab` alive and a
    # later state_dir change fires _on_dir_changed on a deleted C++ object →
    # RuntimeError. See Issue #31 (pre-existing bug).
    watcher = QFileSystemWatcher([parent_dir], tab)
    last_mtime_ns = [p.stat().st_mtime_ns if p.exists() else 0]

    def _on_dir_changed(_dir_str: str) -> None:
        if not p.exists():
            last_mtime_ns[0] = 0
            return
        try:
            mtime = p.stat().st_mtime_ns
        except OSError:
            return
        if mtime == last_mtime_ns[0]:
            return
        last_mtime_ns[0] = mtime
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        tab.apply_annotations(data)

    watcher.directoryChanged.connect(_on_dir_changed)
    tab.apply_annotations(read(dataset, tab.name))
    return watcher
