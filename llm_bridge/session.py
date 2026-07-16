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
from common.paths import safe_resolve
from llm_bridge import chat_store

_log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Datasets that held (or once held) a tracked tab whose empty `tabs:[]`
# persistence is not yet complete. See module docstring of the issue for the
# lifecycle contract. Populated only by note_dataset (called from show/add-tab
# handlers), never by open_dataset.
_touched: set[str] = set()

# Hot-reload (Tier 1): preserve `_touched` across a re-exec of this module so a
# patch doesn't drop pending empty-tabs persistence. Under Tier 3 the set is
# already flushed empty by save_all, so this is consistent there too.
__hot_preserve__ = ["_touched"]


def note_dataset(name: str) -> None:
    """Record that `name` has (or had) a tracked tab. Called from show/add-tab."""
    _touched.add(name)


def forget_dataset(name: str) -> None:
    """Drop *name* from the save-target set (_touched).

    Called by ToolWindow.close_dataset AFTER flushing that dataset's layout, so
    a later window-wide save_all won't write an empty ``tabs:[]`` over the
    flushed layout (close ≠ forget). Idempotent.
    """
    _touched.discard(name)


def _spec_to_tab(spec: dict, work_dir: Path) -> dict | None:
    """Reduce a live tab's session_spec to its persisted form.

    Handles kind in {"figure", "image", "analysis"}; figure/image relativise
    their path against work_dir. Returns None for any other/unknown kind.
    """
    if spec.get("kind") == "figure":
        fig = spec.get("figure")
        try:
            rel = str(Path(fig).relative_to(work_dir))
        except (ValueError, TypeError):
            rel = fig
        return {"name": spec.get("name"), "kind": "figure", "figure": rel}
    if spec.get("kind") == "image":
        img = spec.get("image")
        try:
            rel = str(Path(img).relative_to(work_dir))
        except (ValueError, TypeError):
            rel = img
        return {"name": spec.get("name"), "kind": "image", "image": rel}
    if spec.get("kind") == "analysis":
        return {
            "name": spec.get("name"),
            "kind": "analysis",
            "module": spec.get("module"),
        }
    return None


def _resolve_work_dir_readonly(dataset: str) -> Path:
    """Resolve a dataset's work_dir WITHOUT side effects (no toml/dir creation).

    Delegates to dataset_config.get_work_dir(create=False): validation +
    resolution only, no ensure_config() / mkdir(). Does not absorb exceptions —
    KeyError (unregistered dataset), RuntimeError (no host path), ValueError (bad
    work_dir) all propagate to the caller for individual handling.
    """
    return dataset_config.get_work_dir(dataset, create=False)


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

    New side effect: rebuilds each saved dataset's `<dataset_dir>/meta.json`
    (rebuild_meta, heavy=False) as a ride-along — does not affect saved/failed
    or the dirty-clear gate.
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
                entry = _spec_to_tab(spec, work_dir)
                if entry is not None:
                    tabs.append(entry)
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

    # Materialize display meta (LIGHT only) for each successfully saved dataset.
    # Ride-along side effect (like chat above): never touches saved/failed or the
    # dirty-clear gate; each dataset isolated. Lazy import breaks the cycle.
    for ds in saved:
        try:
            from llm_bridge import dataset_meta
            dataset_meta.rebuild_meta(ds, heavy=False)
        except Exception:
            _log.warning(
                "save_all: failed to update meta for %r", ds, exc_info=True
            )

    # Record the open-dataset workspace (3rd ride-along, like chat/meta above):
    # which datasets were open together + the active one. Fully isolated — never
    # touches saved/failed or the dirty-clear gate.
    try:
        write_last_window(window)
    except Exception:
        _log.warning("save_all: failed to write last_window", exc_info=True)

    if not failed:
        window.clear_session_dirty()
    return saved, failed


def save_dataset(window, dataset: str) -> bool:
    """Write ONLY *dataset*'s current tab layout to <work_dir>/session.json.

    A thin per-dataset variant of save_all's write logic used by
    ToolWindow.close_dataset to flush one dataset before dropping its group.
    Deliberately does NOT touch save_all's (saved, failed) contract, the global
    dirty-clear gate, the chat/meta/last_window ride-alongs, or _touched.

    Returns True on success — INCLUDING the zero-tab case, where it writes
    nothing (persisting an empty layout would erase a synced session.json;
    close ≠ forget). Returns False only on disk / work_dir-resolve / unexpected
    failure, so close_dataset can abort rather than lose unsaved edits.
    """
    try:
        specs = [
            spec for tab in window.tabs()
            if (spec := getattr(tab, "session_spec", None))
            and spec.get("dataset") == dataset
        ]
        if not specs:
            return True   # zero tabs → do not persist an empty layout
        active = window.active_tab()
        active_name = active.name if active is not None else None
        work_dir = dataset_config.get_work_dir(dataset)
        tabs: list[dict] = []
        ds_tab_names: set[str] = set()
        for spec in specs:
            entry = _spec_to_tab(spec, work_dir)
            if entry is not None:
                tabs.append(entry)
            ds_tab_names.add(spec.get("name"))
        ds_active = active_name if active_name in ds_tab_names else None
        write_session(dataset, {
            "version": SCHEMA_VERSION,
            "dataset": dataset,
            "active_tab": ds_active,
            "tabs": tabs,
        })
        return True
    except Exception:
        _log.warning("save_dataset: failed to save %r", dataset, exc_info=True)
        return False


def write_last_window(window) -> None:
    """Persist the open-dataset workspace to data/llm_state/last_window.json.

    {version, datasets, active}: datasets from window.open_dataset_names()
    (the group registry — includes zero-tab datasets), active from
    current_dataset. Duck-typed via getattr so a headless fake window records an
    empty workspace instead of raising.
    """
    from llm_bridge import paths as lb_paths
    names = list(getattr(window, "open_dataset_names", lambda: [])())
    active = getattr(window, "current_dataset", None)
    payload = {"version": 1, "datasets": names, "active": active}
    path = lb_paths.last_window_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(path)


def read_last_window() -> dict:
    """Read last_window.json → {version, datasets, active}. never raise; {} on failure."""
    from llm_bridge import paths as lb_paths
    try:
        data = json.loads(
            lb_paths.last_window_path().read_text(encoding="utf-8")
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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
            #     Stamp explicit tab order from list position so drag-and-drop
            #     reordering persists. live_by_ds[ds] preserves self._sessions
            #     order, so enumerate() index == per-dataset tab position.
            for idx, sess in enumerate(live_by_ds.get(ds, [])):
                if sess.id in tomb_ids:
                    continue
                sess.order = float(idx)
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

    New side effects (on the resolved path only): rebuilds the synced
    `<dataset_dir>/meta.json` (rebuild_meta, heavy=False) and stamps the
    PC-local `data/llm_state/recent_datasets.json` (note_recent_dataset).
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
                    elif kind == "image":
                        img = entry.get("image")
                        ip = Path(img)
                        abs_path = ip if ip.is_absolute() else work_dir / ip
                        if not abs_path.is_file():
                            _log.warning(
                                "open_dataset: missing image %s (tab %r)",
                                abs_path,
                                entry.get("name"),
                            )
                            continue
                        window.dispatch_command(
                            "show-image",
                            path=str(abs_path),
                            name=entry.get("name"),
                            dataset=dataset,
                        )
                        restored += 1
                    elif kind == "analysis":
                        window.dispatch_command(
                            "add-tab", name=entry.get("module"), dataset=dataset
                        )
                        restored += 1
                except Exception:
                    _log.warning(
                        "open_dataset: failed to restore tab %r", entry,
                        exc_info=True,
                    )

            active_tab = sess.get("active_tab")
            if active_tab is not None:
                # 開いた dataset に属するタブのときだけ focus する（同名衝突で別
                # dataset の同名タブを誤 focus しないため。reviewer P1 R2）。
                # ⚠️ 存在チェックの next() も (dataset, name) で照合する（reviewer code
                # P2）。bare name だと先に開いた別 dataset の同名タブに当たり、対象
                # dataset 内に active_tab があっても t_ds != dataset で復元されない。
                t = next(
                    (t for t in window.tabs()
                     if t.name == active_tab
                     and (getattr(t, "session_spec", None) or {}).get("dataset")
                     == dataset),
                    None,
                )
                if t is not None:
                    # Explicit dataset= so a same-named tab in another dataset is
                    # never focused instead (B4 / reviewer P2-e).
                    try:
                        window.set_active_tab(active_tab, dataset=dataset)
                    except TypeError:
                        window.set_active_tab(active_tab)   # headless fake fallback

            # The (empty) placeholder is retired at real-group creation
            # (_ensure_group), so no explicit close_tab("(empty)") is needed on
            # the restored>=1 path. Kept as a fallback for headless windows that
            # have no group model but still hold an "(empty)" tab.
            if restored >= 1 and not hasattr(window, "_ensure_group"):
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
        ensure = getattr(window, "_ensure_group", None)
        set_active_ds = getattr(window, "set_active_dataset", None)
        if ensure is not None and set_active_ds is not None:
            # New group model: register the group (covers the zero-tab
            # no-session/restored:0 case, so the workspace registry is the truth)
            # then bring it to the front — QStackedWidget page switch +
            # current_dataset + chat push all via _select_dataset_group.
            try:
                ensure(dataset)
                set_active_ds(dataset)
            except Exception:
                _log.warning(
                    "open_dataset: failed to select dataset group for %r", dataset,
                    exc_info=True,
                )
        else:
            note = getattr(window, "note_current_dataset", None)
            if note is not None:
                try:
                    note(dataset)
                except Exception:
                    _log.warning(
                        "open_dataset: failed to notify window dataset for %r",
                        dataset, exc_info=True,
                    )
            elif cw is not None:
                try:
                    cw.set_current_dataset(dataset)
                except Exception:
                    _log.warning(
                        "open_dataset: failed to notify chat dataset for %r",
                        dataset, exc_info=True,
                    )
        # Materialize display meta (LIGHT only — keep this GUI-thread path fast)
        # and stamp the PC-local MRU. Lazy import breaks the
        # session→dataset_meta→session cycle; isolated so a failure never
        # affects the restore result.
        try:
            from llm_bridge import dataset_meta, paths as lb_paths
            dataset_meta.rebuild_meta(dataset, heavy=False)
            lb_paths.note_recent_dataset(dataset)
        except Exception:
            _log.warning(
                "open_dataset: failed to update meta/MRU for %r", dataset,
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
            wd = safe_resolve(work_dir)
            if safe_resolve(target).is_relative_to(wd):
                length = len(str(wd))
                if length > best_len:
                    best_len = length
                    best = ds
        except Exception:
            continue
    return best
