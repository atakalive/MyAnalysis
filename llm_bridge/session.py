"""Per-dataset session save/restore (open-tab layout) — Qt-free core.

A dataset's session (which tabs were open, their figures/analysis, the active
tab) belongs to the *dataset*, not the app. It lives at `<work_dir>/session.json`
so it syncs with the data and follows it across PCs/repos. There is no repo-local
global "last session" pointer — that would be app-state restore, not data-bound
restore.

IMPORTANT: this module must NOT import PySide6/gui at top level. `llm_bridge/__init__`
imports it, and that package is imported from the CLI too. Functions that touch a
window use it via duck-typing on the passed-in window argument. Top-level imports
stay standard-library + config/dataset_config so read_session/write_session/
infer_dataset are testable without Qt.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import config
import dataset_config
from llm_bridge import chat_store

_log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Datasets that held (or once held) a tracked tab whose empty `tabs:[]`
# persistence is not yet complete. See module docstring of the issue for the
# lifecycle contract. Populated only by note_dataset (called from show/add-tab
# handlers), never by open_dataset.
_touched: set[str] = set()


def note_dataset(name: str) -> None:
    """Record that `name` has (or had) a tracked tab. Called from show/add-tab."""
    _touched.add(name)


def _resolve_work_dir_readonly(dataset: str) -> Path:
    """Resolve a dataset's work_dir WITHOUT side effects (no toml/dir creation).

    Same logic as dataset_config.get_work_dir but without ensure_config() and
    mkdir(). Does not absorb exceptions — KeyError (unregistered dataset),
    RuntimeError (no host path), FileNotFoundError (no toml), ValueError (bad
    work_dir) all propagate to the caller for individual handling.
    """
    from pathlib import PureWindowsPath

    dataset_dir = config.get_dataset_dir(dataset)
    work_dir = dataset_config.load_config(dataset)["work_dir"]

    p = Path(work_dir)
    pw = PureWindowsPath(work_dir)
    if p.is_absolute():
        return p
    if pw.drive:
        raise ValueError(f"work_dir にドライブ相対パスは使えません: {work_dir!r}")
    if pw.root:
        raise ValueError(f"work_dir にルート相対パスは使えません: {work_dir!r}")
    if ".." in pw.parts:
        raise ValueError(f"work_dir に '..' は使えません: {work_dir!r}")
    resolved = dataset_dir / p
    if not resolved.resolve().is_relative_to(dataset_dir.resolve()):
        raise ValueError(f"work_dir が dataset dir 外に解決されました: {resolved}")
    return resolved


def read_session(dataset: str) -> dict | None:
    """Read <work_dir>/session.json. Returns None on any failure.

    work_dir resolution failure, missing file, and JSON parse failure all
    return None (caught individually).
    """
    try:
        work_dir = _resolve_work_dir_readonly(dataset)
    except Exception:
        return None
    path = work_dir / "session.json"
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def write_session(dataset: str, payload: dict) -> None:
    """Atomically write <work_dir>/session.json (side-effecting work_dir resolve)."""
    work_dir = dataset_config.get_work_dir(dataset)
    target = work_dir / "session.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(target)


def save_all(window) -> tuple[list[str], list[str]]:
    """Save every dataset's session from the window's tracked tabs.

    Returns (saved, failed): dataset names saved successfully and those that
    raised. Clears window dirty only if `failed` is empty.
    """
    # 1. Group session-tracked tabs by dataset, preserving tab order.
    grouped: dict[str, list[dict]] = {}
    for tab in window.tabs():
        spec = getattr(tab, "session_spec", None)
        if not spec:
            continue
        ds = spec.get("dataset")
        if ds is None:
            continue
        grouped.setdefault(ds, []).append(spec)

    active = window.active_tab()
    active_name = active.name if active is not None else None

    # 2. Target datasets = grouped ∪ _touched (latter writes empty tabs:[] for
    #    datasets whose tabs were all closed, reflecting the erasure).
    targets = set(grouped) | set(_touched)

    saved: list[str] = []
    failed: list[str] = []
    for ds in targets:
        specs = grouped.get(ds, [])
        try:
            work_dir = dataset_config.get_work_dir(ds)
            tabs: list[dict] = []
            ds_tab_names: set[str] = set()
            for spec in specs:
                if spec.get("kind") == "figure":
                    fig = spec.get("figure")
                    try:
                        rel = str(Path(fig).relative_to(work_dir))
                    except (ValueError, TypeError):
                        rel = fig
                    tabs.append(
                        {
                            "name": spec.get("name"),
                            "kind": "figure",
                            "figure": rel,
                        }
                    )
                elif spec.get("kind") == "analysis":
                    tabs.append(
                        {
                            "name": spec.get("name"),
                            "kind": "analysis",
                            "module": spec.get("module"),
                        }
                    )
                ds_tab_names.add(spec.get("name"))
            # active_tab is this dataset's tab name only (else null).
            ds_active = active_name if active_name in ds_tab_names else None
            payload = {
                "version": SCHEMA_VERSION,
                "dataset": ds,
                "active_tab": ds_active,
                "tabs": tabs,
            }
            write_session(ds, payload)
            saved.append(ds)
            # Empty tabs:[] persisted → stop tracking. Non-empty → keep tracking.
            if not tabs:
                _touched.discard(ds)
        except Exception:
            _log.warning("save_all: failed to save session for %r", ds, exc_info=True)
            failed.append(ds)

    # Chat persistence ride-along — fully independent of the tab-save loop above.
    # Side-effect only: never touches `saved`/`failed` or the dirty-clear gate
    # (the existing tests assert those exactly). All chat access is duck-typed so
    # the headless _FakeWindow / CLI skip it entirely.
    _save_chat_sessions(window)

    if not failed:
        window.clear_session_dirty()
    return saved, failed


def _save_chat_sessions(window) -> None:
    """Persist dataset-bound chat sessions and apply delete tombstones.

    Independent of tab saving. Each dataset is isolated in its own try/except so
    one work_dir failure can't cascade. Best-effort: failures only warn.
    """
    sessions = getattr(window, "chat_sessions", lambda: [])()
    deleted = getattr(window, "chat_deleted_sessions", lambda: [])()

    # Group dataset-bound, non-empty live sessions by dataset.
    live_by_ds: dict[str, list] = {}
    for sess in sessions:
        if sess.dataset is None:
            continue
        if len(sess.messages) <= 1:  # system-only → nothing worth saving
            continue
        live_by_ds.setdefault(sess.dataset, []).append(sess)

    # Build tombstone lookup: dataset → set of ids to delete.
    tomb_ids_by_ds: dict[str, set] = {}
    for (d, sid) in deleted:
        tomb_ids_by_ds.setdefault(d, set()).add(sid)

    applied: list = []
    for ds in set(live_by_ds) | set(tomb_ids_by_ds):
        try:
            work_dir = dataset_config.get_work_dir(ds)
            tomb_ids = tomb_ids_by_ds.get(ds, set())
            # (a) physical deletes first; collect those confirmed absent.
            for sid in tomb_ids:
                if chat_store.delete_session_file(work_dir, sid):
                    applied.append((ds, sid))
            # (b) live writes (skip any id under a tombstone — tombstone wins).
            for sess in live_by_ds.get(ds, []):
                if sess.id in tomb_ids:
                    continue
                chat_store.write_session_file(work_dir, sess)
        except Exception:
            _log.warning(
                "save_all: failed to persist chat for %r", ds, exc_info=True
            )

    getattr(window, "chat_clear_deleted", lambda a: None)(applied)


def open_dataset(window, dataset: str) -> str:
    """Restore a dataset's tabs + chat sessions. Returns a summary string.

    Chat persistence is independent of tab persistence, so a dataset may have
    chat_sessions/ but no session.json (opened, chatted, saved). Chat restore +
    the final current-dataset push therefore run on every non-`error:` path
    (including `no-session:`), not just `restored:N`.
    """
    was_dirty = window.is_session_dirty()
    cw = getattr(window, "chat_widget", lambda: None)()
    resolved = False
    work_dir = None
    result = f"error:{dataset}"
    window.set_suppress_dirty(True)
    try:
        try:
            import config
            config.reload_datasets()
        except Exception:
            pass  # best-effort; proceed to existing error path
        try:
            work_dir = _resolve_work_dir_readonly(dataset)
            resolved = True
            # Verify dataset_dir actually exists on this host.
            dataset_dir = config.get_dataset_dir(dataset)
            if not dataset_dir.is_dir():
                _log.warning(
                    "open_dataset: dataset dir does not exist: %s", dataset_dir,
                )
                return f"error:{dataset}"
        except Exception:
            return f"error:{dataset}"

        sess = read_session(dataset)
        if sess is None:
            result = f"no-session:{dataset}"
        else:
            restored = 0
            for entry in sess.get("tabs", []):
                try:
                    kind = entry.get("kind")
                    if kind == "figure":
                        fig = entry.get("figure")
                        fp = Path(fig)
                        abs_path = fp if fp.is_absolute() else work_dir / fp
                        if not abs_path.is_file():
                            _log.warning(
                                "open_dataset: missing figure %s (tab %r)",
                                abs_path,
                                entry.get("name"),
                            )
                            continue
                        window.dispatch_command(
                            "show",
                            path=str(abs_path),
                            name=entry.get("name"),
                            dataset=dataset,
                        )
                        restored += 1
                    elif kind == "analysis":
                        window.dispatch_command("add-tab", name=entry.get("module"))
                        restored += 1
                except Exception:
                    _log.warning(
                        "open_dataset: failed to restore tab %r", entry,
                        exc_info=True,
                    )

            active_tab = sess.get("active_tab")
            if active_tab is not None:
                window.set_active_tab(active_tab)

            if restored >= 1:
                window.close_tab("(empty)")

            result = f"restored:{restored}"

        # Chat restore (best-effort, exception-isolated from tab restore).
        if cw is not None:
            try:
                cw.merge_dataset_sessions(
                    dataset, chat_store.load_dataset_sessions(work_dir)
                )
            except Exception:
                _log.warning(
                    "open_dataset: failed to restore chat for %r", dataset,
                    exc_info=True,
                )
    finally:
        window.set_suppress_dirty(False)
        if was_dirty:
            window.mark_session_dirty()

    # Final current-dataset push (after suppress is lifted) — establishes the
    # current dataset / adoption on no-session / restored:0 paths where no
    # currentChanged fired. Idempotent no-op on restored:N≥1.
    if resolved:
        note = getattr(window, "note_current_dataset", None)
        if note is not None:
            try:
                note(dataset)
            except Exception:
                _log.warning(
                    "open_dataset: failed to notify window dataset for %r", dataset,
                    exc_info=True,
                )
        elif cw is not None:
            try:
                cw.set_current_dataset(dataset)
            except Exception:
                _log.warning(
                    "open_dataset: failed to notify chat dataset for %r", dataset,
                    exc_info=True,
                )
    return result


def infer_dataset(abs_path: str) -> str | None:
    """Best-effort dataset inference from an absolute figure path (dataset omitted).

    Scans config.DATASETS, resolving each work_dir read-only; if abs_path is
    under it, that dataset is a candidate. Each dataset's processing is fully
    guarded. On multiple candidates (nested work_dirs), the longest path wins.
    Returns None if no candidate.
    """
    target = Path(abs_path)
    best: str | None = None
    best_len = -1
    for ds in config.DATASETS:
        try:
            work_dir = _resolve_work_dir_readonly(ds)
            wd = work_dir.resolve()
            if target.resolve().is_relative_to(wd):
                length = len(str(wd))
                if length > best_len:
                    best_len = length
                    best = ds
        except Exception:
            continue
    return best
