"""Tests for _write_active dataset extension (Qt-free)."""
import json
from unittest.mock import patch

from llm_bridge import _write_active
from llm_bridge.paths import active_state_path


class _FakeTab:
    def __init__(self, name):
        self.name = name


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


def test_write_active_includes_dataset(tmp_path):
    p = tmp_path / "active.json"
    with patch("llm_bridge.active_state_path", return_value=p):
        win = _Window(_FakeTab("analysis1"), "my_dataset")
        _write_active(win)
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data == {"active_tab": "analysis1", "dataset": "my_dataset"}


def test_write_active_no_current_dataset_attr(tmp_path):
    p = tmp_path / "active.json"
    with patch("llm_bridge.active_state_path", return_value=p):
        win = _WindowNoDataset(_FakeTab("viewer"))
        _write_active(win)
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data == {"active_tab": "viewer", "dataset": None}
