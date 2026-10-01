"""Verb reference for `python -m llm_bridge list-commands` (Qt-free).

The keys must equal the verbs actually registered by llm_bridge._rewire_window,
devtools.qt_integration.install_hotreload, the built-in tab wiring and
gui.imageviewer._register_viewer_verbs — tests/test_verbs_registry.py checks it.
"""

WINDOW_VERBS: dict[str, str] = {
    "add-tab": "name=<analysis> [dataset=<ds>] — open an analysis tab",
    "close-tab": "name=<tab> [dataset=<ds>] — close a tab",
    "list-tabs": "[detail=true] — tab names (detail: [{name, dataset, kind}])",
    "set-active-tab": "name=<tab> [dataset=<ds>] — bring a tab to the front",
    "toggle-chat-float": "— dock / undock the chat panel",
    "show": "path=<abs> [name=viewer] [slot=<path>] [dataset=<ds>] — show a result figure",
    "show-image": (
        "path=<abs> [name=image] [panel=left|right] [slot=<path>] [dataset=<ds>]"
        " — open a raw image in the image viewer (only when the user asks)"
    ),
    "open-dataset": "name=<ds> — open / restore a dataset",
    "list-open-datasets": "— {open: [...], active: <ds>}",
    "set-active-dataset": "name=<ds> — bring a dataset to the front",
    "switch-dataset": "name=<ds> — alias of set-active-dataset",
    "close-dataset": "name=<ds> — save the layout and close a dataset",
    "chat-list": "[dataset=<ds>|scope=all|scope=unbound|search_tab=<sid>] [archived=true] [limit=200] [offset=<n>] — past chats overview (search result tabs excluded)",
    "chat-search": "query=<words> [dataset=<ds>|scope=all|scope=unbound|search_tab=<sid>] [archived=true] [limit=50] [offset=<n>] — keyword search over chats (all words, case-insensitive)",
    "chat-show": "sid=<sid> [start=<idx>] [end=<idx>] [char_offset=<n>] [max_chars=20000] [raw=true] — read a chat transcript (idx = message index, end is inclusive)",
    "reload": "[scope=patch|tab|app|restart] [target=<tab>] [dataset=<ds>] — hot reload (development)",
}

# Registered for the GUI itself / meeting share. Listed separately so that
# list-commands does not present them as ordinary agent verbs.
INTERNAL_WINDOW_VERBS: dict[str, str] = {
    "chat-inject": "text=<text> sender=<name> [session=<id>] — post a guest message",
    "chat-list-sessions": "— chat session summaries",
    "meeting-start": "[ttl_sec=10800] [lan=true] — start meeting share",
    "meeting-token": "— current meeting token",
    "meeting-lan-link": "— LAN deep link",
    "meeting-stop": "— stop meeting share",
}

# Registered on EVERY tab: by attach_tab for analysis tabs, and by
# llm_bridge._finish_new for the viewer tabs that show / show-image create.
COMMON_TAB_VERBS: dict[str, str] = {
    "set-split": "left=<n> right=<n> [slot=<region>] (top=/bottom= are aliases) — split ratio",
    "close-pane": "slot=<path> — close a pane or region",
    "list-panes": "— [{slot, key, kind, path}] in tree order",
    "snapshot": "— save a snapshot of the tab (viewer tabs: does nothing)",
}

# Registered by attach_tab only — viewer tabs (show / show-image) do NOT have it.
ANALYSIS_TAB_VERBS: dict[str, str] = {
    "refresh-state": "— re-sync the tab state file from the UI",
}

IMAGE_VIEWER_VERBS: dict[str, str] = {
    "set-lut": "lut=<name> [invert=true] [channel=<n>] [slot=<path>] — LUT",
    "set-range": "min=<v> max=<v> [channel=<n>] [slot=<path>] — display range",
    "auto-contrast": "[low=0.35] [high=99.65] [channel=<n>] [slot=<path>] — auto range",
    "set-channel": "index=<n> [slot=<path>] — select a channel",
    "set-mode": "mode=single|composite [slot=<path>] — channel mode",
    "set-z": "index=<n> [slot=<path>] — z plane",
    "set-t": "index=<n> [slot=<path>] — time point",
    "set-visible": "channel=<n> visible=true|false [slot=<path>] — channel visibility",
    "load-image": "path=<abs> [slot=<path>] — load another image (not saved to the session)",
}
