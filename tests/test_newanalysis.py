"""Hermetic tests for the `python -m newanalysis` generator.

All tests redirect common.paths roots to tmp_path via monkeypatch so the real
repository's analyses/ directory is never touched:
- `newanalysis.__main__.analyses_root` → tmp_path/analyses (where the generator writes)
- `common.paths.repo_root` → tmp_path (where attach_tab writes state)

Generated analysis.py modules are loaded by file path with
importlib.util.spec_from_file_location, mirroring the loader in
llm_bridge.__init__._make_add_tab_handler and export.__main__.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import newanalysis.__main__ as gen


@pytest.fixture()
def fake_roots(monkeypatch, tmp_path):
    """Redirect generator + state roots into tmp_path. Returns the analyses root."""
    analyses = tmp_path / "analyses"
    monkeypatch.setattr("newanalysis.__main__.analyses_root", lambda: analyses)
    monkeypatch.setattr("common.paths.repo_root", lambda: tmp_path)
    return analyses


def _load_generated(path: Path):
    spec = importlib.util.spec_from_file_location("_test_generated_analysis", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- 正常系 ----

def test_generates_files(fake_roots):
    gen.main(["demo_probe", "--dataset", "dataset_a"])
    assert (fake_roots / "demo_probe" / "analysis.py").is_file()
    assert (fake_roots / "demo_probe" / "README.md").is_file()


def test_generated_files_are_utf8(fake_roots):
    gen.main(["demo_probe"])
    # cp932 で書かれていれば日本語 docstring の decode で失敗する。
    text = (fake_roots / "demo_probe" / "analysis.py").open(encoding="utf-8").read()
    assert 'NAME = "demo_probe"' in text
    assert 'DATASET = ""' in text
    (fake_roots / "demo_probe" / "README.md").open(encoding="utf-8").read()


def test_dataset_embedded(fake_roots):
    gen.main(["demo_probe", "--dataset", "dataset_a"])
    text = (fake_roots / "demo_probe" / "analysis.py").read_text(encoding="utf-8")
    assert 'DATASET = "dataset_a"' in text


# ---- runnable-empty (export 経路) ----

def test_runnable_empty_export(fake_roots):
    gen.main(["demo_probe"])
    mod = _load_generated(fake_roots / "demo_probe" / "analysis.py")
    data = mod.load()
    assert data is not None
    figs = mod.build_export_figs(data)
    assert isinstance(figs, dict)


# ---- runnable-empty (GUI 経路) ----

@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_runnable_empty_gui(fake_roots, qapp, monkeypatch, tmp_path):
    monkeypatch.setattr("llm_bridge.snapshots.writer", lambda name: (lambda tab: None))
    gen.main(["demo_probe"])
    mod = _load_generated(fake_roots / "demo_probe" / "analysis.py")

    from gui.tab import AnalysisTab
    tab = mod.build_tab(parent=None, data=mod.load())
    assert isinstance(tab, AnalysisTab)
    # attach_tab 配線の確認。
    assert tab.has_command("refresh-state") is True

    # 初期 dispatch_command("refresh-state") が state を書き出した証拠。
    state_path = tmp_path / "data" / "analyses" / "demo_probe" / "state" / "current.json"
    assert state_path.is_file()
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"status": "placeholder"}


# ---- バリデーション拒否 (name) ----

@pytest.mark.parametrize(
    "name",
    ["_hidden", "con", "CON", "nul", "com1", "lpt9", "123abc", "my analysis", "", ".",
     "a/b", "a\\b", "Bad"],
)
def test_invalid_name_rejected(fake_roots, name):
    with pytest.raises(SystemExit):
        gen.main([name])


# ---- バリデーション拒否 (dataset) ----

@pytest.mark.parametrize("dataset", ["bad-key", "BadKey", "has space", "_x", "9x", ""])
def test_invalid_dataset_rejected(fake_roots, dataset):
    with pytest.raises(SystemExit):
        gen.main(["demo_probe", "--dataset", dataset])


def test_dataset_reserved_name_accepted(fake_roots):
    """Windows 予約名チェックは name のみ。dataset は文字列埋め込みなので con 等も受理する。"""
    gen.main(["demo_probe", "--dataset", "con"])
    text = (fake_roots / "demo_probe" / "analysis.py").read_text(encoding="utf-8")
    assert 'DATASET = "con"' in text


# ---- 重複防止 ----

def test_duplicate_rejected(fake_roots):
    gen.main(["demo_probe"])
    with pytest.raises(SystemExit):
        gen.main(["demo_probe"])


# ---- rollback ----

def test_rollback_removes_dir_on_write_failure(fake_roots, monkeypatch):
    def _boom(name):
        raise RuntimeError("simulated README failure")

    monkeypatch.setattr("newanalysis.__main__._render_readme", _boom)
    with pytest.raises(RuntimeError):
        gen.main(["demo_probe"])

    # 今回作成した analyses/demo_probe/ は残存しない。
    assert not (fake_roots / "demo_probe").exists()
    # 親 analyses/ は削除されない。
    assert fake_roots.exists()
