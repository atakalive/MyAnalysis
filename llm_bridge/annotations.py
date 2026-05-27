"""Annotation read/write + GUI-side file watcher."""
import contextlib
import fcntl
import json
from collections.abc import Callable
from common.paths import state_dir


def _empty() -> dict:
    """Return a fresh empty annotations dict. Always creates new lists."""
    return {"markers": [], "notes": []}


def _path(name: str):
    return state_dir(name) / "annotations.json"


@contextlib.contextmanager
def _lock(name: str):
    """Advisory file lock for annotations read-modify-write serialization.

    Prevents data loss when multiple CLI processes (e.g. parallel LLM tool
    calls) concurrently submit/clear annotations for the same tab.
    """
    lock_path = _path(name).with_suffix(".json.lock")
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def read(name: str) -> dict:
    p = _path(name)
    if not p.exists():
        return _empty()
    return json.loads(p.read_text(encoding="utf-8"))


def _write(name: str, data: dict) -> None:
    p = _path(name)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def submit(name: str, kind: str, **fields) -> None:
    """Append a marker or note. kind: 'marker' | 'note'."""
    if kind not in ("marker", "note"):
        raise ValueError(f"unknown annotation kind: {kind!r}")
    bucket = "markers" if kind == "marker" else "notes"
    with _lock(name):
        data = read(name)
        data[bucket].append(fields)
        _write(name, data)


def clear(name: str, kind: str | None = None) -> None:
    """Clear annotations. kind=None clears both; else clears only that bucket."""
    if kind is None:
        with _lock(name):
            _write(name, _empty())
        return
    if kind not in ("marker", "note"):
        raise ValueError(f"unknown annotation kind: {kind!r}")
    bucket = "markers" if kind == "marker" else "notes"
    with _lock(name):
        data = read(name)
        data[bucket] = []
        _write(name, data)


def start_watcher(tab) -> object:
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
    p = _path(tab.name)
    if not p.exists():
        _write(tab.name, _empty())
    parent_dir = str(p.parent)
    watcher = QFileSystemWatcher([parent_dir])
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
    tab.apply_annotations(read(tab.name))
    return watcher
