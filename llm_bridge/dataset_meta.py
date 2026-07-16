"""Materialized display metadata for the dataset picker — Qt-free, testable.

Each dataset carries a `<dataset_dir>/meta.json` cache of display-only info
(description + derived metrics) so the picker reads one small JSON per dataset
instead of rescanning the synced drive every time. The only durable field is
`description`; everything else is a derived value re-computed on the next
rebuild.

Metrics split by cost into LIGHT (a handful of small JSON reads + O(analyses)
stat/glob) and HEAVY (a full dataset_dir os.walk + per-analysis content parse).
Synchronous checkpoints call `rebuild_meta(heavy=False)` (LIGHT only, keeps the
GUI/CLI responsive); the picker's background worker runs `heavy=True`.

IMPORTANT: this module must NOT import PySide6/gui. It stays standard-library +
config/dataset_config/common.filelock/common.paths at top level; the llm_bridge readers
(annotations/state/snapshots/session) are imported inside compute_meta to break
the session→dataset_meta→session import cycle.
"""
from __future__ import annotations

import dataclasses
import json
import os
import socket
import time
import tomllib
from collections.abc import Callable, Iterable
from pathlib import Path

import config
from common.paths import atomic_write_text, safe_resolve
import dataset_config
from common.filelock import exclusive_lock

META_VERSION = 1

LIGHT_FIELDS = ("analysis_count", "analysis_names", "last_touched",
                "open_tab_count", "open_analysis_names", "chat_session_count",
                "thumbnail", "format")
HEAVY_FIELDS = ("disk_size_bytes", "last_measurement", "annotation_total",
                "export_png_count")

# Field-type validation sets for DatasetMeta.from_dict. `name` is deliberately
# absent — it is an overlay set from the registry key, never trusted from disk.
_NUM_FIELDS = ("version", "analysis_count", "last_touched", "open_tab_count",
               "chat_session_count", "disk_size_bytes", "last_measurement",
               "annotation_total", "export_png_count", "updated_at")
_LIST_FIELDS = ("analysis_names", "open_analysis_names")
_STR_FIELDS = ("description", "thumbnail", "format")


@dataclasses.dataclass
class DatasetMeta:
    """meta.json schema fields + live overlay. All optional / defaulted.

    `name` and everything below it are overlay fields (not stored in meta.json;
    filled by load_one). `name` comes from the registry key and is authoritative
    — any `name` inside meta.json is ignored.
    """
    # --- meta.json schema ---
    version: int | None = None
    description: str = ""
    analysis_count: int | None = None
    analysis_names: list[str] = dataclasses.field(default_factory=list)
    last_touched: float | None = None
    open_tab_count: int | None = None
    open_analysis_names: list[str] = dataclasses.field(default_factory=list)
    chat_session_count: int | None = None
    thumbnail: str | None = None
    format: str | None = None
    disk_size_bytes: int | None = None
    last_measurement: float | None = None
    annotation_total: int | None = None
    export_png_count: int | None = None
    updated_at: float | None = None
    # --- live overlay (not persisted) ---
    name: str | None = None
    available: bool = False
    unavailable_reason: str | None = None
    host_path: str | None = None
    other_hosts: list[str] = dataclasses.field(default_factory=list)
    last_opened: float | None = None
    uncomputed: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "DatasetMeta":
        """Build from a raw meta.json dict, dropping unknown keys and coercing
        types. read_meta only guarantees `d` is a dict; value-type validation
        (so a hand-edited/sync-corrupted field can't crash sort/render) lives
        here. `name` is ignored (overlay-only)."""
        known = {f.name for f in dataclasses.fields(cls)}
        out: dict = {}
        for k, v in d.items():
            if k not in known or k == "name":
                continue                                  # unknown / overlay-only
            if k in _NUM_FIELDS and not (
                isinstance(v, (int, float)) and not isinstance(v, bool)
            ):
                continue                                  # non-numeric → drop (=missing)
            if k in _LIST_FIELDS:
                if not isinstance(v, list):
                    continue
                v = [x for x in v if isinstance(x, str)]  # keep str elements only
            if k in _STR_FIELDS and not isinstance(v, str):
                continue
            out[k] = v
        return cls(**out)


def _meta_path(dataset: str) -> Path:
    """<dataset_dir>/meta.json. Raises KeyError/RuntimeError via get_dataset_dir."""
    return config.get_dataset_dir(dataset) / "meta.json"


def _meta_lock_path(dataset: str) -> Path:
    return _meta_path(dataset).with_suffix(".json.lock")


def _is_stale(meta: dict | None) -> bool:
    """True if meta is missing/old/uncomputed and needs a background heavy rebuild."""
    if meta is None:
        return True
    v = meta.get("version")
    # bool is an int subclass; version=True must NOT count as current (True==1).
    if not (isinstance(v, int) and not isinstance(v, bool) and v == META_VERSION):
        return True
    ac = meta.get("analysis_count")
    return not (isinstance(ac, int) and not isinstance(ac, bool))


def _merge_meta(existing: dict, computed: dict, *, heavy: bool) -> dict:
    """Merge freshly-computed metrics onto the existing meta.

    Three states are distinguished (see Issue #50): a LIGHT field missing from
    `computed` is dropped (no stale carry-over); a HEAVY field is carried over
    from `existing` on a light update or a cancelled heavy scan, but dropped when
    a completed heavy scan failed to produce it.
    """
    cancelled = computed.pop("_heavy_cancelled", False)   # internal sentinel
    merged: dict = {}
    for k in LIGHT_FIELDS:
        if k in computed:
            merged[k] = computed[k]      # success = truth; missing = "not computed"
    for k in HEAVY_FIELDS:
        if k in computed:
            merged[k] = computed[k]
        elif (not heavy) or cancelled:
            if k in existing:
                merged[k] = existing[k]  # light update / cancelled → keep last heavy value
        # heavy & not cancelled & missing → genuine failure → drop
    desc = existing.get("description")
    merged["description"] = desc if isinstance(desc, str) else ""   # None/non-str → ""
    merged["version"] = META_VERSION
    merged["updated_at"] = time.time()
    return merged


def compute_meta(
    dataset: str, *, heavy: bool = True,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """Compute display metrics for `dataset`. Never raises.

    Returns a dict holding only the metrics successfully obtained (a missing key
    means "not computed", not a zero value). `description` is never computed
    here. With heavy=False only LIGHT_FIELDS are attempted. `should_stop`, if
    given, is polled during HEAVY scans; when it returns True the scan returns
    partial results and sets the internal `_heavy_cancelled` sentinel.

    Underlying resolves may raise (TOMLDecodeError / ValueError / KeyError /
    RuntimeError / OSError) — all are absorbed per sub-step.
    """
    from llm_bridge import annotations, snapshots, session

    meta: dict = {}
    try:
        dataset_dir = config.get_dataset_dir(dataset)
    except (KeyError, RuntimeError):
        return meta   # unresolvable — nothing computable
    try:
        work_dir = dataset_config.get_work_dir(dataset, create=False)
    except (KeyError, RuntimeError, ValueError, tomllib.TOMLDecodeError, OSError):
        work_dir = None

    # ----- LIGHT -----
    analysis_names: list[str] = []
    try:
        root = dataset_config.analyses_root(dataset)
        if root.is_dir():
            root_resolved = safe_resolve(root)
            for d in sorted(root.glob("*")):
                if d.name.startswith("_"):
                    continue
                af = d / "analysis.py"
                if not af.is_file():
                    continue
                try:
                    if not safe_resolve(af).is_relative_to(root_resolved):
                        continue
                except OSError:
                    continue
                analysis_names.append(d.name)
        meta["analysis_names"] = analysis_names
        meta["analysis_count"] = len(analysis_names)
    except OSError:
        pass

    sess = None
    try:
        sess = session.read_session(dataset)
    except Exception:
        sess = None

    # last_touched: newest mtime of session.json + each state/current.json.
    try:
        mtimes: list[float] = []
        if work_dir is not None:
            sp = work_dir / "session.json"
            try:
                mtimes.append(sp.stat().st_mtime)
            except OSError:
                pass
        for n in analysis_names:
            try:
                cj = dataset_config.state_dir(dataset, n, create=False) / "current.json"
                mtimes.append(cj.stat().st_mtime)
            except OSError:
                pass
        if mtimes:
            meta["last_touched"] = max(mtimes)
    except Exception:
        pass

    open_analysis_names: list[str] = []
    try:
        if sess is not None:
            tabs = sess.get("tabs", [])
            if not isinstance(tabs, list):
                tabs = []   # corrupt session.json (e.g. tabs is a string) → no count
            meta["open_tab_count"] = len(tabs)
            open_analysis_names = [
                e.get("module") for e in tabs
                if isinstance(e, dict)
                and e.get("kind") == "analysis" and e.get("module")
            ]
            meta["open_analysis_names"] = open_analysis_names
    except Exception:
        pass

    try:
        if work_dir is not None:
            n_chats = len(list((work_dir / "chat_sessions").glob("*.json")))
            meta["chat_session_count"] = n_chats
    except OSError:
        pass

    # thumbnail: resolve active analysis tab (active_tab is a tab-name string).
    try:
        active_name = sess.get("active_tab") if sess else None
        active_module = next(
            (e.get("module") for e in (sess.get("tabs", []) if sess else [])
             if e.get("name") == active_name and e.get("kind") == "analysis"),
            None,
        )
        cand = active_module or (open_analysis_names[0] if open_analysis_names else None)
        if cand:
            p = snapshots.path(dataset, cand)
            if p.exists():
                try:
                    meta["thumbnail"] = str(p.relative_to(dataset_dir))
                except ValueError:
                    pass   # absolute work_dir outside dataset_dir → no thumbnail key
    except Exception:
        pass

    try:
        meta["format"] = dataset_config.load_config(dataset).get(
            "format", "csv_per_subdir")
    except (tomllib.TOMLDecodeError, ValueError, KeyError, RuntimeError, OSError):
        pass

    # ----- HEAVY -----
    if heavy:
        cancelled = False
        # work_dir-dependent HEAVY: annotations + export PNGs, per analysis.
        if work_dir is not None:
            total_ann = 0
            for n in analysis_names:
                if should_stop and should_stop():
                    cancelled = True
                    break
                try:
                    ann = annotations.read(dataset, n)
                    total_ann += len(ann.get("markers", [])) + len(ann.get("notes", []))
                except (json.JSONDecodeError, OSError, tomllib.TOMLDecodeError,
                        ValueError, KeyError, RuntimeError):
                    continue
            if not cancelled:
                meta["annotation_total"] = total_ann

            if not cancelled:
                total_png = 0
                for n in analysis_names:
                    if should_stop and should_stop():
                        cancelled = True
                        break
                    try:
                        bd = dataset_config.batch_dir(dataset, n, create=False)
                        total_png += len(list(bd.glob("*.png")))
                    except (OSError, tomllib.TOMLDecodeError, ValueError,
                            KeyError, RuntimeError):
                        continue   # per-analysis failure → skip (0), commit partial sum
                if not cancelled:
                    meta["export_png_count"] = total_png

        # dataset_dir-dependent HEAVY (work_dir-independent): single un-pruned
        # os.walk for disk_size_bytes (all non-symlink files) + last_measurement
        # (same walk, excluding work_dir/analyses subtrees + sidecars).
        try:
            total = 0
            latest = None
            wd_res = safe_resolve(work_dir) if work_dir is not None else None
            an_res = safe_resolve(dataset_config.analyses_root(dataset))
            for cur, _dirnames, filenames in os.walk(dataset_dir):
                if should_stop and should_stop():
                    cancelled = True
                    break
                cur_p = Path(cur)
                try:
                    cr = safe_resolve(cur_p)
                    excluded = bool(
                        (wd_res and cr.is_relative_to(wd_res))
                        or cr.is_relative_to(an_res)
                    )
                except OSError:
                    excluded = False
                for fn in filenames:
                    p = cur_p / fn
                    try:
                        if p.is_symlink():
                            continue
                        st = p.stat()
                    except OSError:
                        continue
                    total += st.st_size
                    if excluded:
                        continue
                    if fn in {"myanalysis.toml", "meta.json"} \
                            or fn.endswith((".tmp", ".lock")):
                        continue
                    latest = st.st_mtime if latest is None else max(latest, st.st_mtime)
            if not cancelled:
                meta["disk_size_bytes"] = total
                if latest is not None:
                    meta["last_measurement"] = latest
        except OSError:
            pass   # walk-level failure (PermissionError etc.); keep partial HEAVY keys

        if cancelled:
            meta["_heavy_cancelled"] = True
    return meta


def read_meta(dataset: str) -> dict | None:
    """Read <dataset_dir>/meta.json. Returns None on any failure. Never raises.

    Guarantees only that the return value is a dict; it does NOT validate the
    types of known keys (they are passed through). Type normalization is
    DatasetMeta.from_dict's job. `UnicodeDecodeError` (a ValueError subclass, from
    a binary/non-UTF-8 meta.json) is caught like in read_ui_pref.
    """
    try:
        text = _meta_path(dataset).read_text(encoding="utf-8")
        data = json.loads(text)
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError,
            ValueError, OSError, KeyError, RuntimeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def write_meta(dataset: str, meta: dict) -> None:
    """Atomically write meta.json (tmp + replace)."""
    path = _meta_path(dataset)
    atomic_write_text(path, json.dumps(meta, ensure_ascii=False, indent=2))


def rebuild_meta(
    dataset: str, *, heavy: bool = True,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """Recompute metrics and merge into meta.json. Never raises.

    The heavy compute (incl. os.walk) runs OUTSIDE the lock; only the short
    read-latest → merge → write section holds the lock. This keeps the
    (POSIX-unbounded) exclusive_lock free of long scans, so a background heavy
    rebuild does not block main-thread patch_description / light rebuilds.
    """
    try:
        _meta_path(dataset)               # probe: get_dataset_dir may raise
    except (KeyError, RuntimeError):
        return
    computed = compute_meta(dataset, heavy=heavy, should_stop=should_stop)
    try:
        with exclusive_lock(_meta_lock_path(dataset)):
            existing = read_meta(dataset) or {}
            write_meta(dataset, _merge_meta(existing, computed, heavy=heavy))
    except (KeyError, RuntimeError, OSError):
        return


def patch_description(dataset: str, text: str) -> None:
    """Set meta.json's description, preserving other fields. Does NOT bump
    updated_at. A meta created from scratch here lacks analysis_count, so it is
    _is_stale → picked up by the background heavy rebuild."""
    with exclusive_lock(_meta_lock_path(dataset)):
        meta = read_meta(dataset) or {}
        meta["description"] = text
        meta.setdefault("version", META_VERSION)
        write_meta(dataset, meta)


def load_one(name: str) -> DatasetMeta:
    """Read meta.json for `name` and apply the live overlay. Never raises."""
    from llm_bridge import paths as lb_paths

    m = read_meta(name)
    base = DatasetMeta.from_dict(m) if m else DatasetMeta()
    base.name = name                          # registry key is authoritative
    base.uncomputed = _is_stale(m)

    # availability (live; not read from load_config so one corrupt toml can't
    # take down the whole picker).
    try:
        dir_ = config.get_dataset_dir(name)
        if dir_.is_dir():
            base.available = True
            base.unavailable_reason = None
            base.host_path = str(dir_)
        else:
            base.available = False
            base.unavailable_reason = "missing"
    except KeyError:
        base.available = False
        base.unavailable_reason = "bad-config"
    except RuntimeError:
        base.available = False
        base.unavailable_reason = "no-host"
    except Exception:
        base.available = False
        base.unavailable_reason = "bad-config"

    try:
        this_host = socket.gethostname().upper()
        base.other_hosts = sorted(set(config.DATASETS.get(name, {})) - {this_host})
    except Exception:
        base.other_hosts = []

    try:
        base.last_opened = lb_paths.read_recent_datasets().get(name)
    except Exception:
        base.last_opened = None

    return base


def load_for_picker(names: Iterable[str]) -> list[DatasetMeta]:
    """Load a DatasetMeta for each name. One corrupt entry can't stop the list."""
    return [load_one(n) for n in names]
