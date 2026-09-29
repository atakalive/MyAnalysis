import json
import time

from llm_bridge import commands, state
from llm_bridge.paths import active_state_path
import dataset_config

# Optional `dataset` disambiguator shared by tab-addressing tools: when two open
# datasets hold a same-named tab, dataset= selects which one.
_DATASET_PROP = {
    "type": "string",
    "description": "Optional dataset name to disambiguate a tab that exists in "
    "more than one open dataset. Omit to use the active dataset.",
}

# Pane slot path grammar shared by show / show_image (Issue #97).
_SLOT_DOC = (
    "slot is a '/'-separated path of sides — left|right split that level "
    "horizontally, top|bottom vertically (max 6 levels). Placing re-orients that "
    "level to the side you write; an occupied pane on the way is split (its content "
    "moves to the opposite side); re-showing the same slot updates in place. "
    "2x2 grid: slot=top/left, top/right, bottom/left, bottom/right (any order). "
    "Figures (show) and raw images (show_image) can be mixed per pane in one tab. "
    "Re-orienting renames other panes' slots — check with list_panes."
)

# Optional `slot` targeting one image pane for the image-viewer verbs.
_IMG_SLOT_PROP = {
    "type": "string",
    "description": "Image pane path (e.g. bottom/right). Omit for the first image pane.",
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_analyses",
            "description": "List analysis names across ALL open datasets as a map "
            "{dataset: [names]}. Pass dataset= to restrict to one dataset "
            "(same {dataset: [names]} shape).",
            "parameters": {
                "type": "object",
                "properties": {"dataset": _DATASET_PROP},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_open_datasets",
            "description": "List the datasets currently open in the window and "
            "which one is active: {\"open\": [names], \"active\": name|null}.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_active_dataset",
            "description": "Switch the top-level dataset switcher to the named open "
            "dataset (its analysis tabs + chat sessions become visible).",
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
            "name": "list_open_tabs",
            "description": "List currently open tab names in the GUI.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_active_tab",
            "description": "Return the currently focused tab name, the active dataset, "
            "and the open-datasets list (active.json: active_tab, dataset, "
            "active_dataset, open_datasets).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_analysis",
            "description": "Open an analysis as a new tab (or focus if already open). "
            "Pass dataset= to open it under a specific open dataset.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "dataset": _DATASET_PROP,
                },
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
                "properties": {
                    "name": {"type": "string"},
                    "dataset": _DATASET_PROP,
                },
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
                "properties": {
                    "name": {"type": "string"},
                    "dataset": _DATASET_PROP,
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show",
            "description": "Display a generated result figure (PNG etc.) statically "
            "in a viewer tab. For raw/source images (TIFF/16bit/stacks) that need "
            "ImageJ-style interactive viewing, use show_image instead. "
            "Default (no slot) is full-width single pane; re-showing without slot "
            "collapses any existing split back to single pane. "
            "Use slot to place images side by side: " + _SLOT_DOC,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to image file"},
                    "name": {"type": "string", "description": "Tab name (default: viewer)"},
                    "slot": {
                        "type": "string",
                        "description": "Pane path (e.g. right, top/left). Omit for "
                        "default (single full-width pane).",
                    },
                    "dataset": _DATASET_PROP,
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
            "Both must be positive. `left` is the 1st child (top of a vertical "
            "split), `right` the 2nd. `slot` targets a nested split region "
            "(e.g. top); omit for the outermost split.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "left": {"type": "number"},
                    "right": {"type": "number"},
                    "slot": {"type": "string", "description": "Split region path; omit for the outermost split."},
                    "dataset": _DATASET_PROP,
                },
                "required": ["name", "left", "right"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_pane",
            "description": "Close one pane (or a split region) of a viewer tab by "
            "slot path. Other panes keep their slots; the closed side becomes empty "
            "and hidden. Closing the last visible pane is refused (use close_tab).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "slot": {"type": "string", "description": "Pane path, e.g. bottom/right"},
                    "dataset": _DATASET_PROP,
                },
                "required": ["name", "slot"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_panes",
            "description": "List the visible panes of a tab in tree order as "
            "[{slot, key, kind, path}]. Use it to check current slots (they change "
            "when a placement re-orients a split). `key` is an identifier only — "
            "it does not encode the position; always address panes by slot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "dataset": _DATASET_PROP,
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_image",
            "description": "Open a raw/source image (TIFF, 16bit, z/t stack, "
            "multi-channel) for ImageJ-style INTERACTIVE viewing (dynamic range / "
            "LUT / composite). Use this ONLY when the user explicitly asks to view "
            "a raw image / TIFF, or says 'open in ImageJ'. Do NOT open one on your "
            "own initiative. For a generated result figure (PNG) use `show`. "
            "Without slot, updates the first image pane in place (keeping any "
            "split). Use slot to place images side by side: " + _SLOT_DOC + " "
            "panel is ignored when slot is set. LUT/range verbs take the same "
            "slot to address a specific image pane (default: the first one).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to image file"},
                    "name": {"type": "string", "description": "Tab name (default: image)"},
                    "panel": {
                        "type": "string",
                        "enum": ["left", "right"],
                        "description": "Primary pane side (default: left). Ignored when slot is set.",
                    },
                    "slot": {
                        "type": "string",
                        "description": "Pane path (e.g. right, bottom/left). Omit to "
                        "update the first image pane.",
                    },
                    "dataset": _DATASET_PROP,
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_lut",
            "description": "Set the LUT for a channel of an open image-viewer tab. "
            "`name` is the tab name. `lut` is one of Grays/Red/Green/Blue/Magenta/"
            "Cyan/Yellow/Fire/Ice/Spectrum. `channel` (0-based) omitted = active "
            "channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tab name (default: image)"},
                    "lut": {"type": "string"},
                    "channel": {"type": "integer", "description": "0-based; omit for active channel"},
                    "invert": {"type": "boolean"},
                    "slot": _IMG_SLOT_PROP,
                    "dataset": _DATASET_PROP,
                },
                "required": ["name", "lut"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_range",
            "description": "Set the min/max display range for a channel of an "
            "image-viewer tab. `channel` (0-based) omitted = active channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tab name (default: image)"},
                    "min": {"type": "number"},
                    "max": {"type": "number"},
                    "channel": {"type": "integer", "description": "0-based; omit for active channel"},
                    "slot": _IMG_SLOT_PROP,
                    "dataset": _DATASET_PROP,
                },
                "required": ["name", "min", "max"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_mode",
            "description": "Set the display mode of an image-viewer tab: "
            "'single' or 'composite'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tab name (default: image)"},
                    "mode": {"type": "string", "enum": ["single", "composite"]},
                    "slot": _IMG_SLOT_PROP,
                    "dataset": _DATASET_PROP,
                },
                "required": ["name", "mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_channel",
            "description": "Set the active channel (0-based) of an image-viewer tab.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tab name (default: image)"},
                    "index": {"type": "integer", "description": "0-based channel index"},
                    "slot": _IMG_SLOT_PROP,
                    "dataset": _DATASET_PROP,
                },
                "required": ["name", "index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_visible",
            "description": "Show/hide a channel (0-based) in an image-viewer "
            "composite.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tab name (default: image)"},
                    "channel": {"type": "integer", "description": "0-based channel index"},
                    "visible": {"type": "boolean"},
                    "slot": _IMG_SLOT_PROP,
                    "dataset": _DATASET_PROP,
                },
                "required": ["name", "channel", "visible"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_z",
            "description": "Set the Z-slice index (0-based) of an image-viewer tab.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tab name (default: image)"},
                    "index": {"type": "integer", "description": "0-based Z index"},
                    "slot": _IMG_SLOT_PROP,
                    "dataset": _DATASET_PROP,
                },
                "required": ["name", "index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_t",
            "description": "Set the T-frame index (0-based) of an image-viewer tab.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tab name (default: image)"},
                    "index": {"type": "integer", "description": "0-based T index"},
                    "slot": _IMG_SLOT_PROP,
                    "dataset": _DATASET_PROP,
                },
                "required": ["name", "index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "auto_contrast",
            "description": "Auto-contrast a channel of an image-viewer tab "
            "(percentile 0.35/99.65). `channel` (0-based) omitted = active channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tab name (default: image)"},
                    "channel": {"type": "integer", "description": "0-based; omit for active channel"},
                    "low": {"type": "number"},
                    "high": {"type": "number"},
                    "slot": _IMG_SLOT_PROP,
                    "dataset": _DATASET_PROP,
                },
                "required": ["name"],
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
                "properties": {
                    "name": {"type": "string"},
                    "dataset": _DATASET_PROP,
                },
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


def _analyses_for(dataset: str) -> list[str]:
    """Analysis names (dirs with analysis.py) under one dataset's analyses_root."""
    try:
        root = dataset_config.analyses_root(dataset)
    except (KeyError, RuntimeError):
        return []
    if not root.is_dir():
        return []
    return sorted(
        d.name for d in root.glob("*") if (d / "analysis.py").is_file()
    )


def _dispatch(window, name: str, args: dict, cancelled=None) -> str:
    if "__parse_error__" in args:
        return json.dumps({"error": args["__parse_error__"]})
    if name == "list_analyses":
        want = args.get("dataset")
        if want is not None:
            targets = [want]
        elif hasattr(window, "open_dataset_names"):
            targets = list(window.open_dataset_names())
        else:
            cur = window.current_dataset
            targets = [cur] if cur else []
        return json.dumps({ds: _analyses_for(ds) for ds in targets})
    if name == "list_open_datasets":
        return _via_bridge("window", None, "list-open-datasets", {}, cancelled=cancelled)
    if name == "set_active_dataset":
        return _via_bridge(
            "window", None, "set-active-dataset",
            {"name": args["name"]}, cancelled=cancelled,
        )
    if name == "list_open_tabs":
        return _via_bridge("window", None, "list-tabs", {}, cancelled=cancelled)
    if name == "get_active_tab":
        p = active_state_path()
        if not p.exists():
            return json.dumps({"active_tab": None})
        return p.read_text(encoding="utf-8")
    if name == "open_analysis":
        bargs = {"name": args["name"]}
        if "dataset" in args:
            bargs["dataset"] = args["dataset"]
        return _via_bridge(
            "window", None, "add-tab", bargs, timeout=30.0, cancelled=cancelled
        )
    if name == "set_active_tab":
        bargs = {"name": args["name"]}
        if "dataset" in args:
            bargs["dataset"] = args["dataset"]
        return _via_bridge(
            "window", None, "set-active-tab", bargs, cancelled=cancelled
        )
    if name == "snapshot":
        targs = {}
        if "dataset" in args:
            targs["dataset"] = args["dataset"]
        return _via_bridge(
            "tab", args["name"], "snapshot", targs, timeout=30.0, cancelled=cancelled
        )
    if name == "show":
        kwargs = {"path": args["path"]}
        for k in ("name", "slot", "dataset"):
            if k in args:
                kwargs[k] = args[k]
        return _via_bridge("window", None, "show", kwargs, cancelled=cancelled)
    if name == "show_image":
        kwargs = {"path": args["path"]}
        for k in ("name", "panel", "slot", "dataset"):
            if k in args:
                kwargs[k] = args[k]
        return _via_bridge("window", None, "show-image", kwargs, cancelled=cancelled)
    if name in (
        "set_lut", "set_range", "set_mode", "set_channel", "set_visible",
        "set_z", "set_t", "auto_contrast",
    ):
        verb = name.replace("_", "-")
        tab_name = args.get("name", "image")
        verb_args = {k: v for k, v in args.items() if k not in ("name", "dataset")}
        # Forward dataset for tab addressing (commands._execute pops it at the
        # tab tier); mirrors set_split. Without this the _DATASET_PROP the schema
        # advertises is dropped and a same-named viewer in a non-active dataset
        # becomes unaddressable.
        if "dataset" in args:
            verb_args["dataset"] = args["dataset"]
        return _via_bridge(
            "tab", tab_name, verb, verb_args, cancelled=cancelled
        )
    if name == "set_split":
        targs = {"left": args["left"], "right": args["right"]}
        if "slot" in args:
            targs["slot"] = args["slot"]
        if "dataset" in args:
            targs["dataset"] = args["dataset"]
        return _via_bridge(
            "tab", args["name"], "set-split", targs, cancelled=cancelled
        )
    if name in ("close_pane", "list_panes"):
        targs = {"slot": args["slot"]} if name == "close_pane" else {}
        if "dataset" in args:
            targs["dataset"] = args["dataset"]
        return _via_bridge(
            "tab", args["name"], name.replace("_", "-"), targs, cancelled=cancelled
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
        # named tab の state は、その名前で開いている解析タブ自身の dataset から
        # 引く（current_dataset と乖離し得るため）。(name, dataset) で解決する。
        want_ds = args.get("dataset")
        t = None
        finder = getattr(window, "find_tab", None)
        if finder is not None:
            try:
                t = finder(tab_name, want_ds)
            except LookupError:
                t = None
        else:
            t = next((x for x in window.tabs() if x.name == tab_name), None)
        spec = getattr(t, "session_spec", None) if t is not None else None
        if not isinstance(spec, dict) or spec.get("kind") != "analysis":
            return json.dumps({})
        ds = spec.get("dataset") or getattr(t, "dataset", None)
        if ds is None:
            return json.dumps({})
        return json.dumps(state.read(ds, tab_name), ensure_ascii=False)
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
