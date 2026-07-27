"""Tests for the `set-completed` CLI verb + list-datasets --json completed."""
from __future__ import annotations

import json

import pytest

import config
import dataset_config
from llm_bridge import dataset_meta
from llm_bridge import __main__ as bridge_main


@pytest.fixture()
def ds_env(monkeypatch, tmp_path):
    d = tmp_path / "ds"
    d.mkdir()
    mapping = {"ds": d}
    monkeypatch.setattr(config, "DATASETS", {"ds": {"H": str(d)}})
    monkeypatch.setattr(config, "get_dataset_dir", lambda name: mapping[name])
    monkeypatch.setattr(config, "reload_datasets", lambda config_path=None: None)
    return d


def test_set_completed_writes_meta(ds_env):
    rc = bridge_main.main(["set-completed", "ds"])
    assert rc == 0
    assert dataset_meta.read_meta("ds")["completed"] is True


def test_set_completed_off_writes_false(ds_env):
    bridge_main.main(["set-completed", "ds"])
    rc = bridge_main.main(["set-completed", "ds", "--off"])
    assert rc == 0
    assert dataset_meta.read_meta("ds")["completed"] is False


def test_set_completed_unknown_dataset(ds_env, capsys):
    rc = bridge_main.main(["set-completed", "nope"])
    assert rc == 1
    assert "unknown dataset" in capsys.readouterr().err


def test_set_completed_coexists_with_description(ds_env):
    bridge_main.main(["set-description", "ds", "hi"])
    bridge_main.main(["set-completed", "ds"])
    meta = dataset_meta.read_meta("ds")
    assert meta["description"] == "hi"
    assert meta["completed"] is True


def test_list_datasets_json_has_completed(ds_env, monkeypatch, capsys):
    monkeypatch.setattr(dataset_config, "load_config",
                        lambda name: {"work_dir": "_work", "format": "csv_per_subdir"})
    bridge_main.main(["set-description", "ds", "hello"])
    bridge_main.main(["set-completed", "ds"])
    rc = bridge_main.main(["list-datasets", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    entry = next(e for e in data if e["name"] == "ds")
    assert entry["completed"] is True
    assert entry["description"] == "hello"


def test_list_datasets_json_completed_default_false(ds_env, monkeypatch, capsys):
    monkeypatch.setattr(dataset_config, "load_config",
                        lambda name: {"work_dir": "_work", "format": "csv_per_subdir"})
    rc = bridge_main.main(["list-datasets", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    entry = next(e for e in data if e["name"] == "ds")
    assert entry["completed"] is False
    assert entry["description"] == ""
