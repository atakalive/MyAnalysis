"""Hermetic tests for the headless export driver (export/__main__.py).

Repo-side machinery only — NO dependency on any real dataset or analysis. A
synthetic minimal analysis.py is written into a tmp_path dataset dir and
config.get_dataset_dir is monkeypatched to point there, so this exercises the
driver's validation / error branches / happy path (load → build_export_figs →
figures.save → batch_dir) without touching the synced drive.

dataset_config defers `from config import get_dataset_dir` into its functions,
so patching "config.get_dataset_dir" is sufficient to redirect path resolution.
"""

from __future__ import annotations

import importlib
import sys
import uuid
from pathlib import Path

import pytest

# A self-contained analysis module: load() returns data, build_export_figs()
# returns one Agg figure. core.figures (imported by export/__main__) already
# pins the Agg backend, so pyplot here is headless-safe.
SYNTH_OK = """\
import matplotlib.pyplot as plt

def load():
    return {"sessions": []}

def build_export_figs(data):
    fig = plt.figure()
    fig.add_subplot(111).plot([0, 1], [0, 1])
    return {"out.png": fig, "second.png": plt.figure()}
"""

SYNTH_NO_BUILD = """\
def load():
    return {"sessions": []}
"""

SYNTH_NO_LOAD = """\
def build_export_figs(data):
    return {}
"""

SYNTH_LOAD_NONE = """\
def load():
    return None

def build_export_figs(data):
    return {}
"""

SYNTH_LOAD_RAISES = """\
def load():
    raise RuntimeError("boom")

def build_export_figs(data):
    return {}
"""


@pytest.fixture(autouse=True)
def _isolate_bytecode(monkeypatch, tmp_path):
    """main() はプロセス全体の sys.pycache_prefix を書き換えるので、全テストで保存・復元する。
    退避先は実リポジトリの data/pycache ではなく tmp_path の下にする。
    dont_write_bytecode を False に固定するのは、PYTHONDONTWRITEBYTECODE=1 / python -B の
    下で .pyc がそもそも書かれず、__pycache__ のテストが HEAD でも通ってしまうのを防ぐため。"""
    monkeypatch.setattr(sys, "pycache_prefix", None)
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    monkeypatch.setattr("export.__main__.pycache_prefix", lambda: tmp_path / "pycache")


def _write_analysis(tmp_path: Path, dataset: str, name: str, source: str) -> None:
    adir = tmp_path / dataset / "analyses" / name
    adir.mkdir(parents=True)
    (adir / "analysis.py").write_text(source, encoding="utf-8")


def _run(monkeypatch, tmp_path, dataset: str, name: str):
    """Point get_dataset_dir at tmp_path, set argv, and invoke main()."""
    monkeypatch.setattr("config.get_dataset_dir", lambda n: tmp_path / n)
    monkeypatch.setattr("sys.argv", ["export", dataset, name])
    from export.__main__ import main
    main()


# ---- happy path ----

def test_export_driver_writes_figures_to_batch_dir(tmp_path, monkeypatch, capsys):
    ds, name = "ds_test", "demo_export"
    _write_analysis(tmp_path, ds, name, SYNTH_OK)

    _run(monkeypatch, tmp_path, ds, name)

    batch = tmp_path / ds / "_work" / "analyses" / name / "batch"
    assert (batch / "out.png").is_file()
    assert (batch / "second.png").is_file()
    assert "saved 2 figure(s)" in capsys.readouterr().out


# ---- input validation (rejected before any module load) ----

@pytest.mark.parametrize("bad_name", ["foo/../bar", "..", "a\\b"])
def test_export_driver_rejects_unsafe_name(tmp_path, monkeypatch, bad_name):
    monkeypatch.setattr("sys.argv", ["export", "ds_test", bad_name])
    from export.__main__ import main
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1


# ---- graceful failure branches (each exits 1, never raises uncaught) ----

def test_export_driver_nonexistent_analysis(tmp_path, monkeypatch):
    monkeypatch.setattr("config.get_dataset_dir", lambda n: tmp_path / n)
    monkeypatch.setattr("sys.argv", ["export", "ds_test", "nonexistent"])
    from export.__main__ import main
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1


def test_export_driver_missing_build_export_figs(tmp_path, monkeypatch):
    ds, name = "ds_test", "no_build"
    _write_analysis(tmp_path, ds, name, SYNTH_NO_BUILD)
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, tmp_path, ds, name)
    assert exc.value.code == 1


def test_export_driver_missing_load(tmp_path, monkeypatch):
    ds, name = "ds_test", "no_load"
    _write_analysis(tmp_path, ds, name, SYNTH_NO_LOAD)
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, tmp_path, ds, name)
    assert exc.value.code == 1


def test_export_driver_load_returns_none(tmp_path, monkeypatch):
    ds, name = "ds_test", "load_none"
    _write_analysis(tmp_path, ds, name, SYNTH_LOAD_NONE)
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, tmp_path, ds, name)
    assert exc.value.code == 1


def test_export_driver_load_raises_is_caught(tmp_path, monkeypatch):
    ds, name = "ds_test", "load_raises"
    _write_analysis(tmp_path, ds, name, SYNTH_LOAD_RAISES)
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, tmp_path, ds, name)
    assert exc.value.code == 1


# ---- Issue #100 D-2 / D-8: sys.modules 登録と __pycache__ ----

def test_export_driver_future_annotations_dataclass(tmp_path, monkeypatch, capsys):
    ds, name = "ds_test", "demo_dc"
    src = (
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class P:\n"
        "    x: int = 1\n"
        + SYNTH_OK
    )
    _write_analysis(tmp_path, ds, name, src)
    _run(monkeypatch, tmp_path, ds, name)
    assert "saved 2 figure(s)" in capsys.readouterr().out


def test_export_driver_accepts_bom(tmp_path, monkeypatch, capsys):
    ds, name = "ds_test", "demo_bom"
    adir = tmp_path / ds / "analyses" / name
    adir.mkdir(parents=True)
    (adir / "analysis.py").write_bytes(("\ufeff" + SYNTH_OK).encode("utf-8"))
    _run(monkeypatch, tmp_path, ds, name)
    assert "saved 2 figure(s)" in capsys.readouterr().out


def test_export_driver_writes_no_pycache(tmp_path, monkeypatch):
    ds, name = "ds_test", "demo_nopyc"
    _write_analysis(tmp_path, ds, name, SYNTH_OK)
    _run(monkeypatch, tmp_path, ds, name)
    assert not (tmp_path / ds / "analyses" / name / "__pycache__").exists()


def test_export_driver_sets_pycache_prefix(tmp_path, monkeypatch):
    ds, name = "ds_test", "demo_prefix"
    _write_analysis(tmp_path, ds, name, SYNTH_OK)
    _run(monkeypatch, tmp_path, ds, name)
    assert sys.pycache_prefix == str(tmp_path / "pycache")


def test_export_driver_helper_import_pyc_goes_to_prefix(tmp_path, monkeypatch, request):
    mod = f"helper_{uuid.uuid4().hex[:8]}"
    request.addfinalizer(lambda: sys.modules.pop(mod, None))
    ds, name = "ds_test", "demo_helper"
    _write_analysis(tmp_path, ds, name, f"import {mod}\n" + SYNTH_OK)
    adir = tmp_path / ds / "analyses" / name
    (adir / f"{mod}.py").write_text("V = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(adir))
    importlib.invalidate_caches()
    _run(monkeypatch, tmp_path, ds, name)
    assert not (adir / "__pycache__").exists()
    assert list((tmp_path / "pycache").rglob(f"{mod}*.pyc"))
