import io
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMenu,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtGui import (
    QContextMenuEvent,
    QPainter,
    QPixmap,
    QResizeEvent,
    QShowEvent,
    QWheelEvent,
)

from common.i18n import tr


_PALETTE = [
    "#1f77b4",
    "#ff7f0e",
    "#2ca02c",
    "#d62728",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#7f7f7f",
    "#bcbd22",
    "#17becf",
]


class SelectorPanel(QWidget):
    selectionChanged = Signal(str)

    def __init__(self, label: str, options: list[str], parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        self._label = QLabel(label)
        self._combo = QComboBox()
        self._combo.addItems(options)
        layout.addWidget(self._label)
        layout.addWidget(self._combo)
        layout.addStretch(1)
        self._combo.currentTextChanged.connect(self.selectionChanged.emit)

    def set_options(self, options: list[str]) -> None:
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItems(options)
        self._combo.blockSignals(False)

    def current(self) -> str:
        return self._combo.currentText()

    def set_current(self, name: str) -> None:
        self._combo.blockSignals(True)
        self._combo.setCurrentText(name)
        self._combo.blockSignals(False)


class TrajectoryPanel(QWidget):
    pointClicked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        self._plot = pg.PlotWidget()
        layout.addWidget(self._plot)
        self._scatter = pg.ScatterPlotItem()
        self._plot.addItem(self._scatter)
        self._scatter.sigClicked.connect(self._on_clicked)
        self._markers: list[pg.InfiniteLine] = []

    def _on_clicked(self, scatter_item, points, ev) -> None:
        if len(points) == 0:
            return
        idx = int(points[0].data())
        self.pointClicked.emit(idx)

    def set_data(
        self,
        x: np.ndarray,
        y: np.ndarray,
        color_by: list[str] | None = None,
        highlights: list[int] | None = None,
    ) -> None:
        if len(x) != len(y):
            raise ValueError(f"len(x) != len(y): {len(x)} vs {len(y)}")
        N = len(x)
        if color_by is not None and len(color_by) != N:
            raise ValueError(f"len(color_by) != len(x): {len(color_by)} vs {N}")
        if highlights is not None:
            for i in highlights:
                if not isinstance(i, int):
                    raise ValueError(
                        f"highlight index must be int, got {type(i).__name__}: {i}"
                    )
                if not (0 <= i < N):
                    raise ValueError(f"highlight index out of range: {i}")

        kwargs = dict(x=x, y=y, data=np.arange(N))

        if color_by is not None:
            cat_map: dict[str, str] = {}
            for cat in color_by:
                if cat not in cat_map:
                    cat_map[cat] = _PALETTE[len(cat_map) % len(_PALETTE)]
            kwargs["brush"] = [pg.mkBrush(cat_map[c]) for c in color_by]
        else:
            kwargs["brush"] = pg.mkBrush(_PALETTE[0])

        if highlights is not None:
            hl_set = set(highlights)
            default_pen = pg.mkPen(None)
            highlight_pen = pg.mkPen(color="#ffffff", width=2)
            kwargs["pen"] = [
                highlight_pen if i in hl_set else default_pen for i in range(N)
            ]
        else:
            kwargs["pen"] = pg.mkPen(None)

        self._scatter.setData(**kwargs)

    def add_marker(self, x: float, color: str = "#ff0000", label: str = "") -> None:
        line = pg.InfiniteLine(pos=x, angle=90, label=label, pen=pg.mkPen(color))
        self._plot.addItem(line)
        self._markers.append(line)

    def clear_markers(self) -> None:
        for line in self._markers:
            self._plot.removeItem(line)
        self._markers.clear()


class ImagePanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        self._view = pg.ImageView()
        layout.addWidget(self._view)
        self._notes: list[pg.TextItem] = []

    def set_image(self, image: np.ndarray | Path | str) -> None:
        if isinstance(image, np.ndarray):
            self._view.setImage(image)
        elif isinstance(image, (Path, str)):
            try:
                from PIL import Image as PILImage
            except ImportError as e:
                raise ImportError(
                    "Pillow is required to load images from a path. Run: pip install Pillow"
                ) from e
            # マウント上では PIL に path を渡すと realpath→WinError 1005。逐次読みした
            # バイト列を BytesIO で渡して realpath を回避する（common.mount_compat 参照）。
            arr = np.asarray(PILImage.open(io.BytesIO(Path(image).read_bytes())))
            self._view.setImage(arr)
        else:
            raise TypeError(f"unsupported image type: {type(image)!r}")

    def add_note(self, text: str, x: float = 0, y: float = 0) -> None:
        item = pg.TextItem(text)
        item.setPos(x, y)
        self._view.getView().addItem(item)
        self._notes.append(item)

    def clear_notes(self) -> None:
        view = self._view.getView()
        for item in self._notes:
            view.removeItem(item)
        self._notes.clear()


class FigurePanel(QGraphicsView):
    """汎用画像ビューア。任意の PNG 等を忠実フィット表示する（QGraphicsView）。

    軸付きの ImagePanel と違い、チャート PNG を転置・反転せず原寸比で表示。
    ホイール=カーソル中心に拡縮、左ドラッグ=パン。縮小はフィットで止まる
    （右クリック『50% に縮小』で 50% 表示、その状態から拡大ホイールで 100% 復帰）。
    Pillow/pyqtgraph/numpy 不要 — 依存は PySide6 のみ。
    """

    MAX_ABS_SCALE = 40.0
    ZOOM_IN = 1.25
    ZOOM_OUT = 0.8
    SHRINK_FRACTION = 0.5  # 右クリック「50% に縮小」の倍率（フィット比）

    def __init__(self, parent=None):
        super().__init__(parent)
        # 状態属性を scene/view 設定より前に初期化（resize/show 配送順に非依存）。
        self._pixmap: QPixmap | None = None
        self._user_zoomed = False
        self._half = False  # 右クリックメニューでの 50% 縮小状態か
        self._text_item: QGraphicsTextItem | None = None
        self._path: Path | None = None

        self._scene = QGraphicsScene(self)
        self._item = QGraphicsPixmapItem()
        self._scene.addItem(self._item)
        self.setScene(self._scene)

        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setRenderHints(
            QPainter.RenderHint.SmoothPixmapTransform
            | QPainter.RenderHint.Antialiasing
        )
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        # 親テーマと同じ背景色にして暗テーマで画像周囲が白く浮くのを防ぐ。
        self.setBackgroundBrush(self.palette().window())
        self.setMinimumSize(0, 0)

    def minimumSizeHint(self) -> QSize:
        # splitter が比を自由配分できるよう 0 を返す（QAbstractScrollArea 既定の
        # 非ゼロヒントを上書き）。
        return QSize(0, 0)

    def set_path(self, path: str | Path) -> None:
        """図をこのパネルへ読み込む。

        副作用: 読込成否に関わらず self._path にソースパスを記録する（情報用）。
        session 保存には使われない — session.json へは bridge が置いたペインの
        path（AnalysisTab._bridge_panes。_spec_to_tab が pane_contents() で読む）が
        書かれる。
        """
        self._path = Path(path)
        self._half = False  # 新規読込・再読込は通常（フィット）状態から
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self._pixmap = None
            self._item.setPixmap(QPixmap())
            # エラー表示を必ず中央可視にする（旧変換を引き継がない）。
            self.resetTransform()
            self._user_zoomed = False
            if self._text_item is None:
                self._text_item = QGraphicsTextItem()
                self._text_item.setDefaultTextColor(
                    self.palette().windowText().color()
                )
                self._scene.addItem(self._text_item)
            self._text_item.setVisible(True)
            self._text_item.setPlainText(f"Cannot load: {path}")
            self._text_item.setPos(0, 0)
            self.setSceneRect(self._text_item.boundingRect())
            self.centerOn(self._text_item)
            return
        if self._text_item is not None:
            self._text_item.setVisible(False)
        self._pixmap = pixmap
        self._item.setPixmap(pixmap)
        self.setSceneRect(self._item.boundingRect())
        self._user_zoomed = False
        self._fit()

    def _fit(self) -> None:
        if self._pixmap is None:
            return
        vp = self.viewport().size()
        if vp.width() < 2 or vp.height() < 2:
            return
        self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)
        self._user_zoomed = False
        self._half = False

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if not self._user_zoomed:
            self._fit()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._user_zoomed:
            self._fit()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self._pixmap is None:
            return
        dy = event.angleDelta().y()
        if dy == 0:
            return
        self._zoom_step(dy > 0)
        event.accept()

    def _zoom_step(self, zoom_in: bool) -> None:
        """ホイール 1 ノッチ分の拡縮。通常領域の下限=フィット（100% 未満へは縮まない）。
        50% 縮小状態（self._half）では拡大でフィットへ復帰・縮小で 50% を維持する。"""
        if self._pixmap is None:
            return
        # 縮小下限 fit はその場で解析計算（手動ズーム中のリサイズでも陳腐化しない）。
        br = self._item.boundingRect()
        if br.width() == 0 or br.height() == 0:
            return
        vp = self.viewport().size()
        if vp.width() < 2 or vp.height() < 2:
            return
        fit = min(vp.width() / br.width(), vp.height() / br.height())

        # m11() をスケールとして使えるのは回転/反転/せん断を一切かけない前提
        # （本パネルは scale と平行移動のみ）。将来 回転/反転を足すとこの前提は崩れる。
        current = self.transform().m11()
        if self._half:
            # 50% 縮小状態: 拡大方向で 100%(フィット) へ復帰、縮小方向は 50% 維持。
            if zoom_in:
                target = fit
                self._half = False
            else:
                target = current
        else:
            # 通常: 下限=フィット（既存仕様どおり 100% 未満へは縮まない）。
            factor = self.ZOOM_IN if zoom_in else self.ZOOM_OUT
            hi = max(fit, self.MAX_ABS_SCALE)
            target = min(max(current * factor, fit), hi)
        applied = target / current
        if abs(applied - 1) >= 1e-9:
            self.scale(applied, applied)

        # 自動フィット追従（resize/show 時の _fit）は「50% でなく、かつフィット倍率」の時のみ。
        self._user_zoomed = self._half or (self.transform().m11() > fit * (1 + 1e-6))

    def _set_half_scale(self) -> None:
        """右クリックメニュー『50% に縮小』。フィット倍率の SHRINK_FRACTION 倍へ縮小。"""
        if self._pixmap is None:
            return
        br = self._item.boundingRect()
        if br.width() == 0 or br.height() == 0:
            return
        vp = self.viewport().size()
        if vp.width() < 2 or vp.height() < 2:
            return
        fit = min(vp.width() / br.width(), vp.height() / br.height())
        current = self.transform().m11()
        applied = (fit * self.SHRINK_FRACTION) / current
        if abs(applied - 1) >= 1e-9:
            self.scale(applied, applied)
        self.centerOn(self._item)  # 小さくなった画像をパネル中央へ
        self._half = True
        self._user_zoomed = True  # リサイズで倍率保持（自動再フィットさせない）

    def full_pixmap(self) -> QPixmap | None:
        """ズーム/パンに依存しない原寸の全図。未ロード/読込失敗時は None。

        ミーティング共有/全域キャプチャ用。view の transform（ホスト側ズーム）に
        関係なくロード済みオリジナルを返すので、ゲストは全図を受け取り自分で
        ズーム/パンできる。copy_image_to_clipboard と同一ソース。
        """
        return self._pixmap

    def copy_image_to_clipboard(self) -> bool:
        """表示中の原寸オリジナル画像をシステムクリップボードへコピー。

        コピー対象はズーム/パン後の表示結果ではなくロード済みのオリジナル
        QPixmap（プレゼン等へ高品質で貼り付けられる）。未ロードなら False。
        """
        if self._pixmap is None:
            return False
        QApplication.clipboard().setPixmap(self._pixmap)
        return True

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        if self._pixmap is None:
            super().contextMenuEvent(event)
            return
        menu = QMenu(self)
        copy_action = menu.addAction(tr("figure.menu.copy_image"))
        copy_action.triggered.connect(self.copy_image_to_clipboard)
        shrink_action = menu.addAction(tr("figure.menu.shrink_half"))
        shrink_action.triggered.connect(self._set_half_scale)
        menu.exec(event.globalPos())
        event.accept()
