import logging
import math
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QSplitter, QVBoxLayout, QWidget

from common.slots import format_slot, parse_slot

_log = logging.getLogger(__name__)

RESERVED_TAB_VERBS = frozenset(["set-split", "close-pane", "list-panes"])

_ORIENT = {"h": Qt.Orientation.Horizontal, "v": Qt.Orientation.Vertical}


def _new_leaf(owner: QWidget) -> QWidget:
    """分割木の葉（パネルを縦積みする入れ物）。

    owner を親に生成する: PySide の QSplitter.replaceWidget は所有権を移さない
    ので、親無しで作ると Python 側の参照が切れた時点で C++ ごと破棄される。
    """
    leaf = QWidget(owner)
    QVBoxLayout(leaf)
    return leaf


def _is_node(w) -> bool:
    return isinstance(w, QSplitter)


def _axis_of(splitter: QSplitter) -> str:
    return "h" if splitter.orientation() == Qt.Orientation.Horizontal else "v"


def _leaf_widgets(leaf: QWidget) -> list[QWidget]:
    """葉の中身＝layout に入っている widget 全て（spacer 等は数えない）。"""
    lay = leaf.layout()
    if lay is None:
        return []
    out = []
    for i in range(lay.count()):
        item = lay.itemAt(i)
        w = item.widget() if item is not None else None
        if w is not None:
            out.append(w)
    return out


def _leaves(node: QWidget) -> list[QWidget]:
    """部分木の葉を木順（深さ優先・第 1 子→第 2 子）で。node 自身が葉ならそれ。"""
    if not _is_node(node):
        return [node]
    out: list[QWidget] = []
    for i in range(node.count()):
        out.extend(_leaves(node.widget(i)))
    return out


def _nodes(node: QWidget) -> list[QSplitter]:
    """部分木の QSplitter を前順で（node 自身を含む）。"""
    if not _is_node(node):
        return []
    out = [node]
    for i in range(node.count()):
        out.extend(_nodes(node.widget(i)))
    return out


class _FullGrabFallback(Exception):
    """grab_full: 全域不可のコンテンツがあった → self.grab() へ。"""


class AnalysisTab(QWidget):
    """1 解析あたりのタブ内容。汎用器なので、パネル構成は外から組み立てる。

    splitter 部は分割木（Issue #97）: root ``_splitter`` は常に 2 子で、子は葉
    （QWidget+QVBoxLayout）か入れ子 QSplitter（常に 2 子）。bridge が置いた
    パネルだけが ``_bridge_panes`` に記録され、移動・破棄・永続化の対象になる。
    """

    def __init__(self, name: str, parent=None, *, is_placeholder: bool = False):
        super().__init__(parent)
        self.name = name
        self.is_placeholder = is_placeholder
        self._panels: dict[str, QWidget] = {}
        self._bridge_panes: dict[str, dict] = {}
        self.viewer_host = False
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

        self._splitter.addWidget(_new_leaf(self))
        self._splitter.addWidget(_new_leaf(self))

    # ---- root children (read-only views) ----

    @property
    def _left_container(self) -> QWidget:
        return self._splitter.widget(0)

    @property
    def _right_container(self) -> QWidget:
        return self._splitter.widget(1)

    def root_pane(self, idx: int) -> QWidget:
        if idx not in (0, 1):
            raise ValueError(f"root pane index must be 0 or 1, got {idx!r}")
        return self._splitter.widget(idx)

    def _root_index(self, position: str) -> int:
        if position == "left":
            return 0
        if position == "right":
            return 1
        raise ValueError(f"unknown position: {position!r}")

    # ---- bridge ownership helpers ----

    def _bridge(self) -> dict[str, dict]:
        return getattr(self, "_bridge_panes", {})

    def _bridge_key_of(self, w: QWidget) -> str | None:
        for k in self._bridge():
            if self._panels.get(k) is w:
                return k
        return None

    def _key_of(self, w: QWidget) -> str | None:
        for k, v in self._panels.items():
            if v is w:
                return k
        return None

    def _has_foreign(self, node: QWidget) -> bool:
        """部分木に非 bridge 中身（解析パネル・未登録 widget）があるか。"""
        for leaf in _leaves(node):
            for w in _leaf_widgets(leaf):
                if self._bridge_key_of(w) is None:
                    return True
        return False

    def _check_foreign(self, node: QWidget, what: str) -> None:
        if self._has_foreign(node):
            raise LookupError(
                f"{what} holds panels not placed by the bridge "
                f"(analysis panels are never moved or destroyed)"
            )

    def _remove_bridge_in(self, node: QWidget) -> None:
        for leaf in _leaves(node):
            for w in _leaf_widgets(leaf):
                k = self._bridge_key_of(w)
                if k is not None:
                    self.remove_panel(k)

    def _is_shown(self, w: QWidget) -> bool:
        """w から root までに明示 hide が無いか（isVisible は窓の表示に依存）。"""
        cur = w
        while cur is not None:
            if cur.isHidden():
                return False
            if cur is self._splitter:
                return True
            cur = cur.parentWidget()
        return False

    def _is_tree_node(self, s) -> bool:
        cur = s
        while cur is not self._splitter:
            if not _is_node(cur):
                return False
            cur = cur.parentWidget()
            if cur is None:
                return False
        return True

    def _all_slots(self) -> str:
        out: list[str] = []

        def walk(node, steps):
            out.append(format_slot(steps) or "(root)")
            if _is_node(node):
                axis = _axis_of(node)
                for i in range(node.count()):
                    walk(node.widget(i), steps + [(axis, i)])

        walk(self._splitter, [])
        return ", ".join(out)

    # ---- slot addressing ----

    def node_at(self, slot: str) -> QWidget:
        """厳密照合（向きも一致必須・可視性は問わない・変更なし）。"""
        steps = parse_slot(slot)
        cur: QWidget = self._splitter
        for axis, idx in steps:
            if not _is_node(cur):
                raise ValueError(
                    f"slot {slot!r} goes deeper than the split tree "
                    f"(current slots: {self._all_slots()})"
                )
            if _axis_of(cur) != axis:
                raise ValueError(
                    f"slot {slot!r} does not match the split orientation "
                    f"(current slots: {self._all_slots()})"
                )
            cur = cur.widget(idx)
        return cur

    def slot_of(self, widget: QWidget) -> str | None:
        """widget（葉 or 葉内パネル）の slot。splitter 外（top 行等）は None。"""
        cur = widget
        # 分割木のノードに直接ぶら下がる祖先（＝葉）まで登る。
        while True:
            p = cur.parentWidget()
            if p is None or p is self:
                return None
            if _is_node(p) and self._is_tree_node(p):
                break
            cur = p
        steps: list = []
        while cur is not self._splitter:
            p = cur.parentWidget()
            steps.append((_axis_of(p), p.indexOf(cur)))
            cur = p
        return format_slot(list(reversed(steps)))

    def ensure_pane(self, slot: str, *, dry_run: bool = False) -> QWidget | None:
        """配置用に slot の葉を用意する（向き変更・分割・畳み込み）。

        Phase 1 で所有権を検査し（非 bridge 中身があれば LookupError・無変更）、
        Phase 2 で木を変更する。表示するのは経路上だけで、tidy() は呼ばない。
        """
        steps = parse_slot(slot)
        if not steps:
            raise ValueError("slot is required")
        # Phase 1: read-only inspection.
        cur: QWidget = self._splitter
        for i, (axis, idx) in enumerate(steps):
            last = i == len(steps) - 1
            if _axis_of(cur) != axis:
                self._check_foreign(cur, f"split {self.slot_of_node(cur)!r}")
            child = cur.widget(idx)
            if _is_node(child):
                if last:
                    self._check_foreign(child, f"region {slot!r}")
                cur = child
                continue
            self._check_foreign(child, f"pane {format_slot(steps[: i + 1])!r}")
            break
        if dry_run:
            return None
        # Phase 2: mutate.
        cur = self._splitter
        for i, (axis, idx) in enumerate(steps):
            last = i == len(steps) - 1
            if _axis_of(cur) != axis:
                cur.setOrientation(_ORIENT[axis])
            child = cur.widget(idx)
            was_hidden = child.isHidden()
            if was_hidden:
                child.setVisible(True)
                if not cur.widget(1 - idx).isHidden():
                    cur.setSizes([500, 500])
            if not last:
                if not _is_node(child):
                    n_axis, n_idx = steps[i + 1]
                    nested = QSplitter(_ORIENT[n_axis], self)  # 親付き（_new_leaf 参照）
                    old = cur.replaceWidget(idx, nested)
                    new = _new_leaf(self)
                    if n_idx == 1:
                        nested.addWidget(old)
                        nested.addWidget(new)
                    else:
                        nested.addWidget(new)
                        nested.addWidget(old)
                    nested.setVisible(True)
                    old.setVisible(not was_hidden)
                    new.setVisible(False)
                    nested.setSizes([500, 500])
                    nested.splitterMoved.connect(self._splitter.splitterMoved)
                    child = nested
                cur = child
                continue
            if _is_node(child):
                self._remove_bridge_in(child)
                leaf = _new_leaf(self)
                old = cur.replaceWidget(idx, leaf)
                old.deleteLater()
                leaf.setVisible(True)
                return leaf
            return child
        return None  # unreachable (steps non-empty)

    def slot_of_node(self, node: QWidget) -> str:
        if node is self._splitter:
            return "(root)"
        return self.slot_of(node) or "(root)"

    def check_bridge_only(self, slot: str | None = None) -> None:
        if slot is None or not str(slot).strip():
            self._check_foreign(self._splitter, "this tab")
            return
        self._check_foreign(self.node_at(slot), f"region {slot!r}")

    # ---- bridge panels ----

    def add_bridge_panel(
        self, key: str, widget: QWidget, leaf: QWidget, kind: str, path: str | None
    ) -> None:
        """bridge パネルを葉へ登録（表示はしない＝呼び出し側が経路表示する）。"""
        if key in self._panels:
            raise KeyError(f"panel key {key!r} already registered")
        leaf.layout().addWidget(widget, stretch=1)
        self._panels[key] = widget
        if not hasattr(self, "_bridge_panes"):   # Tier 1 hot reload 前からの旧タブ
            self._bridge_panes = {}
        self._bridge_panes[key] = {"kind": kind, "path": path}

    def bridge_panel_in(self, leaf: QWidget) -> tuple[str, QWidget] | None:
        if _is_node(leaf):
            return None
        for w in _leaf_widgets(leaf):
            k = self._bridge_key_of(w)
            if k is not None:
                return k, w
        return None

    def bridge_panels(self, kind: str | None = None) -> list[tuple[str, str, QWidget]]:
        """可視 bridge パネルの (slot, key, widget) を木順で。"""
        out = []
        bp = self._bridge()
        for leaf in _leaves(self._splitter):
            for w in _leaf_widgets(leaf):
                k = self._bridge_key_of(w)
                if k is None or not self._is_shown(w):
                    continue
                if kind is not None and bp[k].get("kind") != kind:
                    continue
                out.append((self.slot_of(leaf), k, w))
        return out

    def panels_by_slot(self) -> list[dict]:
        """list-panes 用: splitter 内の可視登録パネル全て（木順）。"""
        out = []
        bp = self._bridge()
        for leaf in _leaves(self._splitter):
            for w in _leaf_widgets(leaf):
                k = self._key_of(w)
                if k is None or not self._is_shown(w):
                    continue
                info = bp.get(k)
                out.append({
                    "slot": self.slot_of(leaf),
                    "key": k,
                    "kind": info.get("kind") if info else None,
                    "path": info.get("path") if info else None,
                })
        return out

    def pane_contents(self) -> list[tuple[str, str, str | None]]:
        bp = self._bridge()
        return [
            (slot, bp[k].get("kind"), bp[k].get("path"))
            for slot, k, _w in self.bridge_panels()
        ]

    def close_pane(self, slot: str) -> None:
        if not parse_slot(slot):
            raise ValueError("slot is required for close-pane")
        region = self.node_at(slot)
        self._check_foreign(region, f"region {slot!r}")
        if not any(_leaf_widgets(leaf) for leaf in _leaves(region)):
            raise ValueError(f"pane is empty: {slot!r}")
        remaining = [
            w for w in self._panels.values()
            if self._splitter.isAncestorOf(w)
            and not (region is w or region.isAncestorOf(w))
            and self._is_shown(w)
        ]
        if not remaining:
            raise ValueError(
                f"closing {slot!r} would leave no visible pane; "
                f"use close-tab to close the whole tab"
            )
        self._remove_bridge_in(region)
        self.tidy()

    def clear_panes(self) -> None:
        self._check_foreign(self._splitter, "this tab")
        for k in list(self._bridge()):
            if k in self._panels:
                self.remove_panel(k)
        for idx in (0, 1):
            child = self._splitter.widget(idx)
            if _is_node(child):
                old = self._splitter.replaceWidget(idx, _new_leaf(self))
                old.deleteLater()
        self._splitter.widget(0).setVisible(False)
        self._splitter.widget(1).setVisible(False)

    def prune_panes(self, keep: set[str]) -> None:
        for k in list(self._bridge()):
            w = self._panels.get(k)
            if w is None:
                continue
            if self.slot_of(w) not in keep:
                self.remove_panel(k)
        self.tidy()

    def relocate_bridge_panel(self, key: str, slot: str) -> None:
        """既存 bridge パネルを同一性を保ったまま slot の葉へ移す（session 復元用）。"""
        if key not in self._bridge() or key not in self._panels:
            raise KeyError(f"{key!r} is not a bridge panel")
        self.ensure_pane(slot, dry_run=True)
        w = self._panels[key]
        try:
            target = self.node_at(slot)
        except ValueError:
            target = None
        if target is not None and not _is_node(target) and w in _leaf_widgets(target):
            self.ensure_pane(slot)
            return
        # 先に木から退避（以降の分割・畳み込みに巻き込まれないように）。
        old = w.parentWidget()
        if old is not None and old.layout() is not None:
            old.layout().removeWidget(w)
        w.setParent(self)
        w.setVisible(False)
        leaf = self.ensure_pane(slot)
        other = self.bridge_panel_in(leaf)
        if other is not None and other[1] is not w:
            self.remove_panel(other[0])
        leaf.layout().addWidget(w, stretch=1)
        w.setVisible(True)

    def tidy(self) -> None:
        """bridge 操作の仕上げ: 空ノードを葉へ畳み、空の葉・空ノードを隠す。"""
        def collapse(node: QSplitter) -> None:
            for idx in (0, 1):
                child = node.widget(idx)
                if not _is_node(child):
                    continue
                if not any(_leaf_widgets(leaf) for leaf in _leaves(child)):
                    leaf = _new_leaf(self)
                    old = node.replaceWidget(idx, leaf)
                    old.deleteLater()
                else:
                    collapse(child)

        collapse(self._splitter)
        for leaf in _leaves(self._splitter):
            if not _leaf_widgets(leaf):
                leaf.setVisible(False)

        def hide_empty(node: QSplitter) -> None:
            for idx in (0, 1):
                child = node.widget(idx)
                if _is_node(child):
                    hide_empty(child)
                    if child.widget(0).isHidden() and child.widget(1).isHidden():
                        child.setVisible(False)

        hide_empty(self._splitter)

    # ---- generic panel API ----

    def add_panel(
        self, key: str, widget: QWidget, position: str, stretch: int = 0
    ) -> None:
        if key in self._panels:
            raise KeyError(f"panel key {key!r} already registered")
        if position == "top":
            self._top_row.addWidget(widget)
        elif position in ("left", "right"):
            leaf = _leaves(self._splitter.widget(self._root_index(position)))[0]
            leaf.layout().addWidget(widget, stretch=stretch)
        else:
            raise ValueError(f"unknown position: {position!r}")
        self._panels[key] = widget

    def remove_panel(self, key: str) -> None:
        """Remove and destroy a panel. Caller must disconnect signals before calling."""
        widget = self._panels.pop(key)
        self._bridge().pop(key, None)
        old = widget.parentWidget()
        if old is not None and old.layout() is not None:
            old.layout().removeWidget(widget)
        widget.setParent(None)
        widget.deleteLater()

    def move_panel(self, key: str, position: str) -> None:
        """既存パネルを破棄せず左右コンテナ間へ移す（冪等）。

        add_panel は同キー再登録で KeyError、remove_panel は deleteLater で破棄する
        ため、どちらも「移設」には使えない。ここでは widget を現コンテナの layout から
        外し目的コンテナの layout へ addWidget し直す（widget と内部状態は保持）。
        目的の root 子が分割済み（入れ子 QSplitter）なら ValueError。
        """
        widget = self._panels[key]  # 不在なら KeyError（呼び出し側が存在保証）
        target_container = self._splitter.widget(self._root_index(position))
        if _is_node(target_container):
            raise ValueError(f"{position!r} pane is split; cannot move a panel there")
        target_layout = target_container.layout()
        if target_container.isAncestorOf(widget):
            return  # 既に目的コンテナ → no-op（冪等）
        if self._bridge_key_of(widget) is not None:
            other = self.bridge_panel_in(target_container)
            if other is not None and other[1] is not widget:
                raise ValueError(
                    f"{position!r} pane already holds bridge panel {other[0]!r}"
                )
        old = widget.parentWidget()
        if old is not None and old.layout() is not None:
            old.layout().removeWidget(widget)
        target_layout.addWidget(widget, stretch=1)

    def panel(self, key: str) -> QWidget:
        return self._panels[key]

    def set_pane_visible(self, position: str, visible: bool) -> None:
        self._splitter.widget(self._root_index(position)).setVisible(visible)

    def set_split_orientation(self, orient: str) -> None:
        if orient == "horizontal":
            self._splitter.setOrientation(Qt.Orientation.Horizontal)
        elif orient == "vertical":
            self._splitter.setOrientation(Qt.Orientation.Vertical)
        else:
            raise ValueError(f"unknown orientation: {orient!r}")

    def set_split_ratio(
        self, left: float, right: float, slot: str | None = None
    ) -> None:
        if not (math.isfinite(left) and math.isfinite(right)):
            raise ValueError(f"left and right must be finite: got {left}, {right}")
        if left <= 0 or right <= 0:
            raise ValueError(f"left and right must be positive: got {left}, {right}")
        if slot is None or not str(slot).strip():
            target: QWidget = self._splitter
        else:
            target = self.node_at(slot)
            if not _is_node(target):
                raise ValueError(
                    f"slot {slot!r} is a pane, not a split "
                    f"(current slots: {self._all_slots()})"
                )
        total = 1000
        l = int(total * left / (left + right))
        target.setSizes([l, total - l])

    def capture_layout(self) -> dict:
        """splitter の geometry のみを取り出す（パネル内容は含めない・汎用器を保つ）。

        isHidden()（明示 hide フラグ）を使うのでトップレベルウィンドウの表示状態に
        非依存（offscreen テスト・最小化でも安定）。可視の入れ子ノードがある時だけ
        ``splits``（{node slot: sizes}）を足す（平坦なら従来とバイト同一）。
        """
        horiz = self._splitter.orientation() == Qt.Orientation.Horizontal
        out = {
            "orientation": "horizontal" if horiz else "vertical",
            "sizes": list(self._splitter.sizes()),
            "left_hidden": self._splitter.widget(0).isHidden(),
            "right_hidden": self._splitter.widget(1).isHidden(),
        }
        splits = {}
        for node in _nodes(self._splitter)[1:]:
            if self._is_shown(node):
                splits[self.slot_of(node)] = list(node.sizes())
        if splits:
            out["splits"] = splits
        return out

    def apply_layout(self, layout: dict) -> None:
        """capture_layout の逆。best-effort（失敗しても復元ループを止めない）。

        適用順は 向き → ペイン表示/非表示 → sizes → 入れ子 splits。可視性を先に
        確定してから sizes を配分する。sizes は要素数が splitter の子数と一致し合計
        > 0 の時のみ setSizes する（未表示のまま保存された背景タブの [0, 0] で split
        を潰さない／手編集で要素数が壊れた session.json で意図しない 0 配分を避ける
        ためのガード）。splits はノードごとに個別 try。
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
        splits = layout.get("splits") if isinstance(layout, dict) else None
        if isinstance(splits, dict):
            for slot, sz in splits.items():
                try:
                    node = self.node_at(slot)
                    if not _is_node(node):
                        continue
                    if len(sz) == 2 and sum(sz) > 0:
                        node.setSizes([int(s) for s in sz])
                except Exception:
                    _log.warning(
                        "apply_layout: split %r failed: %r", slot, sz, exc_info=True
                    )

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
        合成は分割木を再帰的に辿る（ノード＝その向きで連結、葉＝葉内の可視パネルを
        縦積み、隠れた子は skip）。1つでも全域を出せないパネルがあれば従来の
        ``grab()``（見たまま合成）にフォールバックする（pyqtgraph 主体タブ・混在
        タブ・エラー状態を安全に処理し、可視パネルを取りこぼさない）。
        """
        # Use isHidden() (the explicit hide flag) not isVisible(), so the result
        # is independent of whether the top-level window is shown (offscreen
        # tests, minimised window) and only reflects pane collapse.
        registered = list(self._panels.values())

        def full_of(w: QWidget) -> QPixmap:
            fn = getattr(w, "full_pixmap", None)
            pm = None
            if callable(fn):
                try:
                    pm = fn()
                except Exception:
                    pm = None
            if pm is None or pm.isNull():
                # 可視コンテンツに全域不可が1つでも → 落とさず見たまま grab()。
                raise _FullGrabFallback
            return pm

        def collect(w: QWidget) -> QPixmap | None:
            if _is_node(w):
                pms = []
                for i in range(w.count()):
                    c = w.widget(i)
                    if c.isHidden():
                        continue
                    pm = collect(c)
                    if pm is not None:
                        pms.append(pm)
                if not pms:
                    return None
                if len(pms) == 1:
                    return pms[0]
                return self._compose_full(
                    pms, horiz=w.orientation() == Qt.Orientation.Horizontal
                )
            pms = [
                full_of(c) for c in _leaf_widgets(w)
                if not c.isHidden() and any(c is r for r in registered)
            ]
            if not pms:
                return None
            if len(pms) == 1:
                return pms[0]
            return self._compose_full(pms, horiz=False)

        try:
            out = collect(self._splitter)
        except _FullGrabFallback:
            return self.grab()
        if out is None:
            return self.grab()
        return out

    def _compose_full(self, pms: list[QPixmap], horiz: bool | None = None) -> QPixmap:
        """複数の全域図を連結（cross 軸を最大寸法に揃える）。

        horiz=None は root splitter の向き（従来互換）。
        """
        gap = 8
        bg = self.palette().window().color()
        smooth = Qt.TransformationMode.SmoothTransformation
        if horiz is None:
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
