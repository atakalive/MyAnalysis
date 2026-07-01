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
    def __init__(self, name, dataset, kind="analysis"):
        self.name = name
        self.session_spec = {"kind": kind, "name": name, "dataset": dataset}


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


def _write_analysis(tmp_path, ds, name):
    d = tmp_path / ds / "analyses" / name
    d.mkdir(parents=True)
    (d / "analysis.py").write_text(
        "import llm_bridge\n"
        "from gui.tab import AnalysisTab\n"
        "def load():\n    return None\n"
        "def build_tab(parent, data):\n"
        f"    tab = AnalysisTab({name!r})\n"
        "    llm_bridge.attach_tab(tab, lambda: {})\n"
        "    return tab\n",
        encoding="utf-8",
    )


def test_add_tab_same_name_other_dataset_creates_second(qapp, monkeypatch, tmp_path):
    """Issue #51: the same analysis name in a DIFFERENT dataset is allowed — it
    creates a second tab (one per dataset group), not a fail-fast collision."""
    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)
    (tmp_path / "dsA").mkdir()
    (tmp_path / "dsB").mkdir()
    _write_analysis(tmp_path, "dsA", "demo")
    _write_analysis(tmp_path, "dsB", "demo")
    from gui.window import ToolWindow

    win = ToolWindow()
    add_tab = llm_bridge._make_add_tab_handler(win)
    assert add_tab("demo", dataset="dsA").startswith("added")
    # Same (dataset, name) → focus (already-present), no duplicate.
    assert add_tab("demo", dataset="dsA") == "already-present:demo"
    # Same name, other dataset → a NEW tab in dsB's group.
    assert add_tab("demo", dataset="dsB").startswith("added")
    assert sorted(win.open_dataset_names()) == ["dsA", "dsB"]
    assert win.find_tab("demo", "dsA") is not None
    assert win.find_tab("demo", "dsB") is not None
    assert win.find_tab("demo", "dsA") is not win.find_tab("demo", "dsB")


def test_find_tab_ambiguous_bare_name(qapp, monkeypatch, tmp_path):
    """A bare name matching two datasets → LookupError unless current resolves it."""
    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)
    (tmp_path / "dsA").mkdir()
    (tmp_path / "dsB").mkdir()
    _write_analysis(tmp_path, "dsA", "demo")
    _write_analysis(tmp_path, "dsB", "demo")
    from gui.window import ToolWindow

    win = ToolWindow()
    add_tab = llm_bridge._make_add_tab_handler(win)
    add_tab("demo", dataset="dsA")
    add_tab("demo", dataset="dsB")
    # No match → None; explicit dataset → resolves; ambiguous bare → LookupError.
    assert win.find_tab("missing") is None
    assert win.find_tab("demo", "dsA") is not None
    win.set_active_dataset("dsB")
    assert win.find_tab("demo") is win.find_tab("demo", "dsB")  # current wins
    win.set_active_dataset("dsA")
    assert win.find_tab("demo") is win.find_tab("demo", "dsA")


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


def test_add_tab_same_name_figure_viewer_raises(monkeypatch, tmp_path):
    """同じ dataset に同名の figure viewer があると、解析タブは fail-fast する
    (viewer を解析と取り違えて focus しない)。reviewer P2 code review。"""
    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)
    d = tmp_path / "dsA" / "analyses" / "demo"
    d.mkdir(parents=True)
    (d / "analysis.py").write_text("def build_tab(p, d): ...\n", encoding="utf-8")

    # 同名 (demo)・同 dataset (dsA) の figure viewer が既に開いている。
    existing = _FakeTab("demo", "dsA", kind="figure")
    win = _FakeWin([existing], current="dsA")
    add_tab = llm_bridge._make_add_tab_handler(win)
    with pytest.raises(ValueError, match="already open"):
        add_tab("demo", dataset="dsA")
    assert win.activated == []  # viewer を focus しない
