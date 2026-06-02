import pytest


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def win(qapp):
    from gui.window import ToolWindow
    from llm_bridge import _make_show_handler
    w = ToolWindow()
    w.register_command("show", _make_show_handler(w))
    return w


@pytest.fixture()
def png_path(tmp_path, qapp):
    from PySide6.QtGui import QPixmap
    p = tmp_path / "test.png"
    QPixmap(10, 10).save(str(p))
    return p


def test_show_nonexistent_raises(win, tmp_path):
    with pytest.raises(LookupError):
        win.dispatch_command("show", path=str(tmp_path / "missing.png"))
    assert win.tab_names() == []


def test_show_new_tab(win, png_path):
    result = win.dispatch_command("show", path=str(png_path))
    assert result == "shown:viewer"
    assert "viewer" in win.tab_names()
    panel = win.active_tab().panel("figure")
    assert not panel._pixmap.isNull()


def test_show_update_existing(win, png_path, tmp_path, qapp):
    win.dispatch_command("show", path=str(png_path))
    from PySide6.QtGui import QPixmap
    p2 = tmp_path / "test2.png"
    QPixmap(20, 20).save(str(p2))
    result = win.dispatch_command("show", path=str(p2), name="viewer")
    assert result == "updated:viewer"
    assert win.tab_names().count("viewer") == 1


def test_show_different_name(win, png_path):
    win.dispatch_command("show", path=str(png_path))
    win.dispatch_command("show", path=str(png_path), name="other")
    assert "viewer" in win.tab_names()
    assert "other" in win.tab_names()
    assert len(win.tab_names()) == 2


def test_show_name_collision_with_analysis_tab(win, png_path, qapp):
    from gui.tab import AnalysisTab
    existing = AnalysisTab("conflict")
    win.add_tab(existing)
    with pytest.raises(LookupError, match="not a show-viewer tab"):
        win.dispatch_command("show", path=str(png_path), name="conflict")
    assert "conflict" in win.tab_names()
    assert len(win.tab_names()) == 1


def test_show_name_collision_figure_key_wrong_type(win, png_path, qapp):
    from gui.tab import AnalysisTab
    from PySide6.QtWidgets import QLabel
    tab = AnalysisTab("clash")
    tab.add_panel("figure", QLabel("not a FigurePanel"), "left")
    win.add_tab(tab)
    with pytest.raises(LookupError, match="not a show-viewer tab"):
        win.dispatch_command("show", path=str(png_path), name="clash")
    assert "clash" in win.tab_names()


def test_show_updates_compatible_non_show_tab(win, png_path, qapp):
    from gui.tab import AnalysisTab
    from gui.panels import FigurePanel
    tab = AnalysisTab("compat")
    panel = FigurePanel()
    tab.add_panel("figure", panel, "left")
    win.add_tab(tab)
    result = win.dispatch_command("show", path=str(png_path), name="compat")
    assert result == "updated:compat"
    assert not panel._pixmap.isNull()
