"""Hermetic tests for the `python -m newanalysis` generator.

All tests redirect the dataset directory into tmp_path by patching
`config.get_dataset_dir`, so the real synced drive is never touched. The
generator now writes into `<dataset_dir>/analyses/<name>/` and analysis output
(state) lands under `<dataset_dir>/_work/analyses/<name>/`.

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

DS = "ds_test"


@pytest.fixture()
def fake_roots(monkeypatch, tmp_path):
    """Point dataset dirs at tmp_path. Returns DS's analyses root."""
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path / name)
    return tmp_path / DS / "analyses"


def _load_generated(path: Path):
    spec = importlib.util.spec_from_file_location("_test_generated_analysis", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- 正常系 ----

def test_generates_files(fake_roots):
    gen.main(["demo_probe", "--dataset", DS])
    assert (fake_roots / "demo_probe" / "analysis.py").is_file()
    assert (fake_roots / "demo_probe" / "README.md").is_file()


def test_generated_files_are_utf8(fake_roots):
    gen.main(["demo_probe", "--dataset", DS])
    # cp932 で書かれていれば日本語 docstring の decode で失敗する。
    text = (fake_roots / "demo_probe" / "analysis.py").open(encoding="utf-8").read()
    assert 'NAME = "demo_probe"' in text
    assert f'DATASET = "{DS}"' in text
    (fake_roots / "demo_probe" / "README.md").open(encoding="utf-8").read()


def test_dataset_embedded(fake_roots, tmp_path):
    gen.main(["demo_probe", "--dataset", "my_dataset"])
    text = (
        tmp_path / "my_dataset" / "analyses" / "demo_probe" / "analysis.py"
    ).read_text(encoding="utf-8")
    assert 'DATASET = "my_dataset"' in text


# ---- runnable-empty (export 経路) ----

def test_runnable_empty_export(fake_roots):
    gen.main(["demo_probe", "--dataset", DS])
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
    import llm_bridge

    gen.main(["demo_probe", "--dataset", DS])
    mod = _load_generated(fake_roots / "demo_probe" / "analysis.py")

    from gui.tab import AnalysisTab
    with llm_bridge._building(DS):
        tab = mod.build_tab(parent=None, data=mod.load())
    assert isinstance(tab, AnalysisTab)
    # attach_tab 配線の確認。
    assert tab.has_command("refresh-state") is True

    # 初期 dispatch_command("refresh-state") が state を書き出した証拠。
    state_path = (
        tmp_path / DS / "_work" / "analyses" / "demo_probe" / "state" / "current.json"
    )
    assert state_path.is_file()
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"status": "placeholder"}


# ---- バリデーション拒否 (name) ----

# 解析名 = <dataset_dir>/analyses/<name>/ という実フォルダ。予約名・空白・空・先頭
# ドット・パス区切り・Windows 禁止文字・末尾ドットは拒否（大文字/数字始まり/'-'/'_'
# は許可）。
@pytest.mark.parametrize(
    "name",
    ["con", "CON", "nul", "com1", "lpt9", "my analysis", "", ".",
     "a/b", "a\\b", "a:b", "a*b", "foo."],
)
def test_invalid_name_rejected(fake_roots, name):
    with pytest.raises(SystemExit):
        gen.main([name, "--dataset", DS])


# ---- バリデーション拒否 (dataset) ----

# データセット名 = dict キー。パスにならないので汎用ハイジーンのみ拒否
# （'-'/大文字/数字始まり/先頭'_' は許可。bad-key 等はもう拒否しない）。
@pytest.mark.parametrize("dataset", ["has space", "", "a/b", "a\\b", ".", "..", ".hidden"])
def test_invalid_dataset_rejected(fake_roots, dataset):
    with pytest.raises(SystemExit):
        gen.main(["demo_probe", "--dataset", dataset])


def test_dataset_reserved_name_accepted(fake_roots, tmp_path):
    """Windows 予約名チェックは name のみ。dataset は文字列埋め込みなので con 等も受理する。"""
    gen.main(["demo_probe", "--dataset", "con"])
    text = (
        tmp_path / "con" / "analyses" / "demo_probe" / "analysis.py"
    ).read_text(encoding="utf-8")
    assert 'DATASET = "con"' in text


# ---- 重複防止 ----

def test_duplicate_rejected(fake_roots):
    gen.main(["demo_probe", "--dataset", DS])
    with pytest.raises(SystemExit):
        gen.main(["demo_probe", "--dataset", DS])


# ---- rollback ----

def test_rollback_removes_dir_on_write_failure(fake_roots, monkeypatch):
    def _boom(name):
        raise RuntimeError("simulated README failure")

    monkeypatch.setattr("newanalysis.__main__._render_readme", _boom)
    with pytest.raises(RuntimeError):
        gen.create_analysis("demo_probe", dataset=DS)

    # 今回作成した analyses/demo_probe/ は残存しない。
    assert not (fake_roots / "demo_probe").exists()


# ---- create_analysis 非終了コア ----

def test_create_analysis_returns_paths(fake_roots):
    analysis_path, readme_path = gen.create_analysis("demo_probe", dataset=DS)
    assert analysis_path.is_file()
    assert readme_path.is_file()
    assert analysis_path == fake_roots / "demo_probe" / "analysis.py"
    assert readme_path == fake_roots / "demo_probe" / "README.md"


def test_create_analysis_dataset_required(fake_roots):
    # dataset は必須引数。省略すると TypeError。
    with pytest.raises(TypeError):
        gen.create_analysis("demo_probe")


def test_create_analysis_invalid_name_raises(fake_roots):
    with pytest.raises(ValueError):
        gen.create_analysis("a:b", dataset=DS)  # 解析名にはフォルダ名禁止文字を使えない。
    with pytest.raises(ValueError):
        gen.create_analysis("con", dataset=DS)


def test_create_analysis_duplicate_raises(fake_roots):
    gen.create_analysis("demo_probe", dataset=DS)
    with pytest.raises(FileExistsError):
        gen.create_analysis("demo_probe", dataset=DS)


# ---- validate_identifier_name ----

def test_validate_identifier_name_ok():
    from common.paths import validate_identifier_name

    validate_identifier_name("my_dataset_240101")
    validate_identifier_name("con", check_reserved=False)


@pytest.mark.parametrize("name", ["con", "CON", "nul", "com1", "lpt9"])
def test_validate_identifier_name_reserved(name):
    from common.paths import validate_identifier_name

    with pytest.raises(ValueError):
        validate_identifier_name(name, check_reserved=True)
    # check_reserved=False では予約名チェックをしないため受理。
    validate_identifier_name(name.lower(), check_reserved=False)


# 両モードで拒否される汎用の危険入力。
@pytest.mark.parametrize(
    "name", ["", "has space", "foo\n", "a/b", "a\\b", ".", "..", ".hidden"]
)
def test_validate_identifier_name_denylist(name):
    from common.paths import validate_identifier_name

    with pytest.raises(ValueError):
        validate_identifier_name(name, check_reserved=False)


# データセット名 (check_reserved=False) では従来 NG だった名前も許可される。
@pytest.mark.parametrize(
    "name",
    [
        "Bad", "9x", "_x", "a-b",
        "240101_test", "AB_mixed_case", "サンプル_240101", "a:b",
    ],
)
def test_validate_identifier_name_allows(name):
    from common.paths import validate_identifier_name

    validate_identifier_name(name, check_reserved=False)  # 例外が出ないこと。


# 解析名 (check_reserved=True) は実フォルダになるため Windows 規則で追加拒否。
# 同じ名前でもデータセット名 (False) なら受理される非対称を固定する。
@pytest.mark.parametrize("name", ["a:b", "a*b", "a?b", 'a"b', "foo."])
def test_validate_identifier_name_dirname_strict(name):
    from common.paths import validate_identifier_name

    with pytest.raises(ValueError):
        validate_identifier_name(name, check_reserved=True)
    validate_identifier_name(name, check_reserved=False)  # dict キーとしては可。


# ---- format template dispatch (#27) ----

def test_custom_format_template(fake_roots):
    analysis_path, _ = gen.create_analysis("cust", dataset=DS, fmt="custom")
    text = analysis_path.read_text(encoding="utf-8")
    assert "load_csv_per_subdir" not in text
    assert 'format="custom"' in text


def test_default_format_template(fake_roots):
    analysis_path, _ = gen.create_analysis("deflt", dataset=DS)
    text = analysis_path.read_text(encoding="utf-8")
    assert "load_csv_per_subdir" in text


def test_unknown_format_raises(fake_roots):
    with pytest.raises(ValueError):
        gen.create_analysis("badfmt", dataset=DS, fmt="unknown")
