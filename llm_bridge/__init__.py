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
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
import dataset_config

if TYPE_CHECKING:
    from gui.window import ToolWindow
from llm_bridge import state, snapshots, commands, annotations, session
from llm_bridge.paths import active_state_path


_building_dataset: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_building_dataset", default=None
)


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
    current = getattr(window, "current_dataset", None)          # sticky（既存意味）
    # active_analysis_dataset: active タブが解析タブのときだけその dataset。
    # figure/viewer/dataset-less は None（同名 viewer による誤読を防ぐ）。
    active_analysis_ds = None
    if tab is not None:
        spec = getattr(tab, "session_spec", None)
        if isinstance(spec, dict) and spec.get("kind") == "analysis":
            active_analysis_ds = spec.get("dataset")
    p = active_state_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(
            {"active_tab": name, "dataset": current,
             "active_analysis_dataset": active_analysis_ds},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tmp.replace(p)


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
    and assign session_spec. Returns ``(tab, mod)``.

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
    return tab, mod


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
        # 同名タブのガード（前提 1: name はウィンドウ内で単一のタブ ID）。
        # already-present として focus してよいのは「同じ dataset の解析タブ」だけ。
        # 別 dataset の同名解析タブ、または同名の figure/viewer タブ（kind!=analysis）は
        # この解析を開けないので fail-fast する（同名 viewer を解析と取り違えない）。
        existing = next((t for t in window.tabs() if t.name == name), None)
        if existing is not None:
            ex_spec = getattr(existing, "session_spec", None) or {}
            ex_kind = ex_spec.get("kind")
            ex_ds = ex_spec.get("dataset") or getattr(existing, "dataset", None)
            if ex_kind == "analysis" and ex_ds == dataset:
                window.set_active_tab(name)
                return f"already-present:{name}"
            raise ValueError(
                f"tab {name!r} is already open (kind={ex_kind!r}, dataset={ex_ds!r}); "
                f"cannot open analysis {name!r} for dataset {dataset!r} in the same "
                f"window (name is the single tab id)"
            )
        from PySide6.QtWidgets import QWidget

        # Build under a sandbox parent so a partially-constructed tab (and any
        # watchers it spawned) are tracked as the sandbox's children and torn
        # down as a unit on failure. build_tab side-effects state.json (via its
        # internal refresh-state), so capture the prior state to restore on any
        # failure / no-op path — the real UI read-back is the source of truth.
        captured_state = state.read(dataset, name)
        sandbox = QWidget()
        try:
            new_tab, mod = _build_analysis(sandbox, dataset, name)
        except Exception:
            sandbox.deleteLater()
            state.writer(dataset, name)(captured_state)
            raise
        if new_tab.name != name:
            sandbox.deleteLater()
            state.writer(dataset, name)(captured_state)
            raise ValueError(
                f"tab name mismatch: expected {name!r}, got {new_tab.name!r}"
            )
        if not _sync_new_tab_state(new_tab, mod, dataset, name, captured_state):
            sandbox.deleteLater()
            state.writer(dataset, name)(captured_state)
            raise RuntimeError(
                f"could not establish state for {name!r} (apply_state / "
                f"refresh-state failed); tab not inserted"
            )
        new_tab.setParent(None)
        sandbox.deleteLater()
        window.add_tab(new_tab)
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
        p = Path(path).resolve()
        if not p.is_file():
            raise LookupError(f"not a file: {path}")
        if slot not in _SLOT_MAP:
            raise ValueError(f"invalid slot: {slot!r}")
        panel_key, container_position, orientation = _SLOT_MAP[slot]
        from gui.panels import (
            FigurePanel,
        )  # 関数内 import（CLI に PySide6 を引き込まない）

        # Locate an existing tab by name WITHOUT activating it — separating the
        # existence check from activation so that, when we finally activate, the
        # currentChanged it fires sees the NEW session_spec (avoiding a stale
        # dataset push to the chat widget). See Issue #26 (reviewer R4 / reviewer R4).
        existing = next((t for t in window.tabs() if t.name == name), None)
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
            # Activate now — spec is final, so this currentChanged pushes the
            # right dataset to the chat widget.
            window.set_active_tab(name)
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
        tab.register_command(
            "set-split",
            lambda left, right: tab.set_split_ratio(float(left), float(right)),
        )
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
        window.set_active_tab(name)
        panel.set_path(p)
        return f"shown:{name}"

    return _show


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
    window.register_command("close-tab", lambda name: window.close_tab(name))
    window.register_command("list-tabs", lambda: window.tab_names())
    window.register_command("set-active-tab", lambda name: window.set_active_tab(name))
    window.register_command("toggle-chat-float", window.toggle_chat_floating)
    window.register_command("show", _make_show_handler(window))
    window.register_command("open-dataset", lambda name: session.open_dataset(window, name))
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
    window.register_command(
        "meeting-start",
        lambda ttl_sec=10800:
            window._meeting_relay.meeting_start(int(ttl_sec))
            or window._meeting_relay.current_token()
            or "starting",
    )
    window.register_command(
        "meeting-token",
        lambda: window._meeting_relay.current_token() or window._meeting_relay.share_status(),
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
