"""LLM bridge: GUI ↔ LLM via filesystem + CLI.

Public entry points (called from GUI side):
- attach_window(window) → starts command watcher, wires window-level verbs, tracks active tab
- attach_tab(tab, state_provider) → wires snapshot/state/annotations for a tab

Both return iterables of QFileSystemWatcher etc. — caller must hold references.
"""

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from common.paths import analyses_root

if TYPE_CHECKING:
    from gui.window import ToolWindow
from llm_bridge import state, snapshots, commands, annotations, session
from llm_bridge.paths import active_state_path


def _validate_analysis_name(name: str) -> None:
    """Reject names that could escape analyses/ directory."""
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"invalid analysis name: {name!r}")


def _write_active(window) -> None:
    tab = window.active_tab()
    name = tab.name if tab is not None else None
    p = active_state_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"active_tab": name}, ensure_ascii=False), encoding="utf-8"
    )
    tmp.replace(p)


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

    Re-import behavior: the analysis module is NOT registered in sys.modules.
    Each successful add-tab (i.e. when the tab is not already present)
    re-executes the file and re-runs `load()`. For close → add cycles, this
    means `load()` runs again — fine for small data, can be slow if `load()`
    reads large files. Analyses that want caching should memoize inside
    `load()` themselves.
    """

    def _add_tab(name: str) -> str:
        _validate_analysis_name(name)
        root = analyses_root().resolve()
        analysis_dir = (analyses_root() / name).resolve()
        analysis_file = (analysis_dir / "analysis.py").resolve()
        if not analysis_dir.is_relative_to(root):
            raise ValueError(f"analysis directory escapes analyses/: {name!r}")
        if not analysis_file.is_relative_to(root):
            raise ValueError(f"analysis.py escapes analyses/: {name!r}")
        if not analysis_file.is_file():
            raise LookupError(f"no analysis named {name!r} under analyses/")
        if window.set_active_tab(name):
            return f"already-present:{name}"
        spec = importlib.util.spec_from_file_location(
            f"_llm_bridge_analysis_{name}", analysis_file
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"could not build module spec for {analysis_file}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not hasattr(mod, "build_tab"):
            raise AttributeError(
                f"analyses/{name}/analysis.py has no build_tab(parent, data)"
            )
        data = mod.load() if hasattr(mod, "load") else None
        tab = mod.build_tab(window, data)
        window.add_tab(tab)
        import config
        ds = getattr(mod, "DATASET", None) or name
        if ds in config.DATASETS:
            tab.session_spec = {"kind": "analysis", "name": name, "module": name, "dataset": ds}
            session.note_dataset(ds)
        return f"added:{name}"

    return _add_tab


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

        if window.set_active_tab(name):
            tab = window.active_tab()
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
        window.add_tab(tab)
        window.set_active_tab(name)
        panel.set_path(p)
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
        return f"shown:{name}"

    return _show


def attach_window(window) -> list[object]:
    """Wire llm_bridge to a ToolWindow. Returns watchers to keep alive."""
    # Built-in window verbs.
    window.register_command("add-tab", _make_add_tab_handler(window))
    window.register_command("close-tab", lambda name: window.close_tab(name))
    window.register_command("list-tabs", lambda: window.tab_names())
    window.register_command("set-active-tab", lambda name: window.set_active_tab(name))
    window.register_command("toggle-chat-float", window.toggle_chat_floating)
    window.register_command("show", _make_show_handler(window))
    window.register_command("open-dataset", lambda name: session.open_dataset(window, name))

    # Active tab tracker.
    window.tab_changed.connect(lambda _i: _write_active(window))
    _write_active(window)  # initial write

    # Session saver wiring.
    window.set_session_saver(lambda: session.save_all(window))
    window.clear_session_dirty()  # 起動時のプレースホルダ追加等を clean ベースライン化

    # Command queue watcher (drains stale on startup, executes new arrivals).
    cmd_watcher = commands.start_watcher(window)
    return [cmd_watcher]


def attach_tab(tab, state_provider: Callable[[], dict]) -> list[object]:
    """Wire llm_bridge to an AnalysisTab. Returns watchers to keep alive.

    state_provider: callable returning the current state dict.
    The analysis is responsible for triggering `tab.dispatch_command("refresh-state")`
    on its panel signals to push state updates.
    """
    name = tab.name
    state_w = state.writer(name)
    snap_w = snapshots.writer(name)

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
    ann_watcher = annotations.start_watcher(tab)
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
