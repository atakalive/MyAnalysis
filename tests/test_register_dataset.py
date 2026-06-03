"""Tests for config.register_dataset — pure file-writer registration.

A temporary config.py-like file is written to tmp_path and passed via
config_path=. The real repo config.py is never touched. The in-memory
config.DATASETS global must remain unchanged (memory reflection is the GUI's
responsibility, not register_dataset's).
"""

from __future__ import annotations

import ast

import pytest

import config


_TEMPLATE = '''\
"""docstring line 1
docstring line 2
"""
import socket
from pathlib import Path

DATASETS: dict[str, dict[str, str]] = {
    "dataset_a": {
        "HOST_A": r"G:\\同期\\測定\\000000\\example",
        "HOST_B": r"H:\\同期\\測定\\000000\\example",
    },
}


def after():
    return DATASETS
'''


def _datasets_from(path):
    """Parse the file and ast.literal_eval its DATASETS value."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for stmt in tree.body:
        if isinstance(stmt, ast.AnnAssign) and getattr(stmt.target, "id", None) == "DATASETS":
            return ast.literal_eval(ast.get_source_segment(source, stmt.value))
        if isinstance(stmt, ast.Assign) and any(
            getattr(t, "id", None) == "DATASETS" for t in stmt.targets
        ):
            return ast.literal_eval(ast.get_source_segment(source, stmt.value))
    raise AssertionError("DATASETS not found")


@pytest.fixture()
def cfg(tmp_path):
    p = tmp_path / "config.py"
    p.write_text(_TEMPLATE, encoding="utf-8")
    return p


def test_new_dataset_created(cfg):
    result = config.register_dataset(
        "new_dataset", r"G:\new\path", host="host_a", config_path=cfg
    )
    assert result["created"] is True
    assert result["host"] == "HOST_A"
    data = _datasets_from(cfg)
    assert data["new_dataset"] == {"HOST_A": r"G:\new\path"}


def test_merge_host_into_existing(cfg):
    result = config.register_dataset(
        "dataset_a", r"J:\foo", host="newhost", config_path=cfg
    )
    assert result["created"] is False
    data = _datasets_from(cfg)
    assert data["dataset_a"]["NEWHOST"] == r"J:\foo"
    assert "HOST_A" in data["dataset_a"]


def test_overwrite_existing_host_path(cfg):
    result = config.register_dataset(
        "dataset_a", r"Z:\override", host="HOST_A", config_path=cfg
    )
    assert result["created"] is False
    data = _datasets_from(cfg)
    assert data["dataset_a"]["HOST_A"] == r"Z:\override"


def test_host_uppercased(cfg):
    result = config.register_dataset(
        "foo", r"G:\foo", host="lowerhost", config_path=cfg
    )
    assert result["host"] == "LOWERHOST"


def test_backslash_path_roundtrips_as_raw(cfg):
    config.register_dataset("foo", r"G:\a\b\c", host="H1", config_path=cfg)
    text = cfg.read_text(encoding="utf-8")
    assert r'r"G:\a\b\c"' in text
    data = _datasets_from(cfg)
    assert data["foo"]["H1"] == r"G:\a\b\c"


def test_trailing_backslash_path_fallback(cfg):
    p = "G:\\a\\b\\"
    config.register_dataset("foo", p, host="H1", config_path=cfg)
    data = _datasets_from(cfg)
    assert data["foo"]["H1"] == p


def test_quote_in_path_fallback(cfg):
    # 末尾でない位置に " を含む絶対パス。
    p = '/a/b"c/d'
    config.register_dataset("foo", p, host="H1", config_path=cfg)
    data = _datasets_from(cfg)
    assert data["foo"]["H1"] == p


def test_control_char_path_fallback(cfg):
    p = "/a/b\nc"
    config.register_dataset("foo", p, host="H1", config_path=cfg)
    data = _datasets_from(cfg)
    assert data["foo"]["H1"] == p


def test_unrelated_lines_unchanged(cfg):
    before = cfg.read_text(encoding="utf-8").splitlines()
    config.register_dataset("foo", r"G:\foo", host="H1", config_path=cfg)
    after = cfg.read_text(encoding="utf-8").splitlines()
    # docstring + imports (上3行+import 2行) と末尾 after() 関数が無改変。
    assert before[:6] == after[:6]
    assert before[-4:] == after[-4:]


def test_invalid_dataset_name(cfg):
    with pytest.raises(ValueError):
        config.register_dataset("Bad", r"G:\foo", host="H1", config_path=cfg)


@pytest.mark.parametrize("host", ['ba"d', "ba\\d", "bad\n", "-lead"])
def test_invalid_hostname(cfg, host):
    with pytest.raises(ValueError):
        config.register_dataset("foo", r"G:\foo", host=host, config_path=cfg)


def test_relative_path_rejected(cfg):
    with pytest.raises(ValueError):
        config.register_dataset("foo", "relative/path", host="H1", config_path=cfg)


def test_windows_path_absolute_on_posix(cfg):
    # WSL/POSIX 上でも G:\... は絶対と判定される (OS 非依存チェック)。
    result = config.register_dataset("foo", r"G:\foo", host="H1", config_path=cfg)
    assert result["created"] is True


def test_datasets_not_found(tmp_path):
    p = tmp_path / "nodatasets.py"
    p.write_text("X = 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        config.register_dataset("foo", r"G:\foo", host="H1", config_path=p)


def test_reserved_name_accepted_for_dataset(cfg):
    # データセット名は check_reserved=False なので "con" を受理する。
    result = config.register_dataset("con", r"G:\foo", host="H1", config_path=cfg)
    assert result["created"] is True
    data = _datasets_from(cfg)
    assert data["con"] == {"H1": r"G:\foo"}


def test_does_not_mutate_global_datasets(cfg):
    before = {k: dict(v) for k, v in config.DATASETS.items()}
    config.register_dataset("brand_new_xyz", r"G:\foo", host="H1", config_path=cfg)
    assert config.DATASETS == before
    assert "brand_new_xyz" not in config.DATASETS
