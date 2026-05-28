"""LLM bridge: GUI ↔ LLM via filesystem + CLI.

Public entry points (called from GUI side):
- attach_window(window) → starts command watcher, wires window-level verbs, tracks active tab
- attach_tab(tab, state_provider) → wires snapshot/state/annotations for a tab

Both return iterables of QFileSystemWatcher etc. — caller must hold references.
"""
import importlib.util
import json
from collections.abc import Callable
from common.paths import analyses_root
from llm_bridge import state, snapshots, commands, annotations
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
    tmp.write_text(json.dumps({"active_tab": name}, ensure_ascii=False), encoding="utf-8")
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
        return f"added:{name}"
    return _add_tab


def attach_window(window) -> list[object]:
    """Wire llm_bridge to a ToolWindow. Returns watchers to keep alive."""
    # Built-in window verbs.
    window.register_command("add-tab", _make_add_tab_handler(window))
    window.register_command("close-tab",
        lambda name: window.close_tab(name))
    window.register_command("list-tabs", lambda: window.tab_names())
    window.register_command("set-active-tab",
        lambda name: window.set_active_tab(name))
    window.register_command("toggle-chat-float", window.toggle_chat_floating)

    # Active tab tracker.
    window.tab_changed.connect(lambda _i: _write_active(window))
    _write_active(window)  # initial write

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
    tab.register_command("set-split",
        lambda left, right: tab.set_split_ratio(float(left), float(right)))
    tab.register_command("snapshot",
        lambda: tab.take_snapshot())
    tab.register_command("refresh-state",
        lambda: state_w(state_provider()))

    # Annotations watcher
    ann_watcher = annotations.start_watcher(tab)
    return [ann_watcher]


__all__ = ["state", "snapshots", "commands", "annotations",
           "attach_window", "attach_tab"]
