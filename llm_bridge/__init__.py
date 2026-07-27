"""LLM bridge: GUI ↔ LLM via filesystem + CLI.

Public entry points (called from GUI side):
- attach_window(window) → starts command watcher, wires window-level verbs, tracks active tab
- attach_tab(tab, state_provider) → wires snapshot/state/annotations for a tab

Both return iterables of QFileSystemWatcher etc. — caller must hold references.
"""

import contextlib
import contextvars
import importlib.util
import json
import shlex
from collections.abc import Callable
from typing import TYPE_CHECKING
import dataset_config
from common.paths import safe_resolve, bak_path, backup_text_if_changed

if TYPE_CHECKING:
    from gui.window import ToolWindow
from llm_bridge import state, snapshots, commands, annotations, session
from llm_bridge.paths import active_state_path


_building_dataset: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_building_dataset", default=None
)


def _flag(v) -> bool:
    """CLI kv フラグを厳格に bool 化。_parse_kvs は int/float/str を返すので
    1/0・true/false・yes/no・on/off を受理し、それ以外は ValueError。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        if v in (0, 1):
            return bool(v)
        raise ValueError(f"invalid boolean flag: {v!r} (use true/false)")
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off", ""):
        return False
    raise ValueError(f"invalid boolean flag: {v!r} (use true/false)")


@contextlib.contextmanager
def _building(dataset: str | None):
    """build_tab 実行中だけ dataset を contextvar に立てる。テスト/将来の再利用用に公開。"""
    token = _building_dataset.set(dataset)
    try:
        yield
    finally:
        _building_dataset.reset(token)


def _write_active(window) -> None:
    tab = window.active_tab()
    name = tab.name if tab is not None else None
    current = getattr(window, "current_dataset", None)          # 明示選択された dataset
    # active_analysis_dataset: active タブが解析タブのときだけその dataset。
    # figure/viewer/dataset-less は None（同名 viewer による誤読を防ぐ）。
    active_analysis_ds = None
    if tab is not None:
        spec = getattr(tab, "session_spec", None)
        if isinstance(spec, dict) and spec.get("kind") == "analysis":
            active_analysis_ds = spec.get("dataset")
    # open_datasets: workspace membership from the group registry (includes
    # zero-tab datasets). active_dataset: the explicitly-selected current dataset
    # (same value as legacy `dataset`, kept for a clearer agent-facing name).
    open_datasets = list(getattr(window, "open_dataset_names", lambda: [])())
    p = active_state_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(
            {"active_tab": name, "dataset": current,
             "active_analysis_dataset": active_analysis_ds,
             "open_datasets": open_datasets, "active_dataset": current},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tmp.replace(p)


def _find_in_dataset(window, name: str, dataset):
    """Find a tab by (name, dataset). Uses window.find_tab when present, else a
    bare-scan fallback for headless/duck windows without the group model."""
    finder = getattr(window, "find_tab", None)
    if finder is not None:
        try:
            return finder(name, dataset)
        except LookupError:
            return None
    for t in window.tabs():
        if getattr(t, "name", None) != name:
            continue
        spec = getattr(t, "session_spec", None) or {}
        ds = spec.get("dataset") or getattr(t, "dataset", None)
        if dataset is None or ds == dataset:
            return t
    return None


def _set_active_tab(window, name: str, dataset=None) -> bool:
    """Focus a tab, passing dataset= when the window supports it (group model)."""
    try:
        return window.set_active_tab(name, dataset=dataset)
    except TypeError:
        return window.set_active_tab(name)


def _resolve_analysis_file(dataset, name: str):
    """Resolve <dataset_dir>/analyses/<name>/analysis.py, or raise.

    Validation/containment is handled by dataset_config.analysis_file; this adds
    the existence check.
    """
    f = dataset_config.analysis_file(dataset, name)
    if not f.is_file():
        raise LookupError(f"no analysis named {name!r} under {dataset!r}")
    return f


def _build_analysis(parent, dataset, name: str):
    """Fresh-import analyses/<name>/analysis.py, run load() + build_tab(parent),
    and assign session_spec. Returns ``(tab, mod, source)``.

    Shared by the `add-tab` verb and the hot-reload Tier 2 `reload_tab` path.
    `parent` is the Qt parent widget for the constructed tab (the live window
    for add-tab, a sandbox QWidget when reloading so a failed build can be
    discarded as a unit).

    Does NOT call `window.add_tab` or `session.note_dataset` — those are
    side effects the caller applies only AFTER a successful insert, so a build
    failure can't leave a stale `_touched` entry that makes `save_all`
    overwrite a dataset's session with empty tabs.

    Re-import behavior: the analysis module is NOT registered in sys.modules.
    Each call re-executes the file and re-runs `load()`. Analyses that want
    caching should memoize inside `load()` themselves.
    """
    analysis_file = _resolve_analysis_file(dataset, name)
    spec = importlib.util.spec_from_file_location(
        f"_llm_bridge_analysis_{name}", analysis_file
    )
    if spec is None:
        raise ImportError(f"could not build module spec for {analysis_file}")
    mod = importlib.util.module_from_spec(spec)
    # Compile + exec from source bytes directly rather than spec.loader.exec_module:
    # the loader may read a stale .pyc when an edit and a reload land in the same
    # filesystem-mtime second (the exact agent edit-then-reload pattern). The
    # module is never registered in sys.modules, so it's always a fresh build.
    source = analysis_file.read_text(encoding="utf-8")
    if not source:   # 真の 0 バイト（Edit 失敗の truncate）だけを診断対象にする
        bp = bak_path(analysis_file)
        try:
            bak_ok = bool(bp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            bak_ok = False
        if bak_ok:
            raise ValueError(
                f"analyses/{name}/analysis.py が空です（0 バイト — 同期ドライブへの"
                f"書き込み失敗の可能性）。バックアップから復旧できます: "
                f"python -m llm_bridge recover-analysis "
                f"{shlex.quote(name)} --dataset {shlex.quote(dataset)}"
            )
        raise ValueError(
            f"analyses/{name}/analysis.py が空です（0 バイト）。バックアップ"
            f"（{bp}）も見つかりません。チャット履歴・git 等から内容を復元してください。"
        )
    code = compile(source, str(analysis_file), "exec")
    exec(code, mod.__dict__)
    if not hasattr(mod, "build_tab"):
        raise AttributeError(
            f"analyses/{name}/analysis.py has no build_tab(parent, data)"
        )
    data = mod.load() if hasattr(mod, "load") else None
    # dataset を contextvar 経由で attach_tab に供給する（build_tab 同期実行中のみ）。
    # mod.load() は mod.DATASET を使うので囲まない（前提 2）。
    with _building(dataset):
        tab = mod.build_tab(parent, data)
    # Assign session_spec BEFORE the tab is inserted so the currentChanged that
    # add_tab fires sees the final spec (→ chat gets the right dataset).
    # 所在 dataset ＝ 呼び出し側が解決済みの登録名（前提 2）。
    tab.session_spec = {
        "kind": "analysis", "name": name, "module": name, "dataset": dataset,
    }
    return tab, mod, source


def _make_add_tab_handler(window) -> Callable[..., str]:
    """Build the `add-tab` window verb handler.

    Loads analyses/<name>/analysis.py by file path (no __init__.py required —
    analyses are loose directories, not Python packages).

    Contract with Layer 4 (analyses/<name>/analysis.py):
      - module must define `build_tab(parent, data) -> AnalysisTab`
      - module SHOULD define `load() -> Any` (called once if present, else `data=None`)
      - `build_tab` is responsible for invoking `llm_bridge.attach_tab(tab, provider)`
        to wire its own state/snapshot/annotations. The window verb does not wire
        the tab itself.

    Idempotent: if a tab with `name` is already present, focuses it and returns
    `"already-present:<name>"` instead of constructing a duplicate.
    """

    def _add_tab(name: str, dataset: str | None = None) -> str:
        if dataset is None:
            dataset = window.current_dataset
            if dataset is None:
                raise ValueError("no dataset open; cannot resolve analysis")
        # Resolve/validate up front so a bad name reports before any build.
        _resolve_analysis_file(dataset, name)
        # 同名タブのガードは (dataset, name) 単位（タブ ID はグループ内で一意）。
        # 同一 (dataset, name) の解析タブは focus して already-present を返す。
        # 別 dataset の同名解析は「開いてよい」— その dataset のグループに新規タブを
        # 作る（本 Issue #51 の看板動線）。同一 dataset・同名の figure/viewer タブ
        # （kind!=analysis）は解析として開けないので fail-fast する。
        existing = _find_in_dataset(window, name, dataset)
        if existing is not None:
            ex_spec = getattr(existing, "session_spec", None) or {}
            ex_kind = ex_spec.get("kind")
            # (existing is already resolved within `dataset` by _find_in_dataset,
            # so its dataset is `dataset` — no need to recompute it here.)
            if ex_kind == "analysis":
                _set_active_tab(window, name, dataset=dataset)
                return f"already-present:{name}"
            raise ValueError(
                f"tab {name!r} is already open in dataset {dataset!r} "
                f"(kind={ex_kind!r}); cannot open analysis {name!r} there "
                f"(name is the tab id within a dataset group)"
            )
        from PySide6.QtWidgets import QWidget

        # Build under a sandbox parent so a partially-constructed tab (and any
        # watchers it spawned) are tracked as the sandbox's children and torn
        # down as a unit on failure. build_tab side-effects state.json (via its
        # internal refresh-state), so capture the prior state to restore on any
        # failure / no-op path — the real UI read-back is the source of truth.
        # Classified capture: if current.json is transiently unreadable on the
        # mount (0-byte/未同期), do NOT restore-write the {} we'd otherwise get —
        # persisting it would erase real tab state. Guarded at each restore site.
        captured_status, captured_state = state.read_status(dataset, name)
        sandbox = QWidget()
        try:
            new_tab, mod, source = _build_analysis(sandbox, dataset, name)
        except Exception:
            sandbox.deleteLater()
            if captured_status != "unreadable":   # transient miss は復元しない（既存を温存）
                state.writer(dataset, name)(captured_state)
            raise
        if new_tab.name != name:
            sandbox.deleteLater()
            if captured_status != "unreadable":   # transient miss は復元しない（既存を温存）
                state.writer(dataset, name)(captured_state)
            raise ValueError(
                f"tab name mismatch: expected {name!r}, got {new_tab.name!r}"
            )
        if not _sync_new_tab_state(new_tab, mod, dataset, name, captured_state):
            sandbox.deleteLater()
            if captured_status != "unreadable":   # transient miss は復元しない（既存を温存）
                state.writer(dataset, name)(captured_state)
            raise RuntimeError(
                f"could not establish state for {name!r} (apply_state / "
                f"refresh-state failed); tab not inserted"
            )
        new_tab.setParent(None)
        sandbox.deleteLater()
        window.add_tab(new_tab)
        # 新規タブも focus して current_dataset を同期する（idempotent 経路・_show と
        # 対称）。window.add_tab は _ensure_group+addTab のみで current_dataset を
        # 動かさないため、これが無いと active.json が dataset:null のまま publish され
        # （active タブは current group 内という不変条件に反する）、直後の dataset 省略
        # add-tab が current_dataset=None で ValueError になり看板動線が degrade する。
        _set_active_tab(window, name, dataset=dataset)
        # note_dataset only AFTER a successful insert (see _build_analysis doc).
        spec = getattr(new_tab, "session_spec", None)
        if spec and spec.get("dataset"):
            session.note_dataset(spec["dataset"])
        # ホットリロード baseline をこの時点の SHA で登録（add-tab・session 復元の
        # 両方をカバー）。タブは既に add 済みなので best-effort（reviewer P2 R4）。
        hr = getattr(window, "_hotreload", None)
        if hr is not None and hasattr(hr, "note_analysis_opened"):
            try:
                hr.note_analysis_opened(dataset, name)
            except Exception:
                pass
        # 最後まで成立した成功経路でのみ last-good を .bak に退避（best-effort）。
        # suppress は Exception まで広げる: パス再解決（dataset_config.analysis_file は
        # KeyError/RuntimeError もあり得る）含め、この副作用が add-tab の成功を巻き添えに
        # しないため（reviewer R2 P2）。純粋な副作用なので fail-open が正。
        with contextlib.suppress(Exception):
            backup_text_if_changed(
                dataset_config.analysis_file(dataset, name), source
            )
        return f"added:{name}"

    return _add_tab


def _sync_new_tab_state(tab, mod, dataset, name: str, captured_state: dict) -> bool:
    """Apply optional `apply_state` then sync the real UI state into state.json.

    Returns True on success, False if apply_state or refresh-state raised (the
    caller then discards the tab and restores captured_state). The read-back
    from refresh-state — not captured_state — is always the truth on success:
      - apply_state present & restores state → state.json = captured = UI
      - apply_state no-op / absent          → state.json = fresh   = UI
    """
    if hasattr(mod, "apply_state"):
        try:
            mod.apply_state(tab, captured_state)
        except Exception:
            return False
    try:
        tab.dispatch_command("refresh-state")
    except Exception:
        return False
    return True


# slot → (panel key, container position, split orientation or None).
# None (no slot) = primary pane, single full-width pane (no split axis).
_SLOT_MAP = {
    None: ("figure", "left", None),
    "left": ("figure", "left", "horizontal"),
    "top": ("figure", "left", "vertical"),
    "right": ("figure-2", "right", "horizontal"),
    "bottom": ("figure-2", "right", "vertical"),
}

# show-image 版: slot=None は単一 primary（viewer, panel= で左右指定可）、
# slot=right/bottom で 2枚目 viewer-2 を反対ペインへ並置し水平/垂直分割。
# _SLOT_MAP（図）と違い left/top は無い（画像 primary の側は panel= で指定）。
_IMG_SLOT_MAP = {
    None: ("viewer", "left", None),
    "right": ("viewer-2", "right", "horizontal"),
    "bottom": ("viewer-2", "right", "vertical"),
}


def _make_show_handler(window: "ToolWindow") -> Callable[..., str]:
    """Build the `show` window verb handler.

    Displays an arbitrary image file (PNG etc.) in a generic viewer tab —
    no analysis module required. The intended flow: Claude saves a figure
    via `common.explore.save_fig` (→ absolute path) then `show`s that path.

    Default (no `slot`) = single full-width pane. Passing
    `slot=left|right|top|bottom` places a second figure in the opposite pane and
    splits horizontally (left/right) or vertically (top/bottom). Re-showing
    without `slot` collapses any existing split back to a single full-width pane.

    Existing-tab handling is structure-based, not provenance-based: a tab is
    treated as a show-viewer if it has a `FigurePanel` under key "figure" or
    "figure-2". Any such tab (even a normal analysis tab) is updated in place; a
    tab with neither key (or a non-FigurePanel under them) raises LookupError
    rather than being silently destroyed.
    """

    def _show(
        path: str,
        name: str = "viewer",
        slot: str | None = None,
        dataset: str | None = None,
    ) -> str:
        p = safe_resolve(path)
        if not p.is_file():
            raise LookupError(f"not a file: {path}")
        if slot not in _SLOT_MAP:
            raise ValueError(f"invalid slot: {slot!r}")
        panel_key, container_position, orientation = _SLOT_MAP[slot]
        from gui.panels import (
            FigurePanel,
        )  # 関数内 import（CLI に PySide6 を引き込まない）

        # Locate an existing tab by (dataset, name) WITHOUT activating it —
        # separating the existence check from activation so that, when we finally
        # activate, the currentChanged it fires sees the NEW session_spec
        # (avoiding a stale dataset push to the chat widget). Resolving by
        # (dataset, name) keeps a same-named viewer in another dataset from being
        # absorbed/overwritten (Issue #51 A1b / #26 reviewer R4).
        existing = _find_in_dataset(window, name, dataset)
        if existing is not None:
            tab = existing
            is_viewer = any(
                isinstance(tab._panels.get(k), FigurePanel)
                for k in ("figure", "figure-2")
            )
            if not is_viewer:
                raise LookupError(
                    f"tab {name!r} exists but is not a show-viewer tab"
                )
            pane_restored = False
            new_panel_added = False
            if panel_key in tab._panels:
                fig = tab.panel(panel_key)
                if not isinstance(fig, FigurePanel):
                    raise LookupError(
                        f"tab {name!r} exists but is not a show-viewer tab"
                    )
                fig.set_path(p)
                if orientation is not None:
                    container = (
                        tab._left_container
                        if container_position == "left"
                        else tab._right_container
                    )
                    if container.isHidden():
                        tab.set_pane_visible(container_position, True)
                        pane_restored = True
            else:
                panel = FigurePanel()
                tab.add_panel(panel_key, panel, container_position, stretch=1)
                panel.set_path(p)
                tab.set_pane_visible(container_position, True)
                new_panel_added = True
            if orientation is not None:
                tab.set_split_orientation(orientation)
            both_visible = (
                not tab._left_container.isHidden()
                and not tab._right_container.isHidden()
            )
            if orientation is not None and both_visible and (
                new_panel_added or pane_restored
            ):
                tab.set_split_ratio(1, 1)
            if orientation is None:
                tab.set_pane_visible("right", False)
            # session_spec is only touched for the primary pane ("figure").
            # figure-2 (slot=right/bottom) must NOT overwrite the primary spec.
            if panel_key == "figure":
                if dataset is not None:
                    ds = str(dataset)
                    tab.session_spec = {"kind": "figure", "name": name, "dataset": ds, "figure": str(p)}
                    session.note_dataset(ds)
                    window.mark_session_dirty()
                else:
                    inferred = session.infer_dataset(str(p))
                    if inferred is not None:
                        tab.session_spec = {"kind": "figure", "name": name, "dataset": inferred, "figure": str(p)}
                        session.note_dataset(inferred)
                        window.mark_session_dirty()
            elif (getattr(tab, "session_spec", None) or {}).get("dataset"):
                # figure-2（slot=right/bottom）追加/更新は session_spec を触らないが、
                # 分割の合成/幾何は保存時にライブタブから導出される（figure2 パス +
                # capture_layout）。上の figure 分岐だけでは復元/保存後に足した分割が
                # dirty にならず closeEvent で保存されない。永続化可能な viewer なら dirty。
                window.mark_session_dirty()
            # Activate now — spec is final, so this focus surfaces the right
            # dataset group + chat. Pass the resolved dataset so a same-named tab
            # in another dataset is never focused instead.
            _set_active_tab(
                window, name,
                dataset=(getattr(tab, "session_spec", None) or {}).get("dataset"),
            )
            # Backstop when the tab was already current (no currentChanged fired).
            getattr(window, "notify_chat_dataset", lambda: None)()
            return f"updated:{name}"
        from gui.tab import (
            AnalysisTab,
        )  # 関数内 import（CLI に PySide6 を引き込まない）

        tab = AnalysisTab(name)
        tab.set_pane_visible("left", False)
        tab.set_pane_visible("right", False)
        panel = FigurePanel()
        tab.add_panel(panel_key, panel, container_position, stretch=1)
        tab.set_pane_visible(container_position, True)
        if orientation is not None:
            tab.set_split_orientation(orientation)
        def _set_split(left, right):
            tab.set_split_ratio(float(left), float(right))
            window.mark_session_dirty()   # 成功後のみ（set_split_ratio は ValueError を投げ得る）
        tab.register_command("set-split", _set_split)
        tab.register_command("snapshot", lambda: None)
        # Assign session_spec/note_dataset BEFORE add_tab so the currentChanged
        # that add_tab/set_active_tab fires sees the final spec.
        if panel_key == "figure":
            if dataset is not None:
                ds = str(dataset)
                tab.session_spec = {"kind": "figure", "name": name, "dataset": ds, "figure": str(p)}
                session.note_dataset(ds)
            else:
                inferred = session.infer_dataset(str(p))
                if inferred is not None:
                    tab.session_spec = {"kind": "figure", "name": name, "dataset": inferred, "figure": str(p)}
                    session.note_dataset(inferred)
        window.add_tab(tab)
        _set_active_tab(
            window, name,
            dataset=(getattr(tab, "session_spec", None) or {}).get("dataset"),
        )
        panel.set_path(p)
        return f"shown:{name}"

    return _show


def _make_show_image_handler(window: "ToolWindow") -> Callable[..., str]:
    """Build the `show-image` window verb handler.

    Opens a raw/source image (TIFF/16bit/stack/multi-channel) in an interactive
    ImageViewerPanel tab — the ImageJ-style onramp (Issue #60). Mirrors
    `_make_show_handler` (see its docstring) but targets ImageViewerPanel under
    key "viewer"; the primary pane defaults to "left".

    Existing-tab handling is structure-based: a tab is a viewer iff it holds an
    ImageViewerPanel under key "viewer". A name collision with a non-viewer tab
    raises LookupError rather than clobbering it (static `show`/FigurePanel path
    is preserved without regression).
    """

    def _show_image(
        path: str,
        name: str = "viewer",
        panel: str = "left",
        slot: str | None = None,
        dataset: str | None = None,
    ) -> str:
        if panel not in ("left", "right"):
            raise ValueError(f"panel must be 'left' or 'right', got {panel!r}")
        if slot not in _IMG_SLOT_MAP:
            raise ValueError(f"invalid slot: {slot!r} (use right/bottom to split)")
        panel_key, _container_position, orientation = _IMG_SLOT_MAP[slot]
        # slot 指定時は 2枚目(viewer-2)が対象・primary は左固定・panel は無視（決定的）。
        primary_side = "left" if slot is not None else panel
        p = safe_resolve(path)
        if not p.is_file():
            raise LookupError(f"not a file: {path}")
        from gui.imageviewer import (
            ImageViewerPanel,
            attach_image_viewer,
        )  # 関数内 import（CLI に PySide6 を引き込まない）

        existing = _find_in_dataset(window, name, dataset)
        if existing is not None:
            tab = existing
            if not isinstance(tab._panels.get("viewer"), ImageViewerPanel):
                raise LookupError(
                    f"tab {name!r} is not an image-viewer tab"
                )
            if panel_key == "viewer":
                tab.panel("viewer").set_image(p)
                # session_spec / note_dataset / dirty only when a dataset resolves.
                if dataset is not None:
                    ds = str(dataset)
                    tab.session_spec = {"kind": "image", "name": name, "dataset": ds, "image": str(p)}
                    session.note_dataset(ds)
                    window.mark_session_dirty()
                else:
                    inferred = session.infer_dataset(str(p))
                    if inferred is not None:
                        tab.session_spec = {"kind": "image", "name": name, "dataset": inferred, "image": str(p)}
                        session.note_dataset(inferred)
                        window.mark_session_dirty()
            else:
                # 2枚目(viewer-2)を並置。primary の session_spec は触らない（figure-2 と
                # 対称）。attach_image_viewer は使わない: _register_viewer_verbs が
                # set-lut 等をタブ単位で再登録し 1枚目の verb ルーティングを clobber する。
                new_v2 = not isinstance(tab._panels.get("viewer-2"), ImageViewerPanel)
                if new_v2:
                    p2 = ImageViewerPanel()
                    tab.add_panel("viewer-2", p2, "right", stretch=1)
                    p2.set_image(p)
                else:
                    tab.panel("viewer-2").set_image(p)
                tab.set_split_orientation(orientation)
                tab.set_pane_visible("right", True)
                if new_v2 and (
                    not tab._left_container.isHidden()
                    and not tab._right_container.isHidden()
                ):
                    tab.set_split_ratio(1, 1)
                if (getattr(tab, "session_spec", None) or {}).get("dataset"):
                    window.mark_session_dirty()
            _set_active_tab(
                window, name,
                dataset=(getattr(tab, "session_spec", None) or {}).get("dataset"),
            )
            getattr(window, "notify_chat_dataset", lambda: None)()
            return f"updated:{name}"

        from gui.tab import (
            AnalysisTab,
        )  # 関数内 import（CLI に PySide6 を引き込まない）

        tab = AnalysisTab(name)
        tab.set_pane_visible("left", False)
        tab.set_pane_visible("right", False)
        # 新規タブは primary のみ（primary 無しに 2枚目は作れない）。slot 指定時は
        # primary_side=left。まず primary を show してから slot で 2枚目を並置する。
        attach_image_viewer(tab, p, panel=primary_side)
        tab.set_pane_visible(primary_side, True)
        def _set_split(left, right):
            tab.set_split_ratio(float(left), float(right))
            window.mark_session_dirty()   # 成功後のみ（set_split_ratio は ValueError を投げ得る）
        tab.register_command("set-split", _set_split)
        tab.register_command("snapshot", lambda: None)
        # Assign session_spec/note_dataset BEFORE add_tab so the currentChanged
        # that add_tab/set_active_tab fires sees the final spec. ds None → no
        # session_spec (volatile tab), same as `show`.
        if dataset is not None:
            ds = str(dataset)
            tab.session_spec = {"kind": "image", "name": name, "dataset": ds, "image": str(p)}
            session.note_dataset(ds)
        else:
            inferred = session.infer_dataset(str(p))
            if inferred is not None:
                tab.session_spec = {"kind": "image", "name": name, "dataset": inferred, "image": str(p)}
                session.note_dataset(inferred)
        window.add_tab(tab)
        _set_active_tab(
            window, name,
            dataset=(getattr(tab, "session_spec", None) or {}).get("dataset"),
        )
        return f"shown:{name}"

    return _show_image


def _list_tabs(window, detail: bool = False):
    """list-tabs handler. detail=false → [name]; detail=true → [{name,dataset,kind}]."""
    if not detail:
        return window.tab_names()
    out = []
    for t in window.tabs():
        spec = getattr(t, "session_spec", None) or {}
        out.append({
            "name": getattr(t, "name", None),
            "dataset": spec.get("dataset"),
            "kind": spec.get("kind"),
        })
    return out


def _do_set_active_dataset(window, name: str) -> str:
    """set-active-dataset / switch-dataset handler. LookupError if not open."""
    if not window.set_active_dataset(name):
        raise LookupError(f"dataset {name!r} is not open")
    return f"active:{name}"


def _do_close_dataset(window, name: str) -> str:
    """close-dataset handler. `closed:<name>:<n>` (n>=0) / `error:<name>` on
    save-failure abort (close_dataset returned -1) / LookupError if not open."""
    n = window.close_dataset(name)
    if n < 0:
        return f"error:{name}"
    return f"closed:{name}:{n}"


def _rewire_window(window) -> None:
    """(Re)register the window-tier verbs + session saver.

    Pulled out of attach_window so the hot-reload `__on_reload__` hook can
    re-run JUST the verb registration (which captures freshly-reloaded handler
    closures) without re-running the one-time wiring that attach_window also
    does. Deliberately EXCLUDES:
      - `tab_changed`/`dataset_changed` connects (re-connecting double-fires;
        the existing lambdas resolve `_write_active` via module globals, so they
        self-heal after a Tier 1 reload — no re-connect needed).
      - `clear_session_dirty()` (would drop a pending dirty flag).
      - `commands.start_watcher` (a second watcher would double-drain).
    """
    window.register_command("add-tab", _make_add_tab_handler(window))
    window.register_command(
        "close-tab", lambda name, dataset=None: window.close_tab(name, dataset=dataset)
    )
    window.register_command("list-tabs", lambda detail=False: _list_tabs(window, detail))
    window.register_command(
        "set-active-tab",
        lambda name, dataset=None: window.set_active_tab(name, dataset=dataset),
    )
    window.register_command("toggle-chat-float", window.toggle_chat_floating)
    window.register_command("show", _make_show_handler(window))
    window.register_command("show-image", _make_show_image_handler(window))
    window.register_command("open-dataset", lambda name: session.open_dataset(window, name))
    # Top-level "open datasets" layer verbs (Issue #51).
    window.register_command(
        "list-open-datasets",
        lambda: {"open": list(window.open_dataset_names()),
                 "active": window.current_dataset},
    )
    _set_active_ds = lambda name: _do_set_active_dataset(window, name)  # noqa: E731
    window.register_command("set-active-dataset", _set_active_ds)
    window.register_command("switch-dataset", _set_active_ds)  # 別名
    window.register_command("close-dataset", lambda name: _do_close_dataset(window, name))
    # Meeting relay verbs (Issue #42). The relay is resolved at call time via
    # `window._meeting_relay` so a hot-reload re-run of _rewire_window picks up
    # the live relay instance.
    window.register_command(
        "chat-inject",
        lambda text, sender, session=None:
            window.chat_widget().inject_remote_message(text, sender, session_id=session)
            or f"injected:{session}",
    )
    window.register_command(
        "chat-list-sessions", lambda: window.chat_widget().session_summaries()
    )
    # meeting-start の lan=true でLAN リンクを併発する。CLI 単独起動は
    # ホスト IP を渡せないため meeting_start が RELAY_LAN_HOST env を読む
    # （未設定で lan=true にすると LAN リンクは空＝トンネル失敗時も非致命分岐に入らない）。
    # 固定ポート要求 RELAY_LAN_PORT は衝突時 OSError で起動失敗（サイレント別ポート化しない）。
    window.register_command(
        "meeting-start",
        lambda ttl_sec=10800, lan=False:
            window._meeting_relay.meeting_start(int(ttl_sec), lan=_flag(lan))
            or window._meeting_relay.current_token()
            or "starting",
    )
    window.register_command(
        "meeting-token",
        lambda: window._meeting_relay.current_token() or window._meeting_relay.share_status(),
    )
    # LAN 配布用: フルディープリンク（http origin）のみを返す。素 LAN トークンは
    # 配らない（mixed-content で http fetch がブロックされるため）。
    window.register_command(
        "meeting-lan-link",
        lambda: window._meeting_relay.lan_link() or "no-lan",
    )
    window.register_command(
        "meeting-stop", lambda: window._meeting_relay.meeting_stop() or "stopped"
    )
    window.set_session_saver(lambda: session.save_all(window))


def __on_reload__(ctx) -> None:
    """Hot-reload (Tier 1) hook: re-register window verbs with freshly-reloaded
    handler closures.

    The registered `_add_tab` / `_show` closures are nested inside
    `_make_add_tab_handler` / `_make_show_handler`. Patching those factories'
    `__code__` does NOT update the already-registered closures (only the
    module-level functions they call via globals self-heal). Re-running
    `_rewire_window` rebuilds the closures from the new code.
    """
    window = getattr(ctx, "window", None)
    if window is not None:
        _rewire_window(window)


def attach_window(window, *, watcher_resume_after: float | None = None) -> list[object]:
    """Wire llm_bridge to a ToolWindow. Returns watchers to keep alive."""
    # Built-in window verbs + session saver (re-runnable on hot reload).
    _rewire_window(window)

    # Active tab tracker.
    window.tab_changed.connect(lambda _i: _write_active(window))
    if hasattr(window, "dataset_changed"):
        window.dataset_changed.connect(lambda _ds: _write_active(window))
    # add-tab/close-dataset on a NON-active dataset don't fire tab_changed/
    # dataset_changed, so active.json's open_datasets would go stale. The window
    # emits open_datasets_changed on those (gui→llm_bridge stays signal-driven,
    # no top-level import / circular-import risk).
    if hasattr(window, "open_datasets_changed"):
        window.open_datasets_changed.connect(lambda: _write_active(window))
    _write_active(window)  # initial write

    window.clear_session_dirty()  # 起動時のプレースホルダ追加等を clean ベースライン化

    # Command queue watcher (drains stale on startup, executes new arrivals).
    # Single call site so a Tier 3 rebuild can't double-start the watcher.
    cmd_watcher = commands.start_watcher(window, resume_after=watcher_resume_after)
    return [cmd_watcher]


def attach_tab(tab, state_provider: Callable[[], dict]) -> list[object]:
    """Wire llm_bridge to an AnalysisTab. Returns watchers to keep alive.

    state_provider: callable returning the current state dict.
    The analysis is responsible for triggering `tab.dispatch_command("refresh-state")`
    on its panel signals to push state updates.

    The owning dataset is supplied via the `_building` contextvar, set by
    `_build_analysis` during `build_tab`. When it is unset (a dataset-less
    demo/placeholder tab, or a direct call not routed through `_build_analysis`),
    persistence is wired as a no-op rather than raising — these tabs are
    synthetic/volatile and not persistence targets (前提 4).
    """
    name = tab.name
    dataset = _building_dataset.get()
    if dataset is None:
        # dataset-less タブ（demo/placeholder、_build_analysis を介さない直接呼び）。
        # 永続化対象が無いので no-op で配線し、watcher は張らない。
        tab.dataset = None
        tab.connect_snapshot_writer(lambda _tab: None)
        tab.register_command(
            "set-split", lambda left, right: tab.set_split_ratio(float(left), float(right))
        )
        tab.register_command("snapshot", lambda: tab.take_snapshot())  # no-op writer
        tab.register_command("refresh-state", lambda: None)
        return []
    tab.dataset = dataset
    state_w = state.writer(dataset, name)
    snap_w = snapshots.writer(dataset, name)

    # snapshot_writer is consumed by tab.take_snapshot() (gui.tab).
    tab.connect_snapshot_writer(snap_w)
    # state_provider is intentionally NOT plumbed via tab.connect_state():
    # gui has no reader for `_state_provider` today. llm_bridge invokes
    # state_provider directly inside the `refresh-state` verb handler below.
    # If gui starts consuming `_state_provider` later, revisit this.

    # Built-in tab verbs
    tab.register_command(
        "set-split", lambda left, right: tab.set_split_ratio(float(left), float(right))
    )
    tab.register_command("snapshot", lambda: tab.take_snapshot())
    tab.register_command("refresh-state", lambda: state_w(state_provider()))

    # Annotations watcher
    ann_watcher = annotations.start_watcher(tab, dataset)
    return [ann_watcher]


__all__ = [
    "state",
    "snapshots",
    "commands",
    "annotations",
    "session",
    "attach_window",
    "attach_tab",
]
