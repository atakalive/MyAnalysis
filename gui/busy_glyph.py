"""作業中の印（応答待ちのアニメ）。pjdeck の gui/busy_glyph.py から移したもの。進行中のターンの文書の
最後の空の行（印の行）の左端で動かす（置き場所は ``ChatWidget`` が決める）。

形は 8 つ（``PATTERNS``）。ターンごとに ``pick_pattern`` でランダムに選び、同じチャットの直前のターンと同じものは
選ばない（替わったことが分かるように）。どれも自前で描く（絵文字・記号のフォントに頼らないので、コマごとに
幅が変わらない）。形は時刻から決めるので、時計のコマを落としても速さは変わらない。

orbit・helix は 3 つの点を 3 次元で動かして画面に写し、手前ほど大きく濃く、奥から順に描く（遠近。
``_balls``）。

動きはどれも連続的にする。位置・大きさ・濃さを時刻のなめらかな周期関数（cos / sin）で決め、跳ばない
（急に現れる・消える・濃さが切り替わることがない）、止まってから急に動き出さない、端で跳ね返らない。
scan の点だけは、濃さが 0 の間に左端へ跳ぶ（ブラウン管の横の帰線。見えないので跳んで見えない）。

動きが連続的かは ``test_pattern_moves_continuously`` が隣のコマとの違いとその変わり方で見る。線の太さも含めて
渡された矩形の真ん中の正方形に収める。色は pjdeck の実行中の丸と同じ橙。見えている間だけ 33ms の時計で描き直す
（``showEvent`` / ``hideEvent``。隠れている間は止まる）。
"""
from __future__ import annotations

import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

GLYPH_COLOR = "#e0a030"   # pjdeck の実行中の丸と同じ橙
TICK_MS = 33              # 描き直しの間隔（見えている間だけ）


def _ease(phase: float) -> float:
    """0 → 1 → 0 のなめらかな波（phase は周期を 1 とした位置。0 で 0、0.5 で 1）。"""
    return 0.5 - 0.5 * math.cos(2 * math.pi * phase)


def _spark(p: QPainter, c: QPointF, r: float, phase: float, color: QColor) -> None:
    """✻ が「· → ✢ → ✳ → ✶ → ✻」と開いて閉じる。縦横の腕と、1/4 周期遅れてまたたく斜めの腕。"""
    width = r * 0.2
    reach = r - width / 2                 # 丸い端も r の中に収める
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    main = reach * (0.25 + 0.75 * _ease(phase))
    diag = reach * 0.7 * (0.25 + 0.75 * _ease(phase - 0.25)) / math.sqrt(2)
    p.drawLine(QPointF(c.x(), c.y() - main), QPointF(c.x(), c.y() + main))
    p.drawLine(QPointF(c.x() - main, c.y()), QPointF(c.x() + main, c.y()))
    p.drawLine(QPointF(c.x() - diag, c.y() - diag), QPointF(c.x() + diag, c.y() + diag))
    p.drawLine(QPointF(c.x() - diag, c.y() + diag), QPointF(c.x() + diag, c.y() - diag))


def _ripple(p: QPainter, c: QPointF, r: float, phase: float, color: QColor) -> None:
    """中心の点から輪が広がる（波紋）。輪は内側で薄く現れて濃くなり、外側で薄れて消える（どちらの端でも
    濃さは 0 なので、輪が急に現れない）。輪は 2 つで半周期ずらす（2 つの濃さの和はいつも 1）。"""
    width = r * 0.15
    inner, outer = r * 0.25, r - width / 2
    base = p.opacity()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawEllipse(c, r * 0.18, r * 0.18)
    p.setBrush(Qt.BrushStyle.NoBrush)
    for shift in (0.0, 0.5):
        k = (phase + shift) % 1.0
        radius = inner + (outer - inner) * k
        p.setOpacity(base * _ease(k))
        p.setPen(QPen(color, width))
        p.drawEllipse(c, radius, radius)


def _dots(p: QPainter, c: QPointF, r: float, phase: float, color: QColor) -> None:
    """横に並んだ 3 つの点が、左から順に 1/3 周期ずつ遅れてなめらかに上下する（左から右へ流れる波）。
    どの点も止まらず、上が濃く下が薄い。"""
    radius = r * 0.22
    base = p.opacity()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    for i in (-1, 0, 1):
        wave = math.sin(2 * math.pi * (phase - (i + 1) / 3))
        p.setOpacity(base * (0.725 + 0.275 * wave))
        p.drawEllipse(QPointF(c.x() + i * r * 0.62, c.y() - wave * r * 0.35), radius, radius)


def _bars(p: QPainter, c: QPointF, r: float, phase: float, color: QColor) -> None:
    """縦の 3 本の棒が、1/3 周期ずつずれて伸び縮みする（イコライザ）。下端はそろえる。"""
    width = r * 0.36
    bottom = c.y() + r * 0.8
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    for i in (-1, 0, 1):
        height = r * (0.3 + 1.3 * _ease(phase - (i + 1) / 3))
        x = c.x() + i * r * 0.62 - width / 2
        p.drawRoundedRect(QRectF(x, bottom - height, width, height), width * 0.3, width * 0.3)


def _moon(p: QPainter, c: QPointF, r: float, phase: float, color: QColor) -> None:
    """月の満ち欠け。明るい側は右上 / 左下（斜め）で、新月 → 右上から満ちて上弦 → 満月 → 右上から欠けて下弦 → 新月。
    暗い側も薄く塗る（新月でも消えない）。明暗の境は、遠くの光が照らす球を正面から見た半楕円（短い方の半径は
    R·|cos θ|）で、なめらかに動く（明るい側が入れ替わる新月・満月では、どちらの側でも同じ姿なので跳ばない）。"""
    radius = r * 0.9
    base = p.opacity()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.setOpacity(base * 0.25)
    p.drawEllipse(c, radius, radius)
    p.translate(c)
    p.rotate(-45)                              # 下の「右」を画面の右上に向ける（円なので回しても矩形に収まる）
    s = math.cos(2 * math.pi * phase)
    side = 1 if phase % 1.0 < 0.5 else -1     # 1 = 右上が明るい（満ちていく）、-1 = 左下が明るい（欠けていく）
    half = radius * abs(s)
    path = QPainterPath(QPointF(0, -radius))
    # 上 → 明るい側の縁 → 下 → 明暗の境 → 上（境は、三日月なら明るい側、十三夜なら暗い側にふくらむ）
    path.arcTo(QRectF(-radius, -radius, 2 * radius, 2 * radius), 90, -180 * side)
    path.arcTo(QRectF(-half, -radius, 2 * half, 2 * radius), 270, 180 if side * s >= 0 else -180)
    path.closeSubpath()
    p.setOpacity(base)
    p.drawPath(path)


def _scan(p: QPainter, c: QPointF, r: float, phase: float, color: QColor) -> None:
    """ブラウン管のラスタースキャン。1 つの点が左から右へ一定の速さで横切っては左端へ戻り（横はノコギリ波）、
    その間に上から下へ一定の速さで下りる（6 本）。一番下まで来たら、横には振らずに、6 本目の終わり（右下）から
    1 本目の始め（左上）へまっすぐ戻る（1 本の 2 倍の時間。端で止まり、途中で速い）。横の戻りは見せない: 1 本ごとに
    始めの 15% で濃さを 0 から上げ、終わりの 15% で 0 へ下げ（smoothstep）、濃さが 0 の所で左端へ移るので、点は
    跳んで見えない。縦の戻りも始めと終わりの 15% で濃さを上げ下げする（向きが変わる所を見せない）。"""
    lines, back, fade = 6, 2, 0.15
    size = r * 0.2
    reach = r - size
    s = ((phase % 1.0) * (lines + back) + 0.25) % (lines + back)   # 1 本を 1 とした時刻。0 は 1 本目の 1/4 の所
    if s < lines:                                   # 走査
        u = s % 1.0                                 # 1 本の中の位置（0 = 左端、1 = 右端）
        x, y = -reach + 2 * reach * u, -reach + 2 * reach * s / lines
    else:                                           # 縦の戻り（右下 → 左上）
        u = (s - lines) / back
        along = (1 - math.cos(math.pi * u)) / 2
        x = y = reach - 2 * reach * along
    a = min(min(u, 1 - u) / fade, 1.0)
    if a <= 0:
        return
    p.setOpacity(p.opacity() * a * a * (3 - 2 * a))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawEllipse(QPointF(c.x() + x, c.y() + y), size, size)


def _balls(p: QPainter, balls: list[tuple[float, float, float]], size: float, color: QColor) -> None:
    """3 次元の点 (x, y, z)（x・y は画面の位置、z は -1 = 奥 〜 1 = 手前）を、奥から順に描く。手前ほど大きく
    濃い（半径は size の 0.6〜1 倍）。色が 1 つなので、重なった 2 つの点の描く順が入れ替わっても（入れ替わるのは
    z が等しく、大きさも濃さも同じとき）見た目は跳ばない（source-over の重ね合わせは同じ色なら順に依らない）。"""
    base = p.opacity()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    for x, y, z in sorted(balls, key=lambda b: b[2]):
        near = (z + 1) / 2
        radius = size * (0.6 + 0.4 * near)
        p.setOpacity(base * (0.4 + 0.6 * near))
        p.drawEllipse(QPointF(x, y), radius, radius)


def _orbit(p: QPainter, c: QPointF, r: float, phase: float, color: QColor) -> None:
    """3 つの点が、少し上から見下ろした水平の輪の上を 120° ずつ離れて回る（回転木馬）。輪の手前（画面の下側）を
    通るときは大きく濃く、奥（上側）では小さく薄い。"""
    size = r * 0.24
    a = r - size             # 横の半径（手前の点が一番大きくても r に収める）
    b = a * 0.45             # 縦の半径（見下ろす角度）
    balls = []
    for i in range(3):
        angle = 2 * math.pi * (phase + i / 3)
        balls.append((c.x() + a * math.cos(angle), c.y() + b * math.sin(angle), math.sin(angle)))
    _balls(p, balls, size, color)


def _helix(p: QPainter, c: QPointF, r: float, phase: float, color: QColor) -> None:
    """縦に並んだ 3 つの点が、縦の軸のまわりを 1/6 周ずつずれて回る（ねじれた鎖を横から見た形）。"""
    size = r * 0.24
    a = r - size
    balls = []
    for i in (-1, 0, 1):
        angle = 2 * math.pi * (phase + (i + 1) / 6)
        balls.append((c.x() + a * math.cos(angle), c.y() + i * a * 0.7, math.sin(angle)))
    _balls(p, balls, size, color)


@dataclass(frozen=True)
class Pattern:
    key: str
    period: float   # 1 周の秒数
    paint: Callable[[QPainter, QPointF, float, float, QColor], None]   # (painter, 中心, 半径, 位相, 色)


PATTERNS: tuple[Pattern, ...] = (
    Pattern("spark", 1.6, _spark),
    Pattern("ripple", 1.6, _ripple),
    Pattern("dots", 1.2, _dots),
    Pattern("bars", 1.0, _bars),
    Pattern("moon", 2.4, _moon),
    Pattern("scan", 1.6, _scan),
    Pattern("orbit", 2.0, _orbit),
    Pattern("helix", 2.0, _helix),
)
PATTERN_KEYS: tuple[str, ...] = tuple(pt.key for pt in PATTERNS)
_BY_KEY = {pt.key: pt for pt in PATTERNS}


def pick_pattern(rng: random.Random, previous: str | None) -> str:
    """previous 以外の形から等しい確率で 1 つ選ぶ（previous が None・知らないキーなら全部から）。"""
    return rng.choice([k for k in PATTERN_KEYS if k != previous])


def paint_pattern(painter: QPainter, rect: QRectF, key: str, t: float, color: QColor) -> None:
    """形 key（知らないキーは先頭の spark）の t 秒の姿を、rect の真ん中の正方形に収めて描く。"""
    pattern = _BY_KEY.get(key, PATTERNS[0])
    r = min(rect.width(), rect.height()) / 2
    if r <= 0:
        return
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pattern.paint(painter, rect.center(), r, t / pattern.period, QColor(color))
    finally:
        painter.restore()


class BusyGlyph(QWidget):
    """作業中の印。地は塗らない（履歴を選択したとき、選択の色の中で四角く抜けない）。見えている間だけ TICK_MS ごとに描き直す
    （親が隠れると子にも Hide が来るので、隠れている間は動かない）。"""

    def __init__(self, parent: QWidget | None = None, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(parent)
        self._clock = clock
        self._key = PATTERN_KEYS[0]
        self._t0 = clock()
        self._color = QColor(GLYPH_COLOR)
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self.update)

    def pattern(self) -> str:
        return self._key

    def set_pattern(self, key: str) -> None:
        """形を替え、動きを最初から数え直す。知らないキーは spark。"""
        self._key = key if key in _BY_KEY else PATTERN_KEYS[0]
        self._t0 = self._clock()
        self.update()

    def elapsed(self) -> float:
        """今の形を始めてからの秒数。"""
        return self._clock() - self._t0

    def is_animating(self) -> bool:
        return self._timer.isActive()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self._timer.start()

    def hideEvent(self, e) -> None:
        super().hideEvent(e)
        self._timer.stop()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        try:
            paint_pattern(p, QRectF(self.rect()), self._key, self.elapsed(), self._color)
        finally:
            p.end()
