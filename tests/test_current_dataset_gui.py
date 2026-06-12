"""GUI tests for window-level current dataset (offscreen Qt)."""
import pytest


@pytest.fixture
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_dataset_tab_switch_updates_current(qapp):
    """dataset 付きタブ切替で current_dataset 更新 + dataset_changed 発火。"""
    from gui.window import ToolWindow
    from gui.tab import AnalysisTab

    win = ToolWindow()
    win.show()

    tab_a = AnalysisTab("a")
    tab_a.session_spec = {"kind": "analysis", "name": "a", "dataset": "ds_a"}
    win.add_tab(tab_a)

    tab_b = AnalysisTab("b")
    tab_b.session_spec = {"kind": "analysis", "name": "b", "dataset": "ds_b"}
    win.add_tab(tab_b)

    signals = []
    win.dataset_changed.connect(lambda ds: signals.append(ds))

    win.set_active_tab("a")
    assert win.current_dataset == "ds_a"

    win.set_active_tab("b")
    assert win.current_dataset == "ds_b"
    assert "ds_b" in signals


def test_datasetless_tab_preserves_sticky(qapp):
    """dataset 無しタブ切替で sticky 保持。"""
    from gui.window import ToolWindow
    from gui.tab import AnalysisTab

    win = ToolWindow()
    win.show()

    tab_ds = AnalysisTab("ds_tab")
    tab_ds.session_spec = {"kind": "analysis", "name": "ds_tab", "dataset": "ds_x"}
    win.add_tab(tab_ds)

    tab_plain = AnalysisTab("plain")
    tab_plain.session_spec = None
    win.add_tab(tab_plain)

    win.set_active_tab("ds_tab")
    assert win.current_dataset == "ds_x"

    win.set_active_tab("plain")
    assert win.current_dataset == "ds_x"  # sticky


def test_notify_chat_dataset_updates_sticky(qapp):
    """notify_chat_dataset 経由で sticky 更新。"""
    from gui.window import ToolWindow
    from gui.tab import AnalysisTab

    win = ToolWindow()
    win.show()

    tab = AnalysisTab("t")
    tab.session_spec = {"kind": "figure", "name": "t", "dataset": "ds_y", "figure": "/tmp/x.png"}
    win.add_tab(tab)

    win.set_active_tab("t")
    win.notify_chat_dataset()
    assert win.current_dataset == "ds_y"
