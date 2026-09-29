"""common.analysis_module: analysis.py を一時的な sys.modules 登録の下で実行する（Issue #100 D-2）。"""

from __future__ import annotations

import sys
import typing

import pytest

from common.analysis_module import analysis_module, analysis_module_name

_DATACLASS_SRC = (
    "from __future__ import annotations\n"
    "from dataclasses import dataclass\n"
    "@dataclass\n"
    "class P:\n"
    "    x: int = 1\n"
)


def _write(tmp_path, source: str):
    path = tmp_path / "analysis.py"
    path.write_text(source, encoding="utf-8")
    return path


def test_future_annotations_dataclass_executes(tmp_path):
    path = _write(tmp_path, _DATACLASS_SRC)
    with analysis_module("_t_mod", path, _DATACLASS_SRC) as mod:
        assert mod.P().x == 1
        assert typing.get_type_hints(mod.P) == {"x": int}


def test_registered_only_inside_with(tmp_path):
    path = _write(tmp_path, "V = 1\n")
    with analysis_module("_t_mod", path, "V = 1\n") as mod:
        assert sys.modules["_t_mod"] is mod
    assert "_t_mod" not in sys.modules


def test_previous_entry_restored(tmp_path, monkeypatch):
    sentinel = object()
    monkeypatch.setitem(sys.modules, "_t_mod", sentinel)
    path = _write(tmp_path, "V = 1\n")
    with analysis_module("_t_mod", path, "V = 1\n"):
        pass
    assert sys.modules["_t_mod"] is sentinel


def test_restored_on_exec_error(tmp_path):
    src = 'raise ValueError("boom")\n'
    path = _write(tmp_path, src)
    with pytest.raises(ValueError):
        with analysis_module("_t_mod", path, src):
            pass
    assert "_t_mod" not in sys.modules


def test_no_pycache_written(tmp_path):
    path = _write(tmp_path, _DATACLASS_SRC)
    with analysis_module("_t_mod", path, _DATACLASS_SRC):
        pass
    assert not (tmp_path / "__pycache__").exists()


def test_module_name_includes_dataset():
    assert analysis_module_name("ds1", "a") != analysis_module_name("ds2", "a")
