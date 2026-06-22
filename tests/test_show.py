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


def test_copy_image_to_clipboard(win, png_path, qapp):
    win.dispatch_command("show", path=str(png_path))
    panel = win.active_tab().panel("figure")
    assert panel.copy_image_to_clipboard() is True
    from PySide6.QtWidgets import QApplication
    assert not QApplication.clipboard().pixmap().isNull()


def test_copy_image_to_clipboard_no_image(qapp):
    from gui.panels import FigurePanel
    panel = FigurePanel()
    assert panel.copy_image_to_clipboard() is False


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


@pytest.fixture()
def png_path2(tmp_path, qapp):
    from PySide6.QtGui import QPixmap
    p = tmp_path / "test2.png"
    QPixmap(20, 20).save(str(p))
    return p


@pytest.fixture()
def png_path3(tmp_path, qapp):
    from PySide6.QtGui import QPixmap
    p = tmp_path / "test3.png"
    QPixmap(30, 30).save(str(p))
    return p


@pytest.fixture()
def png_path4(tmp_path, qapp):
    from PySide6.QtGui import QPixmap
    p = tmp_path / "test4.png"
    QPixmap(40, 40).save(str(p))
    return p


def test_show_default_no_slot_right_hidden(win, png_path):
    win.dispatch_command("show", path=str(png_path))
    tab = win.active_tab()
    assert tab._right_container.isHidden()
    from gui.panels import FigurePanel
    assert isinstance(tab.panel("figure"), FigurePanel)


def test_show_slot_right_creates_secondary(win, png_path, png_path2):
    from PySide6.QtCore import Qt
    from gui.panels import FigurePanel
    win.dispatch_command("show", path=str(png_path))
    win.dispatch_command("show", path=str(png_path2), name="viewer", slot="right")
    tab = win.active_tab()
    assert isinstance(tab.panel("figure-2"), FigurePanel)
    assert not tab._left_container.isHidden()
    assert not tab._right_container.isHidden()
    assert tab._splitter.orientation() == Qt.Orientation.Horizontal
    assert not tab.panel("figure-2")._pixmap.isNull()


def test_show_slot_bottom_vertical(win, png_path):
    from PySide6.QtCore import Qt
    win.dispatch_command("show", path=str(png_path), slot="bottom")
    tab = win.active_tab()
    assert tab._splitter.orientation() == Qt.Orientation.Vertical


def test_show_slot_right_existing_tab_adds_secondary(win, png_path, png_path2):
    win.dispatch_command("show", path=str(png_path))
    win.dispatch_command("show", path=str(png_path2), name="viewer", slot="right")
    tab = win.active_tab()
    assert not tab.panel("figure-2")._pixmap.isNull()
    sizes = tab._splitter.sizes()
    assert abs(sizes[0] - sizes[1]) <= 1


def test_show_slot_right_updates_existing_secondary(win, png_path, png_path2, png_path3):
    win.dispatch_command("show", path=str(png_path))
    win.dispatch_command("show", path=str(png_path2), name="viewer", slot="right")
    result = win.dispatch_command(
        "show", path=str(png_path3), name="viewer", slot="right"
    )
    assert result == "updated:viewer"
    tab = win.active_tab()
    assert not tab.panel("figure-2")._pixmap.isNull()


def test_show_slot_right_only_new_tab(win, png_path):
    win.dispatch_command("show", path=str(png_path), slot="right")
    tab = win.active_tab()
    assert tab._left_container.isHidden()
    assert not tab._right_container.isHidden()
    assert not tab.panel("figure-2")._pixmap.isNull()


def test_show_slot_right_then_left(win, png_path, png_path2):
    win.dispatch_command("show", path=str(png_path), slot="right")
    win.dispatch_command("show", path=str(png_path2), name="viewer", slot="left")
    tab = win.active_tab()
    from gui.panels import FigurePanel
    assert isinstance(tab.panel("figure"), FigurePanel)
    assert not tab._left_container.isHidden()
    assert not tab._right_container.isHidden()
    sizes = tab._splitter.sizes()
    assert abs(sizes[0] - sizes[1]) <= 1


def test_show_no_slot_hides_secondary(win, png_path, png_path2, png_path3):
    win.dispatch_command("show", path=str(png_path))
    win.dispatch_command("show", path=str(png_path2), name="viewer", slot="right")
    win.dispatch_command("show", path=str(png_path3), name="viewer")
    tab = win.active_tab()
    assert tab._right_container.isHidden()


def test_show_slot_left_preserves_secondary(win, png_path, png_path2, png_path3):
    win.dispatch_command("show", path=str(png_path))
    win.dispatch_command("show", path=str(png_path2), name="viewer", slot="right")
    win.dispatch_command("show", path=str(png_path3), name="viewer", slot="left")
    tab = win.active_tab()
    assert not tab._right_container.isHidden()


def test_show_in_place_update_preserves_ratio(win, png_path, png_path2, png_path3):
    win.dispatch_command("show", path=str(png_path))
    win.dispatch_command("show", path=str(png_path2), name="viewer", slot="right")
    tab = win.active_tab()
    tab.set_split_ratio(3, 1)
    before = tab._splitter.sizes()
    win.dispatch_command("show", path=str(png_path3), name="viewer", slot="left")
    after = tab._splitter.sizes()
    assert before == after


def test_show_collapse_then_restore_via_slot(
    win, png_path, png_path2, png_path3, png_path4
):
    from PySide6.QtCore import Qt
    win.dispatch_command("show", path=str(png_path))
    win.dispatch_command("show", path=str(png_path2), name="viewer", slot="right")
    win.dispatch_command("show", path=str(png_path3), name="viewer")
    win.dispatch_command("show", path=str(png_path4), name="viewer", slot="right")
    tab = win.active_tab()
    assert not tab._left_container.isHidden()
    assert not tab._right_container.isHidden()
    assert tab._splitter.orientation() == Qt.Orientation.Horizontal
    assert all(s > 0 for s in tab._splitter.sizes())


def test_show_collapse_then_restore_vertical(
    win, png_path, png_path2, png_path3, png_path4
):
    from PySide6.QtCore import Qt
    win.dispatch_command("show", path=str(png_path))
    win.dispatch_command("show", path=str(png_path2), name="viewer", slot="bottom")
    win.dispatch_command("show", path=str(png_path3), name="viewer")
    win.dispatch_command("show", path=str(png_path4), name="viewer", slot="bottom")
    tab = win.active_tab()
    assert tab._splitter.orientation() == Qt.Orientation.Vertical
    assert all(s > 0 for s in tab._splitter.sizes())


def test_show_secondary_type_conflict(win, png_path, qapp):
    from gui.tab import AnalysisTab
    from gui.panels import FigurePanel
    from PySide6.QtWidgets import QLabel
    tab = AnalysisTab("mixed")
    tab.add_panel("figure", FigurePanel(), "left")
    tab.add_panel("figure-2", QLabel("nope"), "right")
    win.add_tab(tab)
    with pytest.raises(LookupError):
        win.dispatch_command("show", path=str(png_path), name="mixed", slot="right")


def test_show_invalid_slot(win, png_path):
    with pytest.raises(ValueError):
        win.dispatch_command("show", path=str(png_path), slot="center")


def test_cli_tab_gate_relaxed_for_viewer(monkeypatch):
    import llm_bridge.commands as cmds
    from llm_bridge.__main__ import main
    captured = {}

    def fake_submit(tier, target, verb, kwargs):
        captured.update(tier=tier, target=target, verb=verb, kwargs=kwargs)
        return "fake-id"

    monkeypatch.setattr(cmds, "submit", fake_submit)
    main(["tab", "viewer", "set-split", "left=2", "right=1"])
    assert captured["target"] == "viewer"
    assert captured["verb"] == "set-split"


def test_cli_tab_gate_rejects_traversal(monkeypatch):
    import llm_bridge.commands as cmds
    from llm_bridge.__main__ import main
    called = {"n": 0}

    def fake_submit(*a, **k):
        called["n"] += 1
        return "x"

    monkeypatch.setattr(cmds, "submit", fake_submit)
    with pytest.raises(SystemExit):
        main(["tab", "../evil", "set-split", "left=2", "right=1"])
    assert called["n"] == 0


def test_cli_list_commands_viewer(monkeypatch, capsys):
    import llm_bridge.commands as cmds
    from llm_bridge.__main__ import main
    called = {"n": 0}
    monkeypatch.setattr(cmds, "submit", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    main(["list-commands", "viewer"])
    out = capsys.readouterr().out
    assert "set-split" in out
    assert called["n"] == 0


def test_show_update_marks_dirty(win, png_path, tmp_path, qapp, monkeypatch):
    """show-update with dataset= must mark the window dirty (reviewer R1 P1)."""
    import config
    monkeypatch.setattr(config, "DATASETS", {"myds": {}})
    monkeypatch.setattr(config, "get_dataset_dir", lambda name: tmp_path)
    win.set_session_saver(lambda: ([], []))
    win.clear_session_dirty()

    win.dispatch_command("show", path=str(png_path), name="v", dataset="myds")
    win.clear_session_dirty()
    assert not win.is_session_dirty()

    from PySide6.QtGui import QPixmap
    p2 = tmp_path / "updated.png"
    QPixmap(20, 20).save(str(p2))
    result = win.dispatch_command("show", path=str(p2), name="v", dataset="myds")
    assert result == "updated:v"
    assert win.is_session_dirty()


def test_show_figure2_does_not_clobber_primary_spec(
    win, png_path, png_path2, tmp_path, qapp, monkeypatch
):
    """slot=right (figure-2) must NOT overwrite the primary session_spec
    (figure/dataset) — reviewer R5 P2 regression guard."""
    import config
    monkeypatch.setattr(config, "DATASETS", {"ds": {}})
    monkeypatch.setattr(config, "get_dataset_dir", lambda name: tmp_path)

    win.dispatch_command("show", path=str(png_path), name="viewer", dataset="ds")
    tab = win.active_tab()
    spec_before = dict(tab.session_spec)
    assert spec_before["figure"] == str(png_path.resolve())
    assert spec_before["dataset"] == "ds"

    win.dispatch_command("show", path=str(png_path2), name="viewer", slot="right")
    assert tab.session_spec["figure"] == str(png_path.resolve())
    assert tab.session_spec["dataset"] == "ds"
