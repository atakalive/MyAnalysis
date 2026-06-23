import pytest


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _make_bar(qapp, n, labels=None, movable=True, width=360):
    from PySide6.QtWidgets import QTabWidget, QWidget
    from gui.tabbar import MultiRowTabBar
    host = QTabWidget()
    bar = MultiRowTabBar()
    host.setTabBar(bar)
    host.setMovable(movable)
    for i in range(n):
        host.addTab(QWidget(), labels[i] if labels else f"TabLabel{i}")
    host.resize(width, 700)
    return host, bar


def _drag(bar):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    moved = []
    bar.tabMoved.connect(lambda f, t: moved.append((f, t)))
    bar._relayout(300)
    src = bar.tabRect(0).center()
    dst = bar.tabRect(3).center()
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(src), QPointF(bar.mapToGlobal(src)),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    bar.mousePressEvent(press)
    move = QMouseEvent(
        QEvent.Type.MouseMove, QPointF(dst), QPointF(bar.mapToGlobal(dst)),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,   # 移動中は左ボタン保持
        Qt.KeyboardModifier.NoModifier,
    )
    bar.mouseMoveEvent(move)
    return moved


# 1. 折り返し
def test_wraps_when_narrow(qapp):
    host, bar = _make_bar(qapp, 12)
    bar._relayout(300)
    assert bar._row_count > 1
    assert len({bar.tabRect(i).y() for i in range(12)}) > 1


# 2. 広いと 1 段
def test_single_row_when_wide(qapp):
    host, bar = _make_bar(qapp, 12)
    bar._relayout(4000)
    assert bar._row_count == 1
    assert all(bar.tabRect(i).y() == 0 for i in range(12))


# 3. sizeHint 高さが段数に追従
def test_sizehint_height_tracks_rows(qapp):
    host, bar = _make_bar(qapp, 12)
    bar._relayout(300)
    h_narrow = bar.sizeHint().height()
    assert bar.sizeHint().height() == bar._row_count * bar._row_height
    bar._relayout(4000)
    h_wide = bar.sizeHint().height()
    assert h_narrow > h_wide


# 4. 2 段目の tabAt
def test_second_row_tab_at(qapp):
    host, bar = _make_bar(qapp, 12)
    bar._relayout(300)
    second = [i for i in range(12) if bar.tabRect(i).y() == bar._row_height]
    assert second
    i = second[0]
    assert bar.tabAt(bar.tabRect(i).center()) == i
    from PySide6.QtCore import QPoint
    below = QPoint(5, bar._row_count * bar._row_height + 50)
    assert bar.tabAt(below) == -1


# 5. tabRect 一致 / 範囲外
def test_tabrect_match_and_out_of_range(qapp):
    host, bar = _make_bar(qapp, 12)
    bar._relayout(300)
    for i in range(12):
        assert bar.tabRect(i) == bar._rects[i]
    assert bar.tabRect(999).isNull()
    assert bar.tabRect(-1).isNull()


# 6. ドラッグ並べ替え（move セマンティクス）
def test_move_tab_semantics(qapp):
    host, bar = _make_bar(qapp, 12, labels=[f"T{i}" for i in range(12)])
    moved = []
    bar.tabMoved.connect(lambda f, t: moved.append((f, t)))
    bar.moveTab(0, 3)
    assert (0, 3) in moved
    assert [bar.tabText(i) for i in range(4)] == ["T1", "T2", "T3", "T0"]


# 7. 0 タブ / 1 タブのエッジ
def test_zero_and_one_tab_edges(qapp):
    from PySide6.QtCore import QPoint
    host0, bar0 = _make_bar(qapp, 0)
    bar0._relayout(300)
    assert bar0._rects == []
    assert bar0.sizeHint().height() > 0
    assert bar0.tabAt(QPoint(5, 5)) == -1
    host1, bar1 = _make_bar(qapp, 1)
    bar1._relayout(300)
    assert len(bar1._rects) == 1
    assert bar1.tabRect(0).y() == 0


# 8. 幅 < 1 タブでクランプ
def test_clamp_below_one_tab(qapp):
    host, bar = _make_bar(qapp, 1)
    bar._relayout(50)
    assert 0 < bar.tabRect(0).width() <= 50


# 9. ToolWindow 経由のコンテキストメニュー経路
def test_toolwindow_context_menu_path(qapp):
    from gui.window import ToolWindow
    from gui.tab import AnalysisTab
    from gui.tabbar import MultiRowTabBar
    win = ToolWindow()
    bar = win._tabs.tabBar()
    assert isinstance(bar, MultiRowTabBar)
    for i in range(12):
        win.add_tab(AnalysisTab(f"analysis{i}"))
    bar._relayout(300)
    second = [i for i in range(12) if bar.tabRect(i).y() == bar._row_height]
    assert second
    i = second[0]
    assert bar.tabAt(bar.tabRect(i).center()) == i


# 10. isMovable() ゲート
def test_movable_gate(qapp):
    host_m, bar_m = _make_bar(qapp, 12, movable=True)
    moved = _drag(bar_m)
    assert moved
    assert (0, 3) in moved
    host_n, bar_n = _make_bar(qapp, 12, movable=False)
    assert _drag(bar_n) == []


# 12. スクロールボタンを出さない（show 済み実機相当）
def test_no_scroll_buttons(qapp):
    from PySide6.QtWidgets import QAbstractButton
    host, bar = _make_bar(qapp, 14, width=360)
    host.show()
    qapp.processEvents()
    assert bar.usesScrollButtons() is False
    assert [c for c in bar.findChildren(QAbstractButton) if c.isVisible()] == []
    assert bar._row_count > 1
    assert bar.width() > 100


# 13. 実 ToolWindow をウィンドウごと狭めて折り返す
def test_toolwindow_layout_wrap(qapp):
    from gui.window import ToolWindow
    from gui.tab import AnalysisTab
    win = ToolWindow()
    bar = win._tabs.tabBar()
    for i in range(16):
        win.add_tab(AnalysisTab(f"analysis{i:02d}"))
    win.show()
    qapp.processEvents()
    win.resize(520, 800)
    qapp.processEvents()
    assert win.width() <= 560
    assert bar._row_count > 1
    narrow_rc = bar._row_count
    win.resize(1400, 800)
    qapp.processEvents()
    assert bar._row_count < narrow_rc


# 14. minimumSizeHint().width() がタブ数に依存しない
def test_min_width_independent_of_tab_count(qapp):
    from gui.window import ToolWindow
    from gui.tab import AnalysisTab
    win4 = ToolWindow()
    for i in range(4):
        win4.add_tab(AnalysisTab(f"analysis{i:02d}"))
    win4.show()
    qapp.processEvents()
    win16 = ToolWindow()
    for i in range(16):
        win16.add_tab(AnalysisTab(f"analysis{i:02d}"))
    win16.show()
    qapp.processEvents()
    assert (
        win4._tabs.tabBar().minimumSizeHint().width()
        == win16._tabs.tabBar().minimumSizeHint().width()
    )


# 15. minimumSizeHint().width() がラベル長にも依存しない
def test_min_width_independent_of_label_length(qapp):
    from gui.window import ToolWindow
    from gui.tab import AnalysisTab
    winA = ToolWindow()
    for i in range(16):
        winA.add_tab(AnalysisTab(f"analysis{i:02d}"))
    winA.show()
    qapp.processEvents()
    barA = winA._tabs.tabBar()
    winB = ToolWindow()
    winB.add_tab(AnalysisTab("VERY_LONG_ANALYSIS_NAME_" * 4))
    for i in range(15):
        winB.add_tab(AnalysisTab(f"analysis{i:02d}"))
    winB.show()
    qapp.processEvents()
    barB = winB._tabs.tabBar()
    assert barA.minimumSizeHint().width() == barB.minimumSizeHint().width()
    winB.resize(520, 800)
    qapp.processEvents()
    assert winB.width() <= 560
    assert barB._row_count > 1
