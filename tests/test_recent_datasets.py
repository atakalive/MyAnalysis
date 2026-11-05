"""Tests for the PC-local MRU (recent_datasets.json) accessors in llm_bridge.paths."""
from __future__ import annotations

import json

import pytest

from llm_bridge import paths


@pytest.fixture()
def mru_env(monkeypatch, tmp_path):
    p = tmp_path / "recent_datasets.json"
    monkeypatch.setattr(paths, "recent_datasets_path", lambda: p)
    return p


def test_round_trip(mru_env):
    paths.note_recent_dataset("alpha")
    paths.note_recent_dataset("beta")
    data = paths.read_recent_datasets()
    assert set(data) == {"alpha", "beta"}
    assert all(isinstance(v, float) for v in data.values())


def test_corrupt_returns_empty(mru_env):
    mru_env.write_text("{bad json", encoding="utf-8")
    assert paths.read_recent_datasets() == {}


def test_non_dict_returns_empty(mru_env):
    mru_env.write_text("[1,2,3]", encoding="utf-8")
    assert paths.read_recent_datasets() == {}


def test_bad_value_types_dropped(mru_env):
    mru_env.write_text(json.dumps({"ds": "bad", "ok": 12.5, "b": True}),
                       encoding="utf-8")
    data = paths.read_recent_datasets()
    assert data == {"ok": 12.5}   # str and bool dropped


def test_note_never_raises(monkeypatch, tmp_path):
    # unwritable path → note swallows the error
    import common.paths
    # 書込は毎回失敗するので、検証の 5 回の試行の待ち（約 6 秒）だけを省く（回数は変えない）。
    monkeypatch.setattr(common.paths, "_VERIFY_BACKOFF",
                        (0.0,) * len(common.paths._VERIFY_BACKOFF))
    monkeypatch.setattr(paths, "recent_datasets_path",
                        lambda: tmp_path / "nope" / "x.json")
    paths.note_recent_dataset("ds")   # must not raise
