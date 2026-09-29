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


# --- Issue #89: last-good .bak snapshot on add-tab success -------------------

import common.paths as _cp  # noqa: E402
from common.paths import bak_path  # noqa: E402


def _bak_of(tmp_path, ds, name):
    return bak_path(tmp_path / ds / "analyses" / name / "analysis.py")


def _bak_write_spy(monkeypatch):
    """Count atomic_write_text calls that target an analysis.py.bak."""
    real = _cp.atomic_write_text
    hits = []

    def spy(path, text, **kw):
        if str(path).endswith("analysis.py.bak"):
            hits.append(str(path))
        return real(path, text, **kw)

    monkeypatch.setattr(_cp, "atomic_write_text", spy)
    return hits


def test_add_tab_success_snapshots_bak(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)
    (tmp_path / "dsA").mkdir()
    _write_analysis(tmp_path, "dsA", "demo")
    from gui.window import ToolWindow

    win = ToolWindow()
    add_tab = llm_bridge._make_add_tab_handler(win)
    assert add_tab("demo", dataset="dsA").startswith("added")
    bak = _bak_of(tmp_path, "dsA", "demo")
    src = (tmp_path / "dsA" / "analyses" / "demo" / "analysis.py").read_text(encoding="utf-8")
    assert bak.read_text(encoding="utf-8") == src


def test_add_tab_reopen_same_content_no_rewrite(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)
    (tmp_path / "dsA").mkdir()
    _write_analysis(tmp_path, "dsA", "demo")
    from gui.window import ToolWindow

    win = ToolWindow()
    add_tab = llm_bridge._make_add_tab_handler(win)
    add_tab("demo", dataset="dsA")
    assert _bak_of(tmp_path, "dsA", "demo").is_file()
    # Close and re-add with identical content: .bak must not be rewritten.
    hits = _bak_write_spy(monkeypatch)
    win.close_tab("demo", dataset="dsA")
    add_tab("demo", dataset="dsA")
    assert hits == []


def test_add_tab_zero_byte_with_bak_points_at_recover(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)
    (tmp_path / "dsA").mkdir()
    _write_analysis(tmp_path, "dsA", "demo")
    af = tmp_path / "dsA" / "analyses" / "demo" / "analysis.py"
    bak_path(af).write_text("last good\n", encoding="utf-8")
    af.write_text("", encoding="utf-8")   # 0-byte truncation
    from gui.window import ToolWindow

    win = ToolWindow()
    add_tab = llm_bridge._make_add_tab_handler(win)
    with pytest.raises(ValueError, match="recover-analysis"):
        add_tab("demo", dataset="dsA")


def test_add_tab_zero_byte_no_bak_reports_missing(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)
    (tmp_path / "dsA").mkdir()
    _write_analysis(tmp_path, "dsA", "demo")
    af = tmp_path / "dsA" / "analyses" / "demo" / "analysis.py"
    af.write_text("", encoding="utf-8")   # 0-byte, no .bak
    from gui.window import ToolWindow

    win = ToolWindow()
    add_tab = llm_bridge._make_add_tab_handler(win)
    with pytest.raises(ValueError, match="見つかりません"):
        add_tab("demo", dataset="dsA")


def test_add_tab_build_failure_writes_no_bak(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)
    (tmp_path / "dsA").mkdir()
    d = tmp_path / "dsA" / "analyses" / "demo"
    d.mkdir(parents=True)
    (d / "analysis.py").write_text("x = 1\n", encoding="utf-8")  # no build_tab
    from gui.window import ToolWindow

    win = ToolWindow()
    add_tab = llm_bridge._make_add_tab_handler(win)
    with pytest.raises(AttributeError):
        add_tab("demo", dataset="dsA")
    assert not _bak_of(tmp_path, "dsA", "demo").exists()


def test_add_tab_name_mismatch_writes_no_bak(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)
    (tmp_path / "dsA").mkdir()
    d = tmp_path / "dsA" / "analyses" / "demo"
    d.mkdir(parents=True)
    (d / "analysis.py").write_text(
        "import llm_bridge\n"
        "from gui.tab import AnalysisTab\n"
        "def build_tab(parent, data):\n"
        "    tab = AnalysisTab('WRONG')\n"   # name != 'demo'
        "    llm_bridge.attach_tab(tab, lambda: {})\n"
        "    return tab\n",
        encoding="utf-8",
    )
    from gui.window import ToolWindow

    win = ToolWindow()
    add_tab = llm_bridge._make_add_tab_handler(win)
    with pytest.raises(ValueError, match="name mismatch"):
        add_tab("demo", dataset="dsA")
    assert not _bak_of(tmp_path, "dsA", "demo").exists()


def test_add_tab_sync_state_failure_writes_no_bak(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)
    (tmp_path / "dsA").mkdir()
    d = tmp_path / "dsA" / "analyses" / "demo"
    d.mkdir(parents=True)
    (d / "analysis.py").write_text(
        "import llm_bridge\n"
        "from gui.tab import AnalysisTab\n"
        "def apply_state(tab, state):\n"
        "    raise RuntimeError('boom')\n"
        "def build_tab(parent, data):\n"
        "    tab = AnalysisTab('demo')\n"
        "    llm_bridge.attach_tab(tab, lambda: {})\n"
        "    return tab\n",
        encoding="utf-8",
    )
    from gui.window import ToolWindow

    win = ToolWindow()
    add_tab = llm_bridge._make_add_tab_handler(win)
    with pytest.raises(RuntimeError) as ei:
        add_tab("demo", dataset="dsA")
    assert not _bak_of(tmp_path, "dsA", "demo").exists()
    assert isinstance(ei.value.__cause__, RuntimeError)
    assert str(ei.value.__cause__) == "boom"
    assert "apply_state failed: RuntimeError: boom" in str(ei.value)


# ---- _sync_new_tab_state は失敗の原因を返す（Issue #100 D-4）----

def test_sync_new_tab_state_reports_apply_state_cause():
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    exc = ValueError("bad")

    def apply_state(tab, state):
        raise exc

    mod = SimpleNamespace(apply_state=apply_state)
    err = llm_bridge._sync_new_tab_state(MagicMock(), mod, "ds", "a", {})
    assert err == ("apply_state failed: ValueError: bad", exc)
    assert isinstance(err[1], ValueError)
    assert str(err[1]) == "bad"


def test_sync_new_tab_state_reports_refresh_cause():
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    exc = RuntimeError("x")
    tab = MagicMock()
    tab.dispatch_command.side_effect = exc
    err = llm_bridge._sync_new_tab_state(tab, SimpleNamespace(), "ds", "a", {})
    assert err == ("refresh-state failed: RuntimeError: x", exc)
    assert err[1] is exc


def test_sync_new_tab_state_success_returns_none():
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    mod = SimpleNamespace(apply_state=lambda tab, state: None)
    assert llm_bridge._sync_new_tab_state(MagicMock(), mod, "ds", "a", {}) is None
