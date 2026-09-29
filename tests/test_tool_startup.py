"""tool.py の起動経路（Issue #99 C-2）。

pythonw（run.bat）では stderr が無いので、import 時や main() の中の起動失敗は
ダイアログで知らせる。ここでは GUI を作らずに、その配線だけを確かめる。
"""
from __future__ import annotations

import ast
import runpy
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_TOOL = _REPO / "tool.py"


def _calls_attr(node: ast.AST, obj: str, attr: str) -> bool:
    for n in ast.walk(node):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == attr and isinstance(n.func.value, ast.Name)
                and n.func.value.id == obj):
            return True
    return False


def _is_main_guard(node: ast.stmt) -> bool:
    return (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__")


def test_tool_and_crashlog_parse_as_python_310():
    for path in (_TOOL, _REPO / "common" / "crashlog.py"):
        ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 10))


def test_tool_preamble_order():
    body = ast.parse(_TOOL.read_text(encoding="utf-8")).body
    # 先頭の docstring は無い前提（あれば飛ばす）。
    if isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]
    first, guard, tri = body[0], body[1], body[2]
    assert isinstance(first, ast.Import) and [a.name for a in first.names] == ["sys"]

    assert _is_main_guard(guard)
    order = []
    for stmt in guard.body:
        for name in ("require_python", "install", "install_file_logging"):
            if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                    and isinstance(stmt.value.func, ast.Attribute)
                    and stmt.value.func.attr == name
                    and isinstance(stmt.value.func.value, ast.Name)
                    and stmt.value.func.value.id == "crashlog"):
                order.append(name)
    assert order == ["require_python", "install", "install_file_logging"]

    assert isinstance(tri, ast.Try)
    names = set()
    for stmt in tri.body:
        if isinstance(stmt, ast.Import):
            names |= {a.name for a in stmt.names}
        elif isinstance(stmt, ast.ImportFrom):
            names.add(stmt.module)
    assert "llm_bridge" in names
    assert "PySide6.QtWidgets" in names

    for stmt in body[3:]:
        assert not isinstance(stmt, (ast.Import, ast.ImportFrom)), ast.dump(stmt)

    last = body[-1]
    assert _is_main_guard(last)
    inner = last.body[0]
    assert isinstance(inner, ast.Try)
    assert any(isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
               and isinstance(s.value.func, ast.Name) and s.value.func.id == "main"
               for s in inner.body)
    assert any(_calls_attr(h, "crashlog", "report_startup_failure")
               for h in inner.handlers)


@pytest.fixture
def _recorded_crashlog(monkeypatch):
    from common import crashlog

    reported: list[BaseException] = []
    monkeypatch.setattr(crashlog, "install", lambda *a, **k: None)
    monkeypatch.setattr(crashlog, "install_file_logging", lambda *a, **k: None)
    monkeypatch.setattr(crashlog, "require_python", lambda *a, **k: None)
    monkeypatch.setattr(crashlog, "report_startup_failure", reported.append)
    return reported


def test_tool_main_reports_import_failure(monkeypatch, _recorded_crashlog):
    monkeypatch.setitem(sys.modules, "llm_bridge", None)
    with pytest.raises(SystemExit) as ei:
        runpy.run_path(str(_TOOL), run_name="__main__")
    assert ei.value.code == 1
    assert len(_recorded_crashlog) == 1
    assert isinstance(_recorded_crashlog[0], ModuleNotFoundError)


def test_tool_import_reraises_outside_main(monkeypatch, _recorded_crashlog):
    monkeypatch.setitem(sys.modules, "llm_bridge", None)
    with pytest.raises(ModuleNotFoundError):
        runpy.run_path(str(_TOOL), run_name="tool_startup_probe")
    assert _recorded_crashlog == []


def test_tool_main_reports_failure_inside_main(monkeypatch, _recorded_crashlog):
    import common.env

    def _boom():
        raise RuntimeError("boom")
    monkeypatch.setattr(common.env, "load_env", _boom)
    with pytest.raises(SystemExit) as ei:
        runpy.run_path(str(_TOOL), run_name="__main__")
    assert ei.value.code == 1
    assert len(_recorded_crashlog) == 1
    assert isinstance(_recorded_crashlog[0], RuntimeError)
