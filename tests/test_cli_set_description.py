"""Tests for the `set-description` CLI verb + list-datasets --json description."""
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


def test_set_description_writes_meta(ds_env):
    rc = bridge_main.main(["set-description", "ds", "x"])
    assert rc == 0
    assert dataset_meta.read_meta("ds")["description"] == "x"


def test_set_description_unknown_dataset(ds_env, capsys):
    rc = bridge_main.main(["set-description", "nope", "x"])
    assert rc == 1
    assert "unknown dataset" in capsys.readouterr().err


def test_list_datasets_json_has_description(ds_env, monkeypatch, capsys):
    monkeypatch.setattr(dataset_config, "load_config",
                        lambda name: {"work_dir": "_work", "format": "csv_per_subdir"})
    bridge_main.main(["set-description", "ds", "hello"])
    rc = bridge_main.main(["list-datasets", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    entry = next(e for e in data if e["name"] == "ds")
    assert {"name", "path", "format", "description"} <= set(entry)
    assert entry["description"] == "hello"
