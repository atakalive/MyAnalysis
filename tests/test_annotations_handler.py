"""Tests for AnalysisTab.connect_annotations / apply_annotations dispatch logic.

Verifies handler invocation, exception catching, and disconnect without
requiring a running Qt event loop — AnalysisTab is instantiated with a
minimal QApplication (via pytest-qt's qapp fixture if available, else the
test is skipped).
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def tab():
    """Create a bare AnalysisTab. Skip if no display is available."""
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
    except Exception:
        pytest.skip("Qt display not available")
    from gui.tab import AnalysisTab

    return AnalysisTab(name="test")


class TestAnnotationsHandler:
    def test_handler_called(self, tab):
        called_with = {}

        def handler(ann):
            called_with["ann"] = ann

        tab.connect_annotations(handler)
        tab.apply_annotations({"markers": [{"x": 1}], "notes": []})
        assert called_with["ann"] == {"markers": [{"x": 1}], "notes": []}

    def test_no_handler_noop(self, tab):
        tab.apply_annotations({"markers": [], "notes": []})

    def test_handler_exception_caught(self, tab):
        def bad_handler(ann):
            raise ValueError("bad data")

        tab.connect_annotations(bad_handler)
        tab.apply_annotations({"markers": "invalid"})

    def test_disconnect_handler(self, tab):
        called = []

        tab.connect_annotations(lambda ann: called.append(ann))
        tab.apply_annotations({"markers": [], "notes": []})
        assert len(called) == 1

        tab.connect_annotations(None)
        tab.apply_annotations({"markers": [], "notes": []})
        assert len(called) == 1
