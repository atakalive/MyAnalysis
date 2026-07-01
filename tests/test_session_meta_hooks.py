"""Tests for the meta.json / MRU hooks in llm_bridge.session (#50).

open_dataset (resolved path) → rebuild_meta(heavy=False) + note_recent_dataset;
error path → neither. save_all → rebuild_meta(heavy=False) per saved dataset
without touching saved/failed/clear_session_dirty.
"""
from __future__ import annotations

import pytest

import config
from llm_bridge import session, dataset_meta
from llm_bridge import paths as lb_paths


@pytest.fixture()
def ds_env(monkeypatch, tmp_path):
    a = tmp_path / "ds_a"
    b = tmp_path / "ds_b"
    (a / "_work").mkdir(parents=True)
    (b / "_work").mkdir(parents=True)
    mapping = {"ds_a": a, "ds_b": b}
    monkeypatch.setattr(config, "DATASETS", {"ds_a": {}, "ds_b": {}})
    monkeypatch.setattr(config, "get_dataset_dir", lambda name: mapping[name])
    monkeypatch.setattr(config, "reload_datasets", lambda config_path=None: None)
    return mapping


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


class _DispatchWindow(_FakeWindow):
    def __init__(self):
        super().__init__([], active=None)
        self._dirty = False
        self._suppress = False

    def note_current_dataset(self, name):
        pass

    def dispatch_command(self, verb, **kwargs):
        pass

    def is_session_dirty(self):
        return self._dirty

    def set_suppress_dirty(self, b):
        self._suppress = b

    def mark_session_dirty(self):
        if not self._suppress:
            self._dirty = True

    def set_active_tab(self, name):
        return False

    def close_tab(self, name):
        return False


def test_open_dataset_resolved_calls_hooks(ds_env, monkeypatch):
    session._touched.clear()
    calls = {"rebuild": [], "mru": []}
    monkeypatch.setattr(dataset_meta, "rebuild_meta",
                        lambda ds, *, heavy=True, should_stop=None:
                        calls["rebuild"].append((ds, heavy)))
    monkeypatch.setattr(lb_paths, "note_recent_dataset",
                        lambda ds: calls["mru"].append(ds))
    result = session.open_dataset(_DispatchWindow(), "ds_a")
    assert result.startswith("no-session:")
    assert calls["rebuild"] == [("ds_a", False)]      # heavy=False
    assert calls["mru"] == ["ds_a"]


def test_open_dataset_error_no_hooks(ds_env, monkeypatch):
    session._touched.clear()
    calls = {"rebuild": [], "mru": []}
    monkeypatch.setattr(dataset_meta, "rebuild_meta",
                        lambda ds, *, heavy=True, should_stop=None:
                        calls["rebuild"].append(ds))
    monkeypatch.setattr(lb_paths, "note_recent_dataset",
                        lambda ds: calls["mru"].append(ds))
    monkeypatch.setattr(config, "get_dataset_dir",
                        lambda name: (_ for _ in ()).throw(RuntimeError("no host")))
    result = session.open_dataset(_DispatchWindow(), "ds_a")
    assert result.startswith("error:")
    assert calls["rebuild"] == []
    assert calls["mru"] == []


def test_save_all_rebuilds_each_saved(ds_env, monkeypatch):
    session._touched.clear()
    calls = []
    monkeypatch.setattr(dataset_meta, "rebuild_meta",
                        lambda ds, *, heavy=True, should_stop=None:
                        calls.append((ds, heavy)))
    spec_a = {"kind": "figure", "name": "fa", "dataset": "ds_a",
              "figure": str(ds_env["ds_a"] / "_work" / "fa.png")}
    win = _FakeWindow([_FakeTab("fa", spec_a)], active=None)
    saved, failed = session.save_all(win)
    assert saved == ["ds_a"]
    assert failed == []
    assert win.dirty_cleared is True
    assert calls == [("ds_a", False)]                # heavy=False


def test_save_all_hook_failure_does_not_break(ds_env, monkeypatch):
    session._touched.clear()

    def boom(ds, *, heavy=True, should_stop=None):
        raise RuntimeError("meta boom")
    monkeypatch.setattr(dataset_meta, "rebuild_meta", boom)
    spec_a = {"kind": "figure", "name": "fa", "dataset": "ds_a",
              "figure": str(ds_env["ds_a"] / "_work" / "fa.png")}
    win = _FakeWindow([_FakeTab("fa", spec_a)], active=None)
    saved, failed = session.save_all(win)
    assert saved == ["ds_a"]      # hook failure isolated; save unaffected
    assert win.dirty_cleared is True
