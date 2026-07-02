from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QMouseEvent, QPaintEvent, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyleOptionTab,
    QStylePainter,
    QTabBar,
    QWidget,
)

_DEFAULT_ROW_PAD = 10  # 0 タブ時の段高フォールバック余白（潰れ防止用の既定値）
_ELIDE_PAD = 12        # 省略表示時に矩形幅から差し引く左右マージン
_MIN_BAR_WIDTH = 80    # minimumSizeHint 幅の固定下限（タブ数・ラベル長に非依存）


class MultiRowTabBar(QTabBar):
    """横幅に収まらないタブを N 段に折り返すタブバー。

    レイアウト・描画・ヒットテストを自前実装し、QTabWidget.setTabBar() で
    既存の QTabWidget に差し込んで使う。呼び出し側は無改修。
    """

    # super().__init__() 中に sizeHint()/paintEvent() 等の仮想が早発しても
    # AttributeError にならないよう、クラス属性のデフォルトを定義しておく。
    _rects: list[QRect] = []
    _row_count = 1
    _row_height = 1
    _press_index = -1
    _dragging = False
    _hover_index = -1
    _detachable = False
    _pending_detach = False
    _press_name = ""

    tabDetachRequested = Signal(str, QPoint)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setElideMode(Qt.TextElideMode.ElideNone)  # 省略は自前で行う
        self.setExpanding(False)
        self.setUsesScrollButtons(False)  # 横スクロールボタンを出さない（多段の目的）
        self.setMouseTracking(True)  # hover には非押下時の mouseMoveEvent が要る
        self._rects = []
        self._row_count = 1
        self._row_height = self._compute_row_height()
        self._press_index = -1
        self._press_pos = QPoint()
        self._dragging = False
        self._hover_index = -1
        self._detachable = False
        self._pending_detach = False
        self._press_name = ""

    def set_detachable(self, on: bool) -> None:
        self._detachable = bool(on)

    def _outside_bar(self, pos: QPoint) -> bool:
        # 縦方向にバー高さ(row_height)を超えて外れたら「引き出し」とみなす。
        # 横外れは in-bar 並べ替えでカバーされ得るので縦端で判定する。
        return pos.y() < -self._row_height or pos.y() > self.height() + self._row_height

    # --- 段高 -------------------------------------------------------------
    def _compute_row_height(self) -> int:
        """タブが 0 個でも有効な段高を返す（潰れ防止）。"""
        if self.count() > 0:
            return max(1, super().tabSizeHint(0).height())
        return max(1, self.fontMetrics().height() + _DEFAULT_ROW_PAD)

    # --- コアレイアウト ---------------------------------------------------
    def _relayout(self, width: int) -> None:
        avail = max(1, width)
        old_rc = self._row_count
        old_rh = self._row_height
        rh = self._compute_row_height()
        self._row_height = rh
        rects: list[QRect] = []
        x = y = 0
        row = 0
        for i in range(self.count()):
            w = min(super().tabSizeHint(i).width(), avail)  # バー幅にクランプ
            if x > 0 and x + w > avail:  # 入りきらないので次段へ折り返す
                x = 0
                row += 1
                y = row * rh
            rects.append(QRect(x, y, w, rh))
            x += w
        self._rects = rects
        self._row_count = (row + 1) if self.count() > 0 else 1
        # 段数 OR 段高が変わった時だけ（FontChange 等で段高だけ変わる場合も拾う）。
        # 無条件呼び出しはスラッシング/再帰の危険なので避ける。
        if self._row_count != old_rc or rh != old_rh:
            self.updateGeometry()
        self.update()

    # --- サイズヒント（幅方針は sizeHint と minimumSizeHint で非対称）-----
    def sizeHint(self) -> QSize:
        # 幅: 全タブを単段に並べた自然幅を要求する。QTabWidget は非 expanding の
        # バー幅を sizeHint().width() を上限に決めるため、ここを compact にすると
        # バーが全幅へストレッチせず常時多段化する。超過分は _relayout が折り返す。
        return QSize(
            super().sizeHint().width(),
            max(1, self._row_count) * self._row_height,
        )

    def minimumSizeHint(self) -> QSize:
        # 幅: タブ数にもラベル長にも依存しない固定下限。super().minimumSizeHint()
        # .width() は usesScrollButtons(False) 下で「全タブ単段合計幅」へ膨らむし、
        # 先頭タブの自然幅もラベルが長いと膨らむ。どちらも中央ウィジェット →
        # QMainWindow のウィンドウ最小幅へ伝播し「狭めて折り返す」「幅 < 1 タブの
        # クランプ／省略」を実 GUI で不能にする。よって固定下限を返す。
        return QSize(_MIN_BAR_WIDTH, self._row_height)

    # --- 矩形・ヒットテスト ----------------------------------------------
    def tabRect(self, index: int) -> QRect:
        if 0 <= index < len(self._rects):
            return self._rects[index]
        return QRect()

    def tabAt(self, point: QPoint) -> int:
        for i, r in enumerate(self._rects):
            if r.contains(point):
                return i
        return -1

    # --- 描画 -------------------------------------------------------------
    def paintEvent(self, event: QPaintEvent) -> None:
        if not self._rects:
            return  # 0 タブ
        painter = QStylePainter(self)
        cur = self.currentIndex()
        # 非選択を先に、選択タブを最後に描く（選択枠のクリップを防ぐ）
        order = [i for i in range(self.count()) if i != cur]
        if 0 <= cur < self.count():
            order.append(cur)
        for i in order:
            opt = QStyleOptionTab()
            self.initStyleOption(opt, i)
            opt.rect = self._rects[i]
            if i == self._hover_index and i != cur:
                opt.state |= QStyle.StateFlag.State_MouseOver
            else:
                opt.state &= ~QStyle.StateFlag.State_MouseOver
            opt.text = self.fontMetrics().elidedText(
                self.tabText(i), Qt.TextElideMode.ElideRight,
                max(0, self._rects[i].width() - _ELIDE_PAD),
            )
            painter.drawControl(QStyle.ControlElement.CE_TabBarTab, opt)

    # --- 再レイアウトの起点（いずれも super() を先に呼ぶ）---------------
    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._relayout(event.size().width())  # 主たる起点

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._relayout(self.width())  # 非表示中に追加された退化レイアウトの自己修復

    def tabInserted(self, index: int) -> None:
        super().tabInserted(index)
        self._relayout(self.width())

    def tabRemoved(self, index: int) -> None:
        super().tabRemoved(index)
        self._relayout(self.width())

    def tabLayoutChange(self) -> None:
        super().tabLayoutChange()
        self._relayout(self.width())

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._relayout(self.width())

    # --- マウス（左ボタンのみ自前。右・中は super() に委譲）-------------
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            idx = self.tabAt(event.position().toPoint())
            if idx >= 0:
                self.setCurrentIndex(idx)
            # super() をバイパスするため focus が来ない → 明示要求（矢印キー操作維持）
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._press_index = idx
            self._press_pos = event.position().toPoint()
            self._dragging = False
            if idx >= 0:
                data = self.tabData(idx)
                self._press_name = data if isinstance(data, str) else self.tabText(idx)
            else:
                self._press_name = ""
            self._pending_detach = False
            return  # super() は呼ばない（ネイティブ 1 段ロジックと衝突するため）
        # 右クリック → CustomContextMenu の生成を壊さないよう super() に委譲
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if (
            self.isMovable()
            and self._press_index >= 0
            and (event.buttons() & Qt.MouseButton.LeftButton)
        ):
            pos = event.position().toPoint()
            if (pos - self._press_pos).manhattanLength() >= QApplication.startDragDistance():
                target = self.tabAt(pos)
                if target >= 0 and target != self._press_index:
                    self.moveTab(self._press_index, target)  # tabMoved を emit
                    self._press_index = target
                    self._dragging = True
                    self._relayout(self.width())  # 直後に矩形を更新（古い矩形を見ない）
            if self._detachable and self._outside_bar(pos):
                self._pending_detach = True
            return
        # hover 更新（装飾）
        new_hover = self.tabAt(event.position().toPoint())
        if new_hover != self._hover_index:
            self._hover_index = new_hover
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            armed = self._pending_detach
            name = self._press_name
            self._press_index = -1
            self._dragging = False
            self._pending_detach = False
            self._press_name = ""
            if armed and name and self._outside_bar(pos):
                self.tabDetachRequested.emit(name, event.globalPosition().toPoint())
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        if self._hover_index != -1:
            self._hover_index = -1
            self.update()
        super().leaveEvent(event)
