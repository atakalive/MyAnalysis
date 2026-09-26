"""Per-dataset session save/restore (open-tab layout) — Qt-free core.

A dataset's session (which tabs were open, their figures/analysis, the active
tab) belongs to the *dataset*, not the app. It lives at `<work_dir>/session.json`
so it syncs with the data and follows it across PCs/repos. There is no repo-local
global "last session" pointer — that would be app-state restore, not data-bound
restore.

IMPORTANT: this module must NOT import PySide6/gui at top level. `llm_bridge/__init__`
imports it, and that package is imported from the CLI too. Functions that touch a
window use it via duck-typing on the passed-in window argument. Top-level imports
stay standard-library + config/dataset_config/common.paths so read_session/write_session/
infer_dataset are testable without Qt.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import config
import dataset_config
from common.paths import (
    bak_path,
    durable_read_json,
    durable_write_json,
    read_json_classified,
    safe_resolve,
    strip_seq,
)
from common.slots import conflicts, format_slot, parse_slot
from llm_bridge import chat_store

_log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class SessionPersistError(RuntimeError):
    """write_session の read-back 検証失敗（書いた直後の primary が読み戻せない）。

    同期マウント（rclone/WinFsp）は書込を例外なしに 0 バイトへ truncate し得る。
    save_all/save_dataset がこれを捕捉して failed 扱いにすることで、Tier 3/4
    リロードの中止・「保存して終了」の close 拒否・close_dataset の中止という
    既存の失敗経路に乗る（黙ってタブ構成を失わない）。
    """


class SessionUnreadableError(RuntimeError):
    """session.json が存在するのに読めない（0byte/破損で .bak でも回復不能）。

    「セッションが無い」(no-session) と混同してはならない — 混同すると
    transient なマウント障害が恒久的なタブ構成の消失として観測される。
    """

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


def _spec_to_tab(spec: dict, work_dir: Path, tab=None) -> dict | None:
    """Reduce a live tab's session_spec to its persisted form.

    Handles kind in {"figure", "image", "analysis"}; figure/image relativise
    their path against work_dir. Returns None for any other/unknown kind.

    `tab` (optional, default None) is the live tab widget. When present and it
    exposes the relevant duck-typed members, two OPTIONAL fields are added — both
    back-compat, since the Qt-free _FakeTab (name/session_spec only) exposes
    neither, so existing save tests see an unchanged entry:
      - ``layout`` (all kinds): tab.capture_layout() if callable.
      - ``figure2`` (kind=="figure" only): work_dir-relative path of the split
        second figure, from tab._panels["figure-2"]._path if present.

    A live viewer tab exposing ``pane_contents()`` (gui AnalysisTab, Issue #97)
    is serialised from its split tree instead — see _tree_to_entry.
    """
    kind = spec.get("kind")
    if kind in ("figure", "image") and callable(getattr(tab, "pane_contents", None)):
        return _tree_to_entry(spec, work_dir, tab)
    if kind == "figure":
        fig = spec.get("figure")
        try:
            # as_posix: session.json は PC 間を渡る同期資産なので相対パスは常に
            # '/' 区切りで書く（Windows ネイティブ区切りだと POSIX 側の復元で
            # 単一ファイル名扱いになりタブが黙って落ちる）。Windows の pathlib は
            # '/' を解決できるので読み手は無改修・旧 '\\' 入りセッションも
            # Windows 上では従来どおり読める（次回保存で正規化される）。
            rel = Path(fig).relative_to(work_dir).as_posix()
        except (ValueError, TypeError):
            rel = fig
        entry: dict = {"name": spec.get("name"), "kind": "figure", "figure": rel}
    elif kind == "image":
        img = spec.get("image")
        try:
            rel = Path(img).relative_to(work_dir).as_posix()
        except (ValueError, TypeError):
            rel = img
        entry = {"name": spec.get("name"), "kind": "image", "image": rel}
    elif kind == "analysis":
        entry = {
            "name": spec.get("name"),
            "kind": "analysis",
            "module": spec.get("module"),
        }
    else:
        return None

    if tab is not None:
        capture = getattr(tab, "capture_layout", None)
        if callable(capture):
            entry["layout"] = capture()
        if kind == "figure":
            fig2 = getattr(
                getattr(tab, "_panels", {}).get("figure-2", None), "_path", None
            )
            if fig2 is not None:
                try:
                    rel2 = Path(fig2).relative_to(work_dir).as_posix()
                except (ValueError, TypeError):
                    rel2 = str(fig2)
                entry["figure2"] = rel2
            # desync を二度と書かない: figure2 が採れなかった（右ペインが空）のに
            # layout が両可視だと、次回復元で空の分割ペインが出る（観測症状）。figure2
            # が無ければ右ペイン（=図タブでは figure-2 専用、primary は常に左）を必ず
            # 畳んで保存する。left_hidden は触らない。capture_layout 非対応の fake tab は
            # layout キー自体が無いのでスキップ（既存 save テスト回帰なし）。
            if "figure2" not in entry and "layout" in entry:
                entry["layout"] = {**entry["layout"], "right_hidden": True}
        elif kind == "image":
            img2 = getattr(
                getattr(tab, "_panels", {}).get("viewer-2", None), "_path", None
            )
            if img2 is not None:
                try:
                    rel2 = Path(img2).relative_to(work_dir).as_posix()
                except (ValueError, TypeError):
                    rel2 = str(img2)
                entry["image2"] = rel2
            # 画像は復元が収束型（left_hidden から side を選び逆ペインを hide）なので
            # 両可視+image2 なしは復元時に自動で単一へ畳まれる → 保存側正規化は不要
            # （図と非対称。図は apply_layout が layout を忠実再現するため両側が必要）。
    return entry


def _rel(p, work_dir: Path):
    """work_dir 相対（'/' 区切り）へ。相対化できなければ str(p)。

    as_posix: session.json は PC 間を渡る同期資産なので相対パスは常に '/' 区切り。
    """
    try:
        return Path(p).relative_to(work_dir).as_posix()
    except (ValueError, TypeError):
        return str(p)


def _legacy_expressible(panes) -> bool:
    """(slot, kind, path) の列が旧フィールド（figure/figure2・image/image2）で
    表現でき、旧経路の復元で同じ配置に戻るか（Qt 非依存）。"""
    if not panes:
        return False
    kinds = {k for _s, k, _p in panes}
    if len(kinds) != 1:
        return False
    kind = kinds.pop()
    steps = [parse_slot(s) for s, _k, _p in panes]
    if any(len(st) != 1 for st in steps):
        return False
    idxs = {st[0][1] for st in steps}
    if len(idxs) != len(steps):
        return False
    if kind == "figure":
        return idxs in ({0}, {0, 1})
    if kind == "image":
        if idxs == {0, 1}:
            return True
        # 旧経路の単一画像復元は panel=left|right で向きを適用しないので横のみ。
        return steps[0][0][0] == "h"
    return False


def _tree_to_entry(spec: dict, work_dir: Path, tab) -> dict | None:
    """ライブ viewer タブの分割木から session エントリを導出する（Issue #97）。

    旧フィールドで表現できる配置は従来形式（``panes`` 無し）で書き、それ以外は
    ``panes``（slot/kind/path の列）で書く。旧ビルドは ``panes`` を知らず先頭 1 枚
    （``figure``/``image`` フィールド）で縮退表示する。可視ペインが無ければ None。
    """
    panes = [(s, k, p) for (s, k, p) in tab.pane_contents() if p is not None]
    if not panes:
        return None
    capture = getattr(tab, "capture_layout", None)
    layout = capture() if callable(capture) else None
    name = spec.get("name")
    if _legacy_expressible(panes):
        kind = panes[0][1]
        by_idx = {parse_slot(s)[0][1]: p for s, _k, p in panes}
        if kind == "figure":
            entry: dict = {"name": name, "kind": "figure",
                           "figure": _rel(by_idx[0], work_dir)}
            if 1 in by_idx:
                entry["figure2"] = _rel(by_idx[1], work_dir)
            if layout is not None:
                entry["layout"] = layout
                if "figure2" not in entry:
                    entry["layout"] = {**layout, "right_hidden": True}
        else:
            primary = by_idx[0] if 0 in by_idx else by_idx[1]
            entry = {"name": name, "kind": "image",
                     "image": _rel(primary, work_dir)}
            if 0 in by_idx and 1 in by_idx:
                entry["image2"] = _rel(by_idx[1], work_dir)
            if layout is not None:
                entry["layout"] = layout
        return entry
    first_kind = panes[0][1]
    entry = {
        "name": name,
        "kind": first_kind,
        first_kind: _rel(panes[0][2], work_dir),
        "panes": [
            {"slot": s, "kind": k, "path": _rel(p, work_dir)} for s, k, p in panes
        ],
    }
    if layout is not None:
        # 旧ビルドが先頭 1 枚を左に出せるように left_hidden を False に固定する
        # （新ビルドの panes 復元は hidden フラグを使わない）。
        entry["layout"] = {**layout, "left_hidden": False}
    return entry


def _abs_under(p, work_dir: Path) -> Path:
    fp = Path(p)
    return fp if fp.is_absolute() else work_dir / fp


def _legacy_panes(entry: dict, work_dir: Path) -> list[dict] | None:
    """旧形式エントリを明示 slot＋reuse_key 付きのペイン列へ変換（Qt 非依存）。

    primary のファイルが無ければ None（エントリ skip）。2 枚目は「フィールドが
    あり、かつファイルが存在する」ときだけ split 扱い（旧 will_split と同じ判定）。
    """
    layout = entry.get("layout") or {}
    first, second = (
        ("top", "bottom") if layout.get("orientation") == "vertical"
        else ("left", "right")
    )
    kind = entry.get("kind")
    if kind == "figure":
        k1, k2, base = "figure", "figure2", "figure"
    elif kind == "image":
        k1, k2, base = "image", "image2", "viewer"
    else:
        return None
    prim = entry.get(k1)
    if not prim:
        return None
    abs1 = _abs_under(prim, work_dir)
    if not abs1.is_file():
        return None
    sec = entry.get(k2)
    abs2 = _abs_under(sec, work_dir) if sec else None
    if abs2 is not None and abs2.is_file():
        return [
            {"slot": first, "kind": kind, "path": str(abs1), "reuse_key": base},
            {"slot": second, "kind": kind, "path": str(abs2),
             "reuse_key": f"{base}-2"},
        ]
    if kind == "figure":
        return [{"slot": first, "kind": kind, "path": str(abs1), "reuse_key": base}]
    side = second if layout.get("left_hidden") else first
    return [{"slot": side, "kind": kind, "path": str(abs1), "reuse_key": base}]


def _find_ds_tab(window, name, dataset):
    return next(
        (t for t in window.tabs()
         if t.name == name
         and (getattr(t, "session_spec", None) or {}).get("dataset") == dataset),
        None,
    )


def _restore_panes(window, entry, candidates, work_dir, dataset, t0) -> bool:
    """ペイン列を差分適用で復元する。True なら restored += 1。

    置換範囲の契約: 成功 ≥ 1 件 → タブの bridge ペインは成功したペインちょうど
    （prune_panes）。成功 0 件 → 既存タブは無変更。
    """
    name = entry.get("name")
    adopted: list[tuple] = []
    for pane in candidates:
        try:
            if not isinstance(pane, dict):
                raise ValueError(f"pane is not an object: {pane!r}")
            slot = pane.get("slot")
            if not isinstance(slot, str) or not slot.strip():
                raise ValueError(f"bad slot: {slot!r}")
            steps = parse_slot(slot)
            if not steps:
                raise ValueError(f"bad slot: {slot!r}")
            kind = pane.get("kind")
            if kind not in ("figure", "image"):
                raise ValueError(f"bad kind: {kind!r}")
            path = pane.get("path")
            if not isinstance(path, str) or not path:
                raise ValueError(f"bad path: {path!r}")
            abs_path = _abs_under(path, work_dir)
            if not abs_path.is_file():
                raise FileNotFoundError(str(abs_path))
            if any(conflicts(steps, a[4]) for a in adopted):
                raise ValueError(f"slot {slot!r} conflicts with an earlier pane")
            adopted.append((slot, kind, abs_path, pane.get("reuse_key"), steps))
        except Exception:
            _log.warning(
                "open_dataset: skipping pane %r (tab %r)", pane, name, exc_info=True
            )
    if not adopted:
        return False
    ok: set[str] = set()
    claimed: set[str] = set()
    for slot, kind, abs_path, reuse_key, steps in adopted:
        try:
            if t0 is None:
                window.dispatch_command(
                    "show" if kind == "figure" else "show-image",
                    path=str(abs_path), name=name, slot=slot, dataset=dataset,
                )
            else:
                from llm_bridge import _place_viewer
                key, _w = _place_viewer(
                    t0, slot, kind, abs_path,
                    reuse_key=reuse_key, claimed=frozenset(claimed),
                )
                claimed.add(key)
            ok.add(format_slot(steps))
        except Exception:
            _log.warning(
                "open_dataset: failed to restore pane %r (tab %r)", slot, name,
                exc_info=True,
            )
    if not ok:
        return False
    t = _find_ds_tab(window, name, dataset)
    if t is not None:
        if hasattr(t, "prune_panes"):
            t.prune_panes(ok)
        layout = entry.get("layout")
        if isinstance(layout, dict) and hasattr(t, "apply_layout"):
            t.apply_layout({
                k: v for k, v in layout.items()
                if k not in ("left_hidden", "right_hidden")
            })
        if hasattr(t, "tidy"):
            t.tidy()
    return True


def _resolve_work_dir_readonly(dataset: str) -> Path:
    """Resolve a dataset's work_dir WITHOUT side effects (no toml/dir creation).

    Delegates to dataset_config.get_work_dir(create=False): validation +
    resolution only, no ensure_config() / mkdir(). Does not absorb exceptions —
    KeyError (unregistered dataset), RuntimeError (no host path), ValueError (bad
    work_dir) all propagate to the caller for individual handling.
    """
    return dataset_config.get_work_dir(dataset, create=False)


def read_session(dataset: str) -> dict | None:
    """Read <work_dir>/session.json (durable: primary → `.bak` fallback).

    Returns the session dict, or None when there is genuinely no session
    (work_dir resolution failure, or primary AND `.bak` both absent). A primary
    that exists but cannot be parsed — and cannot be recovered from `.bak` —
    raises SessionUnreadableError instead of returning None: callers must not
    treat a corrupt session.json (0-byte truncation on the synced mount) as
    "no session".
    """
    try:
        work_dir = _resolve_work_dir_readonly(dataset)
    except Exception:
        return None
    path = work_dir / "session.json"
    status, data = durable_read_json(path)
    if status == "ok":
        return data
    if status == "recovered":
        # primary は次回の write_session（durable_write_json）が自己修復する。
        _log.warning("read_session: %s unreadable; recovered from .bak", path)
        return data
    if status == "absent":
        # durable_read_json の 'absent' は「primary が本当に無い」だけで、.bak が
        # 『存在するのに読めない』ケースも丸め込む（common/paths.py）。それを
        # no-session にすると、evict で primary が消え .bak が transient に
        # 読めないだけの状態を「セッション無し」と誤読し、次の保存が .bak を
        # 上書きして最後の復旧材料を潰す。存在判定できない場合も安全側（raise）。
        try:
            bak_present = bak_path(path).exists()
        except OSError:
            bak_present = True
        if bak_present:
            raise SessionUnreadableError(
                f"session.json for {dataset!r} is absent but its .bak exists "
                f"and is unreadable (transient mount failure?): {path}"
            )
        return None
    raise SessionUnreadableError(
        f"session.json for {dataset!r} exists but is unreadable "
        f"(0-byte/corrupt; .bak unusable): {path}"
    )


def write_session(dataset: str, payload: dict) -> None:
    """Durably write <work_dir>/session.json and verify (side-effecting resolve).

    durable_write_json = primary + `.bak` の 2 コピー。Issue #96 以降、各コピーの
    read-back 検証とリトライは書込 chokepoint（common.paths）が担うので、ここは
    「2 コピーが揃って正しく読み戻せる」という**最終的な整合性**だけを確認する。
    primary が 'ok' で読めない（= 'recovered' に落ちる）のもマウント異常のサインなので
    失敗扱い。`.bak` 単独の劣化も二重化の黙った喪失なので失敗扱い。比較は JSON 正規化後
    （非 JSON 型の混入で偽陽性の保存失敗を出さないため）と `_seq` を除いた内容で行う。
    """
    work_dir = dataset_config.get_work_dir(dataset)
    target = work_dir / "session.json"
    durable_write_json(target, payload)
    normalized = json.loads(json.dumps(payload, ensure_ascii=False))
    status, data = durable_read_json(target)
    if status != "ok" or data != normalized:
        raise SessionPersistError(
            f"session.json write verification failed for {dataset!r} "
            f"(read-back status={status}): {target}"
        )
    # .bak も個別に read-back する: durable_read_json は 2 コピーのうち新しい方を
    # 返すので、.bak 側だけが劣化しても primary が 'ok' なら気づけない。
    bstatus, bdata = read_json_classified(bak_path(target))
    if bstatus != "ok" or strip_seq(bdata) != normalized:
        raise SessionPersistError(
            f"session.json .bak write verification failed for {dataset!r} "
            f"(read-back status={bstatus}): {bak_path(target)}"
        )


def save_all(window) -> tuple[list[str], list[str]]:
    """Save every dataset's session from the window's tracked tabs.

    Returns (saved, failed): dataset names saved successfully and those that
    raised. Clears window dirty only if `failed` is empty.

    New side effect: rebuilds each saved dataset's `<dataset_dir>/meta.json`
    (rebuild_meta, heavy=False) as a ride-along — does not affect saved/failed
    or the dirty-clear gate.
    """
    # 1. Group session-tracked tabs by dataset, preserving tab order.
    grouped: dict[str, list[tuple]] = {}
    for tab in window.tabs():
        spec = getattr(tab, "session_spec", None)
        if not spec:
            continue
        ds = spec.get("dataset")
        if ds is None:
            continue
        grouped.setdefault(ds, []).append((spec, tab))

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
            for spec, tab in specs:
                entry = _spec_to_tab(spec, work_dir, tab)
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
        pairs = [
            (spec, tab) for tab in window.tabs()
            if (spec := getattr(tab, "session_spec", None))
            and spec.get("dataset") == dataset
        ]
        if not pairs:
            return True   # zero tabs → do not persist an empty layout
        active = window.active_tab()
        active_name = active.name if active is not None else None
        work_dir = dataset_config.get_work_dir(dataset)
        tabs: list[dict] = []
        ds_tab_names: set[str] = set()
        for spec, tab in pairs:
            entry = _spec_to_tab(spec, work_dir, tab)
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
    (including `no-session:` and `unreadable-session:`), not just `restored:N`.

    Result strings: `restored:N` / `no-session:<ds>` / `unreadable-session:<ds>`
    (session.json exists but is corrupt and `.bak` cannot recover it — the file
    is left untouched, tabs are not restored, the dataset still opens) /
    `error:<ds>`.

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

        unreadable = False
        try:
            sess = read_session(dataset)
        except SessionUnreadableError:
            # 破損 session.json は上書きも削除もしない（復旧材料を保全）。DS は
            # 開く（チャット復元・グループ生成は下の非 error 経路が担う）。
            _log.warning(
                "open_dataset: unreadable session.json for %r (left untouched)",
                dataset, exc_info=True,
            )
            sess = None
            unreadable = True
        if sess is None:
            result = (
                f"unreadable-session:{dataset}" if unreadable
                else f"no-session:{dataset}"
            )
        else:
            restored = 0
            for entry in sess.get("tabs", []):
                try:
                    fig2_shown = False
                    img2_shown = False
                    kind = entry.get("kind")
                    if kind in ("figure", "image"):
                        name = entry.get("name")
                        t0 = _find_ds_tab(window, name, dataset)
                        if t0 is not None:
                            from llm_bridge import _is_viewer_host
                            if not _is_viewer_host(t0):
                                # 同名の解析タブを潰さない。
                                _log.warning(
                                    "open_dataset: tab %r exists but is not a "
                                    "viewer tab; skipped", name,
                                )
                                continue
                        panes = entry.get("panes")
                        cands = None
                        if isinstance(panes, list):
                            # reuse_key は _legacy_panes 由来のときだけ有効（session.json
                            # に書かれていても無視する）。
                            cands = [
                                {k: v for k, v in pn.items() if k != "reuse_key"}
                                if isinstance(pn, dict) else pn
                                for pn in panes
                            ]
                        elif t0 is not None and (
                            entry.get("layout")
                            or entry.get("figure2" if kind == "figure" else "image2")
                        ):
                            cands = _legacy_panes(entry, work_dir)
                            if cands is None:
                                _log.warning(
                                    "open_dataset: missing %s for tab %r",
                                    kind, name,
                                )
                                continue
                        if cands is not None:
                            if _restore_panes(
                                window, entry, cands, work_dir, dataset, t0
                            ):
                                restored += 1
                            continue
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
                        fig2 = entry.get("figure2")
                        if fig2:
                            f2 = Path(fig2)
                            abs2 = f2 if f2.is_absolute() else work_dir / f2
                            if abs2.is_file():
                                orient = (entry.get("layout") or {}).get("orientation")
                                slot = {"horizontal": "right",
                                        "vertical": "bottom"}.get(orient, "right")
                                window.dispatch_command(
                                    "show",
                                    path=str(abs2),
                                    name=entry.get("name"),
                                    slot=slot,
                                    dataset=dataset,
                                )
                                fig2_shown = True
                            else:
                                _log.warning(
                                    "open_dataset: missing figure2 %s (tab %r)",
                                    abs2, entry.get("name"),
                                )
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
                        # 2枚目(image2)を出せるなら split。その場合 primary は左固定、
                        # 出せない（欠落/ファイル欠損）なら記録側(left_hidden)へ収束。
                        img2 = entry.get("image2")
                        abs2 = None
                        if img2:
                            i2 = Path(img2)
                            abs2 = i2 if i2.is_absolute() else work_dir / i2
                        will_split = abs2 is not None and abs2.is_file()
                        panel = "left" if will_split else (
                            "right" if (entry.get("layout") or {}).get("left_hidden")
                            else "left"
                        )
                        window.dispatch_command(
                            "show-image",
                            path=str(abs_path),
                            name=entry.get("name"),
                            panel=panel,
                            dataset=dataset,
                        )
                        restored += 1
                        if will_split:
                            orient = (entry.get("layout") or {}).get("orientation")
                            slot = {"horizontal": "right",
                                    "vertical": "bottom"}.get(orient, "right")
                            window.dispatch_command(
                                "show-image",
                                path=str(abs2),
                                name=entry.get("name"),
                                slot=slot,
                                dataset=dataset,
                            )
                            img2_shown = True
                        elif img2:
                            _log.warning(
                                "open_dataset: missing image2 %s (tab %r)",
                                abs2, entry.get("name"),
                            )
                    elif kind == "analysis":
                        window.dispatch_command(
                            "add-tab", name=entry.get("module"), dataset=dataset
                        )
                        restored += 1

                    layout = entry.get("layout")
                    t = next(
                        (t for t in window.tabs()
                         if t.name == entry.get("name")
                         and (getattr(t, "session_spec", None) or {}).get(
                             "dataset") == dataset),
                        None,
                    )
                    if kind in ("figure", "analysis"):
                        if layout and t is not None and hasattr(t, "apply_layout"):
                            if kind == "figure" and not fig2_shown:
                                # 2枚目を出せなかった図タブ（figure2 欠落 null／ファイル
                                # 欠損の双方）は右ペイン（slot=right/bottom の 2 枚目は
                                # 向きに依らず root 第 2 子）を必ず畳む。
                                # これが無いと「両可視 layout + figure2 なし」の desync
                                # session がそのまま空の分割ペインで復元される（観測症状）。
                                # primary 欠損が continue でエントリ全体を捨てるのと対称に、
                                # 単一図の見た目へ落とす。analysis kind は対象外。
                                layout = {**layout, "right_hidden": True}
                            t.apply_layout(layout)
                    elif kind == "image":
                        if img2_shown:
                            # 2枚 split: 図と同じく layout を忠実復元（両可視・向き・sizes）。
                            if layout and t is not None and hasattr(t, "apply_layout"):
                                t.apply_layout(layout)
                        elif layout and t is not None and hasattr(t, "move_panel"):
                            # 単一 viewer: 記録側へ viewer を移設して配置を収束させる
                            # （reviewer round3 P1）。両可視+image2 なしもここで side=left・
                            # 逆ペイン hide で自動的に単一へ畳まれる（A-1 相当）。旧
                            # session=layout 無しは既存側のまま＝回帰なし。
                            side = "right" if layout.get("left_hidden") else "left"
                            other = "left" if side == "right" else "right"
                            t.move_panel("viewer", side)
                            t.set_pane_visible(side, True)
                            t.set_pane_visible(other, False)
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
