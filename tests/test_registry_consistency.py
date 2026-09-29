"""Registry consistency guards (Issue #103 G-5).

No QApplication is created, but importing gui.tools pulls in gui/__init__.py,
which imports PySide6 and pyqtgraph at top level (both required dependencies).
"""
import json

import gui.tools
import llm_backend
from llm_backend.engines import ENGINES

_DUMMY = {"string": "x", "number": 1.0, "integer": 0, "boolean": True}


class _FakeWindow:
    """Qt-free window, same shape as tests/test_llm_tool_use.py's _FakeWindow."""
    current_dataset = None

    def open_dataset_names(self):
        return []

    def tabs(self):
        return []


def _is_unknown_tool(result: str) -> bool:
    err = json.loads(result).get("error")
    return isinstance(err, str) and err.startswith("unknown tool")


def test_engine_backend_keys_are_registered():
    missing = [
        (e.id, e.backend_key) for e in ENGINES
        if e.backend_key not in llm_backend._BACKENDS
    ]
    assert not missing, f"engines whose backend_key is not in _BACKENDS: {missing}"


def test_every_tool_is_dispatched(monkeypatch, tmp_path):
    monkeypatch.setattr(
        gui.tools, "_via_bridge",
        lambda tier, target, verb, args, **kw: json.dumps({"status": "ok", "verb": verb}),
    )
    monkeypatch.setattr("gui.tools.active_state_path", lambda: tmp_path / "active.json")
    win = _FakeWindow()

    # 陰性対照: 判定が空振りしていないこと。
    assert _is_unknown_tool(gui.tools._dispatch(win, "no_such_tool", {}))

    unknown = []
    for tool in gui.tools.TOOLS:
        name = tool["function"]["name"]
        params = tool["function"].get("parameters", {})
        args = {
            k: _DUMMY[params["properties"][k]["type"]]
            for k in params.get("required", [])
        }
        if _is_unknown_tool(gui.tools._dispatch(win, name, args)):
            unknown.append(name)
    assert not unknown, f"tools not handled by _dispatch: {unknown}"
