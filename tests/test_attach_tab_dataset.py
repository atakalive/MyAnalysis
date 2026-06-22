"""Issue #37: attach_tab dataset wiring via the _building contextvar, and the
add-tab same-name cross-dataset collision guard."""

from __future__ import annotations

import json

import pytest

import llm_bridge


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_attach_tab_writes_to_dataset_state(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)
    (tmp_path / "dsx").mkdir()  # dataset dir exists on the synced drive
    from gui.tab import AnalysisTab

    tab = AnalysisTab("an1")
    with llm_bridge._building("dsx"):
        watchers = llm_bridge.attach_tab(tab, lambda: {"k": 1})
    assert tab.dataset == "dsx"
    assert watchers  # annotations watcher present
    tab.dispatch_command("refresh-state")
    sp = tmp_path / "dsx" / "_work" / "analyses" / "an1" / "state" / "current.json"
    assert json.loads(sp.read_text(encoding="utf-8")) == {"k": 1}


def test_attach_tab_no_dataset_is_noop(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)
    from gui.tab import AnalysisTab

    tab = AnalysisTab("an1")
    # No _building context → persistence wired as no-op, watcher list empty.
    watchers = llm_bridge.attach_tab(tab, lambda: {"k": 1})
    assert watchers == []
    assert tab.dataset is None
    tab.dispatch_command("refresh-state")  # must not raise, must not write
    assert list(tmp_path.iterdir()) == []  # nothing written to the synced drive


class _FakeTab:
    def __init__(self, name, dataset):
        self.name = name
        self.session_spec = {"kind": "analysis", "name": name, "dataset": dataset}


class _FakeWin:
    def __init__(self, tabs, current):
        self._tabs = tabs
        self.current_dataset = current
        self.activated = []

    def tabs(self):
        return self._tabs

    def set_active_tab(self, name):
        self.activated.append(name)
        return True


def test_add_tab_same_name_other_dataset_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)
    # The requested analysis must resolve to an existing file under dsB.
    d = tmp_path / "dsB" / "analyses" / "demo"
    d.mkdir(parents=True)
    (d / "analysis.py").write_text("def build_tab(p, d): ...\n", encoding="utf-8")

    existing = _FakeTab("demo", "dsA")
    win = _FakeWin([existing], current="dsB")
    add_tab = llm_bridge._make_add_tab_handler(win)
    with pytest.raises(ValueError, match="already open for dataset"):
        add_tab("demo", dataset="dsB")


def test_add_tab_same_name_same_dataset_focuses(monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)
    d = tmp_path / "dsA" / "analyses" / "demo"
    d.mkdir(parents=True)
    (d / "analysis.py").write_text("def build_tab(p, d): ...\n", encoding="utf-8")

    existing = _FakeTab("demo", "dsA")
    win = _FakeWin([existing], current="dsA")
    add_tab = llm_bridge._make_add_tab_handler(win)
    assert add_tab("demo", dataset="dsA") == "already-present:demo"
    assert win.activated == ["demo"]
