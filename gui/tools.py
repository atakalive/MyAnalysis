import json
import time

from llm_bridge import commands
from llm_bridge.paths import active_state_path
from common.paths import analyses_root, repo_root

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_analyses",
            "description": "List analysis names available under analyses/.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_open_tabs",
            "description": "List currently open tab names in the GUI.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_active_tab",
            "description": "Return the currently focused tab name and the currently open dataset (null if none).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_analysis",
            "description": "Open an analysis as a new tab (or focus if already open).",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_active_tab",
            "description": "Focus the named tab.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "snapshot",
            "description": "Save a PNG snapshot of the named tab's current view.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show",
            "description": "Display an image file in a viewer tab. "
            "Default (no slot) is full-width single pane; re-showing without slot "
            "collapses any existing split back to single pane. "
            "Use slot to place a second image alongside "
            "(left/right for horizontal, top/bottom for vertical split).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to image file"},
                    "name": {"type": "string", "description": "Tab name (default: viewer)"},
                    "slot": {
                        "type": "string",
                        "enum": ["left", "right", "top", "bottom"],
                        "description": "Pane position. Omit for default (primary/full-width).",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_split",
            "description": "Set left/right panel split ratio for the named tab "
            "(including viewer tabs). "
            "Values are relative weights (e.g. left=3, right=7 gives "
            "30%/70%). The ratio left:(left+right) determines the split. "
            "Both must be positive.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "left": {"type": "number"},
                    "right": {"type": "number"},
                },
                "required": ["name", "left", "right"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_state",
            "description": "Read the latest published state.json for the named tab. "
            "Empty {} if the tab has not pushed state yet.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
]


def make_dispatch(window):
    """Build a dispatch function bound to a ToolWindow."""

    def dispatch(name: str, args: dict, cancelled=None) -> str:
        try:
            return _dispatch(window, name, args, cancelled=cancelled)
        except Exception as e:
            return json.dumps({"error": repr(e)})

    return dispatch


def _dispatch(window, name: str, args: dict, cancelled=None) -> str:
    if "__parse_error__" in args:
        return json.dumps({"error": args["__parse_error__"]})
    if name == "list_analyses":
        names = [
            d.name for d in analyses_root().glob("*") if (d / "analysis.py").is_file()
        ]
        return json.dumps(sorted(names))
    if name == "list_open_tabs":
        return _via_bridge("window", None, "list-tabs", {}, cancelled=cancelled)
    if name == "get_active_tab":
        p = active_state_path()
        if not p.exists():
            return json.dumps({"active_tab": None})
        return p.read_text(encoding="utf-8")
    if name == "open_analysis":
        return _via_bridge(
            "window",
            None,
            "add-tab",
            {"name": args["name"]},
            timeout=30.0,
            cancelled=cancelled,
        )
    if name == "set_active_tab":
        return _via_bridge(
            "window",
            None,
            "set-active-tab",
            {"name": args["name"]},
            cancelled=cancelled,
        )
    if name == "snapshot":
        return _via_bridge(
            "tab", args["name"], "snapshot", {}, timeout=30.0, cancelled=cancelled
        )
    if name == "show":
        kwargs = {"path": args["path"]}
        if "name" in args:
            kwargs["name"] = args["name"]
        if "slot" in args:
            kwargs["slot"] = args["slot"]
        return _via_bridge("window", None, "show", kwargs, cancelled=cancelled)
    if name == "set_split":
        return _via_bridge(
            "tab",
            args["name"],
            "set-split",
            {"left": args["left"], "right": args["right"]},
            cancelled=cancelled,
        )
    if name == "get_state":
        tab_name = args["name"]
        if (
            not tab_name
            or "/" in tab_name
            or "\\" in tab_name
            or tab_name in (".", "..")
        ):
            return json.dumps({"error": f"invalid name: {tab_name!r}"})
        p = repo_root() / "data" / "analyses" / tab_name / "state" / "current.json"
        if not p.exists():
            return json.dumps({})
        return json.dumps(json.loads(p.read_text(encoding="utf-8")), ensure_ascii=False)
    return json.dumps({"error": f"unknown tool: {name}"})


def _via_bridge(tier, target, verb, args, timeout: float = 10.0, cancelled=None) -> str:
    cmd_id = commands.submit(tier, target, verb, args)
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return json.dumps({"status": "timeout", "verb": verb})
        if cancelled and cancelled():
            return json.dumps({"status": "cancelled", "verb": verb})
        poll_timeout = min(1.0, remaining)
        result = commands.wait_for(cmd_id, timeout=poll_timeout)
        if result is not None:
            return json.dumps(
                {k: result.get(k) for k in ("status", "error", "result")},
                ensure_ascii=False,
            )
