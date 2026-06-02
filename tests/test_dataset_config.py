"""Tests for dataset_config: per-dataset myanalysis.toml read/generate/resolve.

config.get_dataset_dir is monkeypatched to tmp_path (same approach as the
dataset-loading tests in tests/test_explore.py), so no real data dir is touched.
"""

from __future__ import annotations

import tomllib

import pytest

import dataset_config


@pytest.fixture()
def ds_dir(monkeypatch, tmp_path):
    """config.get_dataset_dir → tmp_path. Returns the dataset dir."""
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    return tmp_path


# ---- (a) generation on save ----

def test_ensure_config_generates(ds_dir):
    path = dataset_config.ensure_config("ds")
    assert path == ds_dir / "myanalysis.toml"
    assert path.is_file()
    # the template is valid TOML and carries the default work_dir.
    with open(path, "rb") as f:
        assert tomllib.load(f)["work_dir"] == "_work"


def test_get_work_dir_generates_config(ds_dir):
    dataset_config.get_work_dir("ds")
    assert (ds_dir / "myanalysis.toml").is_file()


# ---- (b) idempotent: never overwrite a hand-edited file ----

def test_ensure_config_preserves_existing(ds_dir):
    path = ds_dir / "myanalysis.toml"
    path.write_text('work_dir = "results"\n', encoding="utf-8")

    dataset_config.ensure_config("ds")

    assert path.read_text(encoding="utf-8") == 'work_dir = "results"\n'


# ---- (c) load_config: no write when absent, returns defaults ----

def test_load_config_missing_returns_defaults_without_write(ds_dir):
    cfg = dataset_config.load_config("ds")
    assert cfg["work_dir"] == "_work"
    assert not (ds_dir / "myanalysis.toml").exists()


def test_load_config_merges_defaults(ds_dir):
    # an empty file → work_dir filled from defaults.
    (ds_dir / "myanalysis.toml").write_text("", encoding="utf-8")
    assert dataset_config.load_config("ds")["work_dir"] == "_work"


def test_load_config_reads_value(ds_dir):
    (ds_dir / "myanalysis.toml").write_text(
        'work_dir = "results"\n', encoding="utf-8"
    )
    assert dataset_config.load_config("ds")["work_dir"] == "results"


# ---- (d) work_dir resolution: relative under dir, absolute as-is ----

def test_get_work_dir_relative(ds_dir):
    (ds_dir / "myanalysis.toml").write_text(
        'work_dir = "results"\n', encoding="utf-8"
    )
    assert dataset_config.get_work_dir("ds") == ds_dir / "results"


def test_get_work_dir_default(ds_dir):
    assert dataset_config.get_work_dir("ds") == ds_dir / "_work"


def test_get_work_dir_absolute(ds_dir, tmp_path):
    abs_out = tmp_path / "elsewhere" / "out"
    (ds_dir / "myanalysis.toml").write_text(
        f'work_dir = {str(abs_out)!r}\n', encoding="utf-8"
    )
    assert dataset_config.get_work_dir("ds") == abs_out


# ---- (e) mkdir(parents=True, exist_ok=True) ----

def test_get_work_dir_creates_dir(ds_dir):
    (ds_dir / "myanalysis.toml").write_text(
        'work_dir = "out/figures"\n', encoding="utf-8"
    )
    out = dataset_config.get_work_dir("ds")
    assert out == ds_dir / "out" / "figures"
    assert out.is_dir()


# ---- (f) path validation rejects (OS-independent) ----

@pytest.mark.parametrize("bad", ["C:foo", "\\foo", "../x"])
def test_get_work_dir_rejects_bad_paths(ds_dir, bad):
    (ds_dir / "myanalysis.toml").write_text(
        f'work_dir = {bad!r}\n', encoding="utf-8"
    )
    with pytest.raises(ValueError):
        dataset_config.get_work_dir("ds")


# ---- (g) type validation ----

def test_load_config_rejects_non_string_work_dir(ds_dir):
    (ds_dir / "myanalysis.toml").write_text(
        "work_dir = 123\n", encoding="utf-8"
    )
    with pytest.raises(ValueError):
        dataset_config.load_config("ds")


# ---- (h) TOML parse error propagates ----

def test_load_config_propagates_parse_error(ds_dir):
    (ds_dir / "myanalysis.toml").write_text(
        "work_dir = = bad\n", encoding="utf-8"
    )
    with pytest.raises(tomllib.TOMLDecodeError):
        dataset_config.load_config("ds")
