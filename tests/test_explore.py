"""Hermetic tests for common.explore and the new llm_bridge CLI subcommands.

All filesystem-touching tests redirect common.paths.repo_root to tmp_path via
monkeypatch (the `fake_roots` fixture), mirroring tests/test_newanalysis.py, so
the real repo's data/ directory is never touched. Dataset-loading tests
monkeypatch config.get_dataset_dir / common.loaders.load_csv_per_subdir at the
module paths common.explore looks them up from at call time.
"""

from __future__ import annotations

import pytest

import common.explore as explore
import llm_bridge.__main__ as bridge_main
from common.paths import validate_name


@pytest.fixture()
def fake_roots(monkeypatch, tmp_path):
    """common.paths.repo_root を tmp_path にリダイレクト。"""
    monkeypatch.setattr("common.paths.repo_root", lambda: tmp_path)
    return tmp_path


# ---- load_dataset ----

def test_load_dataset_delegates(monkeypatch, tmp_path):
    captured = {}

    def fake_get_dataset_dir(name):
        captured["name"] = name
        return tmp_path / "ds_root"

    def fake_load(root, subdir_pattern, csv_name, encoding):
        captured["root"] = root
        captured["subdir_pattern"] = subdir_pattern
        captured["csv_name"] = csv_name
        captured["encoding"] = encoding
        return [{"name": "s0", "dir": root / "s0", "df": object()}]

    monkeypatch.setattr("config.get_dataset_dir", fake_get_dataset_dir)
    monkeypatch.setattr("common.loaders.load_csv_per_subdir", fake_load)

    result = explore.load_dataset("test")

    assert captured["name"] == "test"
    assert captured["root"] == tmp_path / "ds_root"
    assert captured["subdir_pattern"] == "session_*"
    assert captured["csv_name"] == "samples.csv"
    assert captured["encoding"] is None
    assert result == [{"name": "s0", "dir": tmp_path / "ds_root" / "s0", "df": result[0]["df"]}]


def test_load_dataset_custom_pattern(monkeypatch, tmp_path):
    captured = {}

    def fake_load(root, subdir_pattern, csv_name, encoding):
        captured["subdir_pattern"] = subdir_pattern
        captured["csv_name"] = csv_name
        captured["encoding"] = encoding
        return [{"name": "x", "dir": root, "df": object()}]

    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    monkeypatch.setattr("common.loaders.load_csv_per_subdir", fake_load)

    explore.load_dataset(
        "test", subdir_pattern="run_*", csv_name="data.csv", encoding="cp932"
    )

    assert captured["subdir_pattern"] == "run_*"
    assert captured["csv_name"] == "data.csv"
    assert captured["encoding"] == "cp932"


def test_load_dataset_unknown_raises(monkeypatch):
    monkeypatch.setattr("config.DATASETS", {})
    with pytest.raises(KeyError):
        explore.load_dataset("nope")


def test_load_dataset_empty_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    monkeypatch.setattr("common.loaders.load_csv_per_subdir",
                        lambda **kw: [])
    with pytest.raises(FileNotFoundError):
        explore.load_dataset("test")


# ---- save_fig ----

def test_save_fig_creates_file(monkeypatch, tmp_path):
    from matplotlib.figure import Figure
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    fig = Figure()
    fig.add_subplot(111).plot([0, 1], [0, 1])

    path = explore.save_fig("ds", fig, "test_out")

    expected = tmp_path / "_work" / "figures" / "test_out.png"
    assert path == expected
    assert expected.is_file()
    assert expected.stat().st_size > 0


@pytest.mark.parametrize("label", ["", "a/b", "a\\b", ".", "..", "has\0nul"])
def test_save_fig_invalid_labels(monkeypatch, tmp_path, label):
    from matplotlib.figure import Figure
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    fig = Figure()
    with pytest.raises(ValueError):
        explore.save_fig("ds", fig, label)


def test_save_fig_overwrites(monkeypatch, tmp_path):
    from matplotlib.figure import Figure
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)

    fig1 = Figure(figsize=(2, 2))
    fig1.add_subplot(111).plot([0, 1], [0, 1])
    explore.save_fig("ds", fig1, "dup")

    expected = tmp_path / "_work" / "figures" / "dup.png"
    first_size = expected.stat().st_size

    fig2 = Figure(figsize=(8, 8))
    fig2.add_subplot(111).plot([0, 1], [1, 0])
    path = explore.save_fig("ds", fig2, "dup")

    assert path == expected
    assert expected.is_file()
    assert expected.stat().st_size > first_size


# ---- save_code ----

def test_save_code_creates_file(monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    path = explore.save_code("ds", "snippet", "print('hello')")
    expected = tmp_path / "_work" / "code" / "snippet.py"
    assert path == expected
    assert expected.is_file()
    assert expected.read_text(encoding="utf-8") == "print('hello')"


@pytest.mark.parametrize("label", ["", "a/b", "a\\b", ".", "..", "has\0nul"])
def test_save_code_invalid_labels(monkeypatch, tmp_path, label):
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    with pytest.raises(ValueError):
        explore.save_code("ds", label, "x")


# ---- dataset_summary ----

def test_dataset_summary_structure(monkeypatch, tmp_path):
    import pandas as pd

    df = pd.DataFrame({"i": [1, 2, 3], "j": [0.1, 0.2, 0.3]})
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path / "root")
    monkeypatch.setattr(
        "common.loaders.load_csv_per_subdir",
        lambda **kw: [{"name": "sess0", "dir": tmp_path, "df": df}],
    )

    summary = explore.dataset_summary("test")

    assert summary["name"] == "test"
    assert summary["path"] == str(tmp_path / "root")
    assert len(summary["sessions"]) == 1
    s = summary["sessions"][0]
    assert s["name"] == "sess0"
    assert s["rows"] == 3
    assert s["columns"] == ["i", "j"]
    assert set(s["dtypes"]) == {"i", "j"}
    assert all(isinstance(v, str) for v in s["dtypes"].values())


def test_dataset_summary_empty_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    monkeypatch.setattr("common.loaders.load_csv_per_subdir", lambda **kw: [])
    with pytest.raises(FileNotFoundError):
        explore.dataset_summary("test")


# ---- validate_name ----

@pytest.mark.parametrize("name", ["", "a/b", "a\\b", ".", "..", "x\0y"])
def test_validate_name_rejects(name):
    with pytest.raises(ValueError):
        validate_name(name)


@pytest.mark.parametrize("name", ["hello", "test_123", "a"])
def test_validate_name_accepts(name):
    validate_name(name)  # 例外が出ないこと。


# ---- CLI: list-datasets ----

def test_list_datasets_cli(monkeypatch, capsys):
    monkeypatch.setattr(
        "config.DATASETS",
        {"zeta": {}, "alpha": {}, "dataset_a": {}},
    )
    rc = bridge_main.main(["list-datasets"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.splitlines() == ["alpha", "dataset_a", "zeta"]


# ---- CLI: list-analyses excludes underscore-prefixed dirs ----

def test_list_analyses_excludes_underscore(fake_roots, capsys):
    analyses = fake_roots / "analyses"
    for name in ("_viewer", "real_one"):
        d = analyses / name
        d.mkdir(parents=True)
        (d / "analysis.py").write_text("# stub\n", encoding="utf-8")

    rc = bridge_main.main(["list-analyses"])
    assert rc == 0
    names = capsys.readouterr().out.splitlines()
    assert "_viewer" not in names
    assert "real_one" in names
