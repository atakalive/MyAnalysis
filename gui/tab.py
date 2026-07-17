import logging
import math
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QSplitter, QVBoxLayout, QWidget

_log = logging.getLogger(__name__)

RESERVED_TAB_VERBS = frozenset(["set-split"])


class AnalysisTab(QWidget):
    """1 解析あたりのタブ内容。汎用器なので、パネル構成は外から組み立てる。"""

    def __init__(self, name: str, parent=None, *, is_placeholder: bool = False):
        super().__init__(parent)
        self.name = name
        self.is_placeholder = is_placeholder
        self._panels: dict[str, QWidget] = {}
        self._state_provider: Callable[[], dict] | None = None
        self._snapshot_writer: Callable[["AnalysisTab"], None] | None = None
        self._annotations_handler: Callable[[dict], None] | None = None
        self._command_handlers: dict[str, Callable[..., object]] = {}
        self.session_spec: dict | None = None

        outer = QVBoxLayout(self)
        self._top_row = QHBoxLayout()
        outer.addLayout(self._top_row)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(self._splitter, stretch=1)

        self._left_container = QWidget()
        self._left_layout = QVBoxLayout(self._left_container)
        self._right_container = QWidget()
        self._right_layout = QVBoxLayout(self._right_container)
        self._splitter.addWidget(self._left_container)
        self._splitter.addWidget(self._right_container)

    def add_panel(
        self, key: str, widget: QWidget, position: str, stretch: int = 0
    ) -> None:
        if key in self._panels:
            raise KeyError(f"panel key {key!r} already registered")
        if position == "top":
            self._top_row.addWidget(widget)
        elif position == "left":
            self._left_layout.addWidget(widget, stretch=stretch)
        elif position == "right":
            self._right_layout.addWidget(widget, stretch=stretch)
        else:
            raise ValueError(f"unknown position: {position!r}")
        self._panels[key] = widget

    def remove_panel(self, key: str) -> None:
        """Remove and destroy a panel. Caller must disconnect signals before calling."""
        widget = self._panels.pop(key)
        widget.setParent(None)
        widget.deleteLater()

    def move_panel(self, key: str, position: str) -> None:
        """既存パネルを破棄せず左右コンテナ間へ移す（冪等）。

        add_panel は同キー再登録で KeyError、remove_panel は deleteLater で破棄する
        ため、どちらも「移設」には使えない。ここでは widget を現コンテナの layout から
        外し目的コンテナの layout へ addWidget し直す（widget と内部状態は保持）。
        image viewer の配置を session 復元で記録側へ収束させるために使う。
        """
        widget = self._panels[key]  # 不在なら KeyError（呼び出し側が存在保証）
        if position == "left":
            target_layout, target_container = self._left_layout, self._left_container
        elif position == "right":
            target_layout, target_container = self._right_layout, self._right_container
        else:
            raise ValueError(f"unknown position: {position!r}")
        if target_container.isAncestorOf(widget):
            return  # 既に目的コンテナ → no-op（冪等）
        old = widget.parentWidget()
        if old is not None and old.layout() is not None:
            old.layout().removeWidget(widget)
        target_layout.addWidget(widget, stretch=1)

    def panel(self, key: str) -> QWidget:
        return self._panels[key]

    def set_pane_visible(self, position: str, visible: bool) -> None:
        if position == "left":
            container = self._left_container
        elif position == "right":
            container = self._right_container
        else:
            raise ValueError(f"unknown position: {position!r}")
        container.setVisible(visible)

    def set_split_orientation(self, orient: str) -> None:
        if orient == "horizontal":
            self._splitter.setOrientation(Qt.Orientation.Horizontal)
        elif orient == "vertical":
            self._splitter.setOrientation(Qt.Orientation.Vertical)
        else:
            raise ValueError(f"unknown orientation: {orient!r}")

    def set_split_ratio(self, left: float, right: float) -> None:
        if not (math.isfinite(left) and math.isfinite(right)):
            raise ValueError(f"left and right must be finite: got {left}, {right}")
        if left <= 0 or right <= 0:
            raise ValueError(f"left and right must be positive: got {left}, {right}")
        total = 1000
        l = int(total * left / (left + right))
        self._splitter.setSizes([l, total - l])

    def capture_layout(self) -> dict:
        """splitter の geometry のみを取り出す（figure2 の内容は含めない・汎用器を保つ）。

        isHidden()（明示 hide フラグ）を使うのでトップレベルウィンドウの表示状態に
        非依存（offscreen テスト・最小化でも安定）。既存 _compose_full の可視判定と同じ流儀。
        """
        horiz = self._splitter.orientation() == Qt.Orientation.Horizontal
        return {
            "orientation": "horizontal" if horiz else "vertical",
            "sizes": list(self._splitter.sizes()),
            "left_hidden": self._left_container.isHidden(),
            "right_hidden": self._right_container.isHidden(),
        }

    def apply_layout(self, layout: dict) -> None:
        """capture_layout の逆。best-effort（失敗しても復元ループを止めない）。

        適用順は 向き → ペイン表示/非表示 → sizes。可視性を先に確定してから
        sizes を配分する。sizes は要素数が splitter の子数と一致し合計 > 0 の時のみ
        setSizes する（未表示のまま保存された背景タブの [0, 0] で split を潰さない／
        手編集で要素数が壊れた session.json で意図しない 0 配分を避けるためのガード）。
        """
        try:
            orient = layout.get("orientation")
            if orient in ("horizontal", "vertical"):
                self.set_split_orientation(orient)
            self.set_pane_visible("left", not layout.get("left_hidden", False))
            self.set_pane_visible("right", not layout.get("right_hidden", False))
            sizes = layout.get("sizes")
            if (
                sizes
                and len(sizes) == self._splitter.count()
                and sum(sizes) > 0
            ):
                self._splitter.setSizes([int(s) for s in sizes])
        except Exception:
            _log.warning("apply_layout failed: %r", layout, exc_info=True)

    def connect_state(self, provider: Callable[[], dict] | None) -> None:
        self._state_provider = provider

    def current_state(self) -> dict | None:
        return self._state_provider() if self._state_provider else None

    def connect_snapshot_writer(
        self, writer: Callable[["AnalysisTab"], None] | None
    ) -> None:
        self._snapshot_writer = writer

    def register_command(
        self, verb: str, handler: Callable[..., object]
    ) -> None:
        self._command_handlers[verb] = handler

    def dispatch_command(self, verb: str, **kwargs) -> object:
        return self._command_handlers[verb](**kwargs)

    def has_command(self, verb: str) -> bool:
        return verb in self._command_handlers

    def take_snapshot(self) -> None:
        if self._snapshot_writer:
            self._snapshot_writer(self)

    def connect_annotations(self, handler: Callable[[dict], None] | None) -> None:
        self._annotations_handler = handler

    def apply_annotations(self, ann: dict) -> None:
        if self._annotations_handler is not None:
            try:
                self._annotations_handler(ann)
            except Exception:
                _log.warning("apply_annotations failed", exc_info=True)

    # ---- full-extent capture (meeting share) ----

    def grab_full(self) -> QPixmap:
        """共有用の全域レンダリング（viewport 切り取りでなくコンテンツ全域）。

        splitter 内の可視コンテンツパネルが **全て** ``full_pixmap()`` で原寸の
        全域を出せるなら、その単一図 or 合成図を返す（ホストのズーム/パン/スク
        ロールから分離されるので、ゲストは全図を受け取り自分でズーム/パンできる）。
        1つでも全域を出せないパネルがあれば従来の ``grab()``（見たまま合成）に
        フォールバックする（pyqtgraph 主体タブ・混在タブ・エラー状態を安全に処理し、
        可視パネルを取りこぼさない）。
        """
        # Visible splitter content = panels inside a non-collapsed left/right
        # container. Use isHidden() (the explicit hide flag) not isVisible(), so
        # the result is independent of whether the top-level window is shown
        # (offscreen tests, minimised window) and only reflects pane collapse.
        content = []
        for w in self._panels.values():
            if w.isHidden():
                continue
            if self._left_container.isAncestorOf(w):
                if not self._left_container.isHidden():
                    content.append(w)
            elif self._right_container.isAncestorOf(w):
                if not self._right_container.isHidden():
                    content.append(w)
        if not content:
            return self.grab()
        fulls: list[QPixmap] = []
        for w in content:
            fn = getattr(w, "full_pixmap", None)
            pm = None
            if callable(fn):
                try:
                    pm = fn()
                except Exception:
                    pm = None
            if pm is None or pm.isNull():
                # 可視コンテンツに全域不可が1つでも → 落とさず見たまま grab()。
                return self.grab()
            fulls.append(pm)
        if len(fulls) == 1:
            return fulls[0]
        return self._compose_full(fulls)

    def _compose_full(self, pms: list[QPixmap]) -> QPixmap:
        """複数の全域図を splitter の向きで連結（cross 軸を最大寸法に揃える）。"""
        gap = 8
        bg = self.palette().window().color()
        smooth = Qt.TransformationMode.SmoothTransformation
        horiz = self._splitter.orientation() == Qt.Orientation.Horizontal
        if horiz:
            h = max(p.height() for p in pms)
            ps = [p if p.height() == h else p.scaledToHeight(h, smooth) for p in pms]
            out = QPixmap(sum(p.width() for p in ps) + gap * (len(ps) - 1), h)
        else:
            w = max(p.width() for p in pms)
            ps = [p if p.width() == w else p.scaledToWidth(w, smooth) for p in pms]
            out = QPixmap(w, sum(p.height() for p in ps) + gap * (len(ps) - 1))
        out.fill(bg)
        painter = QPainter(out)
        x = y = 0
        for p in ps:
            painter.drawPixmap(x, y, p)
            if horiz:
                x += p.width() + gap
            else:
                y += p.height() + gap
        painter.end()
        return out
