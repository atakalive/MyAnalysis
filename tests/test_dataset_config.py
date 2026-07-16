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


# ---- (i) format metadata (#27) ----

def test_load_config_default_format(ds_dir):
    # no toml → default format.
    assert dataset_config.load_config("ds")["format"] == "csv_per_subdir"


def test_load_config_reads_format(ds_dir):
    (ds_dir / "myanalysis.toml").write_text(
        'work_dir = "_work"\nformat = "custom"\n', encoding="utf-8"
    )
    assert dataset_config.load_config("ds")["format"] == "custom"


def test_load_config_reads_format_single_quote(ds_dir):
    (ds_dir / "myanalysis.toml").write_text(
        "work_dir = '_work'\nformat = 'custom'\n", encoding="utf-8"
    )
    assert dataset_config.load_config("ds")["format"] == "custom"


def test_load_config_rejects_non_string_format(ds_dir):
    (ds_dir / "myanalysis.toml").write_text(
        'work_dir = "_work"\nformat = 123\n', encoding="utf-8"
    )
    with pytest.raises(ValueError):
        dataset_config.load_config("ds")


def test_load_config_rejects_unknown_format(ds_dir):
    (ds_dir / "myanalysis.toml").write_text(
        'work_dir = "_work"\nformat = "unknown"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError):
        dataset_config.load_config("ds")


def test_set_format_creates_and_sets(ds_dir):
    dataset_config.set_format("ds", "custom")
    assert dataset_config.load_config("ds")["format"] == "custom"


def test_set_format_updates_existing_double_quote(ds_dir):
    (ds_dir / "myanalysis.toml").write_text(
        'work_dir = "_work"\nformat = "csv_per_subdir"\n', encoding="utf-8"
    )
    dataset_config.set_format("ds", "custom")
    text = (ds_dir / "myanalysis.toml").read_text(encoding="utf-8")
    assert 'format = "custom"' in text
    assert text.count("format =") == 1


def test_set_format_updates_existing_single_quote(ds_dir):
    (ds_dir / "myanalysis.toml").write_text(
        "work_dir = '_work'\nformat = 'csv_per_subdir'\n", encoding="utf-8"
    )
    dataset_config.set_format("ds", "custom")
    text = (ds_dir / "myanalysis.toml").read_text(encoding="utf-8")
    assert 'format = "custom"' in text
    assert text.count("format =") == 1


def test_set_format_updates_existing_indented(ds_dir):
    (ds_dir / "myanalysis.toml").write_text(
        'work_dir = "_work"\n  format = "csv_per_subdir"\n', encoding="utf-8"
    )
    dataset_config.set_format("ds", "custom")
    text = (ds_dir / "myanalysis.toml").read_text(encoding="utf-8")
    assert 'format = "custom"' in text
    assert text.count("format =") == 1
    assert dataset_config.load_config("ds")["format"] == "custom"


def test_set_format_preserves_work_dir(ds_dir):
    (ds_dir / "myanalysis.toml").write_text(
        'work_dir = "results"\nformat = "csv_per_subdir"\n', encoding="utf-8"
    )
    dataset_config.set_format("ds", "custom")
    assert dataset_config.load_config("ds")["work_dir"] == "results"


def test_set_format_rejects_unknown(ds_dir):
    with pytest.raises(ValueError):
        dataset_config.set_format("ds", "unknown")


def test_set_format_handles_no_trailing_newline(ds_dir):
    (ds_dir / "myanalysis.toml").write_text(
        'work_dir = "_work"', encoding="utf-8"
    )
    dataset_config.set_format("ds", "custom")
    text = (ds_dir / "myanalysis.toml").read_text(encoding="utf-8")
    # no line concatenation: work_dir and format on separate lines, parseable.
    assert 'work_dir = "_work"format' not in text
    assert dataset_config.load_config("ds")["format"] == "custom"
    assert dataset_config.load_config("ds")["work_dir"] == "_work"


# ---- (c) analysis path-resolution layer (Issue #37) ----

def test_analyses_root_no_mkdir(ds_dir):
    root = dataset_config.analyses_root("ds")
    assert root == ds_dir / "analyses"
    assert not root.exists()  # read-only: never created


def test_analysis_file_resolves_without_existence_check(ds_dir):
    f = dataset_config.analysis_file("ds", "an1")
    assert f == (ds_dir / "analyses" / "an1" / "analysis.py").resolve()
    assert not f.exists()  # existence is the caller's concern


@pytest.mark.parametrize("bad", ["../etc", "a/b", "a\\b", ".", "..", ""])
def test_analysis_file_rejects_traversal(ds_dir, bad):
    with pytest.raises(ValueError):
        dataset_config.analysis_file("ds", bad)


def test_analysis_out_dir_create_true_makes_tree(ds_dir):
    p = dataset_config.analysis_out_dir("ds", "an1", create=True)
    assert p == ds_dir / "_work" / "analyses" / "an1"
    assert p.is_dir()


def test_state_dir_create_false_no_mkdir(ds_dir):
    d = dataset_config.state_dir("ds", "an1", create=False)
    assert d == ds_dir / "_work" / "analyses" / "an1" / "state"
    # create=False must not write myanalysis.toml nor grow the work_dir tree.
    assert not (ds_dir / "myanalysis.toml").exists()
    assert not (ds_dir / "_work").exists()


# ---- (d) WinFsp/rclone mount: resolve() raises, get_work_dir must survive ----

def test_get_work_dir_survives_winfsp_realpath_failure(ds_dir, monkeypatch):
    """On a WinFsp/rclone mount Path.resolve() raises OSError [WinError 1005].

    get_work_dir's containment check goes through common.paths.safe_resolve, which
    falls back to os.path.abspath, so the read path must not raise and must still
    return <dataset_dir>/_work. This is the exact failure that made a cloud-drive
    (rclone) dataset open empty — the OSError was swallowed by open_dataset's
    except and tab restore never ran.
    """
    import os

    def _boom(*_a, **_k):
        raise OSError(1005, "The volume does not contain a recognized file system")

    monkeypatch.setattr(os.path, "realpath", _boom)
    # Sanity: the mount really would break a naive resolve().
    with pytest.raises(OSError):
        (ds_dir / "_work").resolve()

    wd = dataset_config.get_work_dir("ds", create=False)
    assert wd == ds_dir / "_work"


def test_analysis_file_survives_winfsp_realpath_failure(ds_dir, monkeypatch):
    """analysis_file's containment check also routes through safe_resolve, so it
    must resolve on a WinFsp/rclone mount (realpath raising) instead of crashing —
    this is the code path that opening an analysis tab hits."""
    import os

    def _boom(*_a, **_k):
        raise OSError(1005, "The volume does not contain a recognized file system")

    monkeypatch.setattr(os.path, "realpath", _boom)
    f = dataset_config.analysis_file("ds", "an1")  # must not raise
    assert str(f) == os.path.abspath(
        str(ds_dir / "analyses" / "an1" / "analysis.py")
    )


def test_state_dir_create_true_makes_tree(ds_dir):
    d = dataset_config.state_dir("ds", "an1", create=True)
    assert d.is_dir()
    assert (ds_dir / "myanalysis.toml").is_file()


def test_batch_dir_create_true(ds_dir):
    d = dataset_config.batch_dir("ds", "an1")
    assert d == ds_dir / "_work" / "analyses" / "an1" / "batch"
    assert d.is_dir()


def test_get_work_dir_create_false_no_side_effects(ds_dir):
    out = dataset_config.get_work_dir("ds", create=False)
    assert out == ds_dir / "_work"
    assert not out.exists()
    assert not (ds_dir / "myanalysis.toml").exists()
