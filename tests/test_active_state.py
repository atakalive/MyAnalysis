"""Tests for _write_active dataset extension (Qt-free)."""
import json
from unittest.mock import patch

from llm_bridge import _write_active
from llm_bridge.paths import active_state_path


class _FakeTab:
    def __init__(self, name, session_spec=None):
        self.name = name
        if session_spec is not None:
            self.session_spec = session_spec


class _Window:
    def __init__(self, tab=None, dataset=None):
        self._tab = tab
        self._dataset = dataset

    def active_tab(self):
        return self._tab

    @property
    def current_dataset(self):
        return self._dataset


class _WindowNoDataset:
    """Window without current_dataset property (pre-upgrade compatibility)."""
    def __init__(self, tab=None):
        self._tab = tab

    def active_tab(self):
        return self._tab


class _WindowOpen:
    """Window exposing open_dataset_names (group model) for the 5-key output."""
    def __init__(self, tab=None, dataset=None, open_names=()):
        self._tab = tab
        self._dataset = dataset
        self._open = list(open_names)

    def active_tab(self):
        return self._tab

    @property
    def current_dataset(self):
        return self._dataset

    def open_dataset_names(self):
        return list(self._open)


def test_write_active_includes_dataset(tmp_path):
    p = tmp_path / "active.json"
    with patch("llm_bridge.active_state_path", return_value=p):
        tab = _FakeTab(
            "analysis1",
            session_spec={"kind": "analysis", "dataset": "my_dataset"},
        )
        win = _Window(tab, "my_dataset")
        _write_active(win)
        data = json.loads(p.read_text(encoding="utf-8"))
        # _Window has no open_dataset_names → getattr default [] (the []-default
        # path; the populated path is covered by test_write_active_open_datasets).
        assert data == {
            "active_tab": "analysis1",
            "dataset": "my_dataset",
            "active_analysis_dataset": "my_dataset",
            "open_datasets": [],
            "active_dataset": "my_dataset",
        }


def test_write_active_no_current_dataset_attr(tmp_path):
    p = tmp_path / "active.json"
    with patch("llm_bridge.active_state_path", return_value=p):
        win = _WindowNoDataset(_FakeTab("viewer"))
        _write_active(win)
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data == {
            "active_tab": "viewer",
            "dataset": None,
            "active_analysis_dataset": None,
            "open_datasets": [],
            "active_dataset": None,
        }


def test_write_active_open_datasets(tmp_path):
    """A window exposing open_dataset_names writes the real 5-key output."""
    p = tmp_path / "active.json"
    with patch("llm_bridge.active_state_path", return_value=p):
        tab = _FakeTab(
            "analysis1", session_spec={"kind": "analysis", "dataset": "dsA"}
        )
        win = _WindowOpen(tab, "dsA", open_names=["dsA", "dsB"])
        _write_active(win)
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data == {
            "active_tab": "analysis1",
            "dataset": "dsA",
            "active_analysis_dataset": "dsA",
            "open_datasets": ["dsA", "dsB"],
            "active_dataset": "dsA",
        }


def test_write_active_figure_tab_no_analysis_dataset(tmp_path):
    """figure viewer タブが active のとき active_analysis_dataset は None。"""
    p = tmp_path / "active.json"
    with patch("llm_bridge.active_state_path", return_value=p):
        tab = _FakeTab(
            "viewer", session_spec={"kind": "figure", "dataset": "dsA"}
        )
        win = _Window(tab, "dsA")
        _write_active(win)
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["active_analysis_dataset"] is None
        assert data["dataset"] == "dsA"


def test_write_active_analysis_tab_sets_analysis_dataset(tmp_path):
    """解析タブが active のときだけ active_analysis_dataset に dataset が入る。"""
    p = tmp_path / "active.json"
    with patch("llm_bridge.active_state_path", return_value=p):
        tab = _FakeTab(
            "a", session_spec={"kind": "analysis", "dataset": "dsA"}
        )
        win = _Window(tab, "dsB")
        _write_active(win)
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["active_analysis_dataset"] == "dsA"
        assert data["dataset"] == "dsB"
