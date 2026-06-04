"""Tests for llm_bridge.session: per-dataset session.json round-trip (Qt-free).

config.DATASETS / config.get_dataset_dir are monkeypatched to tmp dirs so no
real data dir is touched. These exercise write_session/read_session round-trip,
infer_dataset prefix matching (longest match wins), and graceful None on
unregistered host / missing dataset.
"""

from __future__ import annotations

import pytest

import config
import dataset_config
from llm_bridge import session


@pytest.fixture()
def ds_env(monkeypatch, tmp_path):
    """Register two datasets, each mapped to its own tmp dir."""
    a_dir = tmp_path / "ds_a"
    b_dir = tmp_path / "ds_b"
    a_dir.mkdir()
    b_dir.mkdir()
    mapping = {"ds_a": a_dir, "ds_b": b_dir}
    monkeypatch.setattr(config, "DATASETS", {"ds_a": {}, "ds_b": {}})
    monkeypatch.setattr(config, "get_dataset_dir", lambda name: mapping[name])
    return mapping


# ---- round-trip ----

def test_write_read_roundtrip(ds_env):
    payload = {
        "version": 1,
        "dataset": "ds_a",
        "active_tab": "fig1",
        "tabs": [
            {"name": "fig1", "kind": "figure", "figure": "figures/fig1.png"},
        ],
    }
    session.write_session("ds_a", payload)
    got = session.read_session("ds_a")
    assert got == payload


def test_read_missing_returns_none(ds_env):
    assert session.read_session("ds_a") is None


def test_read_bad_json_returns_none(ds_env):
    work_dir = dataset_config.get_work_dir("ds_a")
    (work_dir / "session.json").write_text("{ not json", encoding="utf-8")
    assert session.read_session("ds_a") is None


def test_read_unknown_dataset_returns_none(ds_env):
    assert session.read_session("nope") is None


# ---- infer_dataset ----

def test_infer_dataset_matches(ds_env):
    work_dir = dataset_config.get_work_dir("ds_a")
    fig = work_dir / "figures" / "x.png"
    fig.parent.mkdir(parents=True, exist_ok=True)
    fig.write_bytes(b"")
    assert session.infer_dataset(str(fig)) == "ds_a"


def test_infer_dataset_no_match_returns_none(ds_env, tmp_path):
    outside = tmp_path / "outside" / "y.png"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"")
    assert session.infer_dataset(str(outside)) is None


def test_infer_dataset_longest_match_wins(monkeypatch, tmp_path):
    """Nested work_dirs: the deeper (longest) one is the correct owner."""
    outer = tmp_path / "outer"
    inner = outer / "nested"
    inner.mkdir(parents=True)
    monkeypatch.setattr(config, "DATASETS", {"outer": {}, "inner": {}})
    monkeypatch.setattr(
        config, "get_dataset_dir",
        lambda name: {"outer": outer, "inner": inner}[name],
    )
    # both use absolute work_dir = the dataset dir itself
    for ds, d in (("outer", outer), ("inner", inner)):
        (d / dataset_config.CONFIG_FILENAME).write_text(
            f'work_dir = {str(d)!r}\n', encoding="utf-8"
        )
    fig = inner / "z.png"
    fig.write_bytes(b"")
    assert session.infer_dataset(str(fig)) == "inner"


def test_infer_dataset_unregistered_host_no_raise(monkeypatch, tmp_path):
    """get_dataset_dir raising for a dataset must not abort inference."""
    good = tmp_path / "good"
    good.mkdir()

    def fake_get_dataset_dir(name):
        if name == "bad":
            raise RuntimeError("no host path")
        return good

    monkeypatch.setattr(config, "DATASETS", {"bad": {}, "good": {}})
    monkeypatch.setattr(config, "get_dataset_dir", fake_get_dataset_dir)
    fig = (good / "_work" / "f.png")
    fig.parent.mkdir(parents=True)
    fig.write_bytes(b"")
    assert session.infer_dataset(str(fig)) == "good"


# ---- save_all _touched lifecycle (Qt-free via a fake window) ----

class _FakeTab:
    def __init__(self, name, spec):
        self.name = name
        self.session_spec = spec


class _FakeWindow:
    def __init__(self, tabs, active=None):
        self._tabs = tabs
        self._active = active
        self.dirty_cleared = False

    def tabs(self):
        return self._tabs

    def active_tab(self):
        return self._active

    def clear_session_dirty(self):
        self.dirty_cleared = True


def test_save_all_partial_failure(ds_env, monkeypatch):
    session._touched.clear()
    spec_a = {"kind": "figure", "name": "fa", "dataset": "ds_a",
              "figure": str(ds_env["ds_a"] / "_work" / "fa.png")}
    spec_b = {"kind": "figure", "name": "fb", "dataset": "ds_b",
              "figure": str(ds_env["ds_b"] / "_work" / "fb.png")}
    tabs = [_FakeTab("fa", spec_a), _FakeTab("fb", spec_b)]
    win = _FakeWindow(tabs, active=tabs[0])

    orig = dataset_config.get_work_dir

    def failing(name):
        if name == "ds_b":
            raise RuntimeError("no host path")
        return orig(name)

    monkeypatch.setattr(dataset_config, "get_work_dir", failing)
    saved, failed = session.save_all(win)
    assert saved == ["ds_a"]
    assert failed == ["ds_b"]
    assert not win.dirty_cleared  # dirty kept on partial failure


def test_save_all_touched_removed_after_empty_save(ds_env):
    session._touched.clear()
    # First: a tab present for ds_a → save.
    spec_a = {"kind": "figure", "name": "fa", "dataset": "ds_a",
              "figure": str(ds_env["ds_a"] / "_work" / "fa.png")}
    win = _FakeWindow([_FakeTab("fa", spec_a)], active=None)
    session.note_dataset("ds_a")
    session.save_all(win)
    assert "ds_a" in session._touched  # kept after non-empty save

    # Then: all tabs closed → re-save writes empty tabs:[] and removes from _touched.
    win2 = _FakeWindow([], active=None)
    saved, failed = session.save_all(win2)
    assert "ds_a" in saved
    assert "ds_a" not in session._touched
    data = session.read_session("ds_a")
    assert data["tabs"] == []
