"""作業中の印（gui/busy_glyph.py）: 形の描き方・選び方・時計。画素を見るので offscreen 専用。
pjdeck の tests/test_busy_glyph.py から移したもの。"""
import os
import random

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # PySide6 の import より前

import pytest
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPalette
from PySide6.QtWidgets import QWidget

from gui.busy_glyph import GLYPH_COLOR, PATTERN_KEYS, PATTERNS, BusyGlyph, paint_pattern, pick_pattern

RECT = QRectF(10, 10, 20, 20)


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def draw(key, t):
    img = QImage(40, 40, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    try:
        paint_pattern(p, RECT, key, t, QColor(GLYPH_COLOR))
    finally:
        p.end()
    return img


def drawn_pixels(img):
    return {(x, y) for y in range(img.height()) for x in range(img.width())
            if img.pixelColor(x, y).alpha() > 0}


@pytest.mark.parametrize("pattern", PATTERNS, ids=PATTERN_KEYS)
def test_pattern_moves_inside_its_rect(qapp, pattern):
    inside = {(x, y) for y in range(10, 30) for x in range(10, 30)}
    images = [draw(pattern.key, pattern.period * k / 4) for k in range(4)]
    for img in images:
        drawn = drawn_pixels(img)
        assert drawn, "何も描いていない"
        assert drawn <= inside, sorted(drawn - inside)[:5]
    assert any(img != images[0] for img in images[1:]), "時刻で姿が変わらない"


def alphas(key, t, size=48):
    """size × size の rect いっぱいに描いた、画素ごとの不透明度の並び。"""
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    try:
        paint_pattern(p, QRectF(0, 0, size, size), key, t, QColor(GLYPH_COLOR))
    finally:
        p.end()
    return bytes(img.constBits())[3::4]   # B, G, R, A の並び（little endian）の A


# 刻みを細かくする倍率。scan は横が速く、1 本の端で濃さを上げ下げする区間が、2 周 240 コマの刻みでは 3 コマしかない。
FINER = {"scan": 6}


@pytest.mark.parametrize("pattern", PATTERNS, ids=PATTERN_KEYS)
def test_pattern_moves_continuously(qapp, pattern):
    """2 周を細かく刻んで、隣のコマとの違い（≒ 動きの速さ）を見る。周の切れ目もまたぐ。
    - 違いに飛び抜けて大きいものが無い（急に現れる・消える・濃さが切り替わることがない）
    - 違いが隣どうしで急に変わらない（止まってから急に動き出す・端で跳ね返ることがない）
    閾値は実測から（直した形は 2.01 倍・0.32 倍まで、直す前の ripple・coin は 5.8 倍・dots は 0.82 倍）。"""
    steps = 240 * FINER.get(pattern.key, 1)
    frames = [alphas(pattern.key, 2 * pattern.period * k / steps) for k in range(steps + 1)]
    diffs = [sum(abs(a - b) for a, b in zip(x, y)) for x, y in zip(frames, frames[1:])]
    mean = sum(diffs) / len(diffs)
    assert mean > 0
    assert max(diffs) <= 3 * mean, (max(diffs), mean)
    accel = max(abs(b - a) for a, b in zip(diffs, diffs[1:]))
    assert accel <= 0.5 * mean, (accel, mean)


def period_of(key):
    return next(pt.period for pt in PATTERNS if pt.key == key)


def test_moon_waxes_from_the_upper_right(qapp):
    """moon: 新月 → 上弦（右上が明るい）→ 満月 → 下弦（左下が明るい）。境は斜め。暗い側も薄く描く。"""
    period = period_of("moon")

    def around(phase):
        """中心から右・上・左・下に 12px の点の不透明度。"""
        a = alphas("moon", period * phase)
        return a[24 * 48 + 36], a[12 * 48 + 24], a[24 * 48 + 12], a[36 * 48 + 24]

    def dim(alpha):
        return 32 < alpha < 96

    assert all(dim(alpha) for alpha in around(0))
    right, up, left, down = around(0.25)
    assert right == up == 255 and dim(left) and dim(down)
    assert around(0.5) == (255, 255, 255, 255)
    right, up, left, down = around(0.75)
    assert left == down == 255 and dim(right) and dim(up)


def centroid(key, t, size=48):
    a = alphas(key, t, size)
    total = sum(a)
    return (sum(v * (i % size) for i, v in enumerate(a)) / total,
            sum(v * (i // size) for i, v in enumerate(a)) / total)


def test_scan_sweeps_left_to_right_and_top_to_bottom(qapp):
    """scan: 6 本のどれも左から右へ横切り、1 本ごとに下へ進む。その後、横には振らずに右下から左上へまっすぐ戻る。
    1 本の切れ目（左端へ戻る所）と、戻りの始めと終わりでは点が見えない。"""
    period = period_of("scan")

    def at(line, u):
        """line 本目（0 から）の u（0 = 左端、1 = 右端）の時刻。1 周は 6 本と戻り（2 本分）で 8 本分、時刻 0 は
        1 本目の 1/4 の所（_scan と同じ）。line = 6 は戻りで、u は 2 本分の時間の中の位置。"""
        return period * ((line + u * (2 if line == 6 else 1)) / 8 - 0.25 / 8)

    rows = []
    for line in range(6):
        xs = [centroid("scan", at(line, u))[0] for u in (0.3, 0.5, 0.7)]
        assert xs[0] < xs[1] < xs[2]
        rows.append(centroid("scan", at(line, 0.5))[1])
    assert all(a < b for a, b in zip(rows, rows[1:]))
    back = [centroid("scan", at(6, u)) for u in (0.3, 0.5, 0.7)]
    assert back[0][0] > back[1][0] > back[2][0] and back[0][1] > back[1][1] > back[2][1]
    for line, u in [(1, 0), (2, 0), (3, 0), (4, 0), (5, 0), (6, 0), (6, 1)]:
        assert sum(alphas("scan", at(line, u))) == 0


def test_unknown_key_draws_spark(qapp):
    assert draw("nope", 0.3) == draw("spark", 0.3)


def test_pick_pattern_never_repeats_and_covers_all():
    rng = random.Random(1)
    previous = None
    seen = set()
    for _ in range(300):
        key = pick_pattern(rng, previous)
        assert key in PATTERN_KEYS and key != previous
        seen.add(key)
        previous = key
    assert seen == set(PATTERN_KEYS)


def test_pick_pattern_without_previous_picks_from_all():
    rng = random.Random(2)
    assert {pick_pattern(rng, None) for _ in range(300)} == set(PATTERN_KEYS)
    assert pick_pattern(rng, "unknown") in PATTERN_KEYS


def test_set_pattern_restarts_the_motion(qapp):
    now = [100.0]
    glyph = BusyGlyph(clock=lambda: now[0])
    now[0] = 105.0
    assert glyph.elapsed() == 5.0
    glyph.set_pattern("dots")
    assert glyph.pattern() == "dots" and glyph.elapsed() == 0.0
    glyph.set_pattern("nope")
    assert glyph.pattern() == "spark"


def test_timer_runs_only_while_visible(qapp):
    host = QWidget()
    glyph = BusyGlyph(host)
    try:
        assert not glyph.is_animating()
        host.show()
        qapp.processEvents()
        assert glyph.isVisible() and glyph.is_animating()
        host.hide()                 # 親が隠れる（隠れているタブ）と止まる
        assert not glyph.is_animating()
        host.show()
        assert glyph.is_animating()
        glyph.hide()
        assert not glyph.is_animating()
        glyph.show()
        assert glyph.is_animating()
    finally:
        host.close()
        host.deleteLater()


def test_background_is_not_filled(qapp):
    host = QWidget()
    pal = host.palette()
    pal.setColor(QPalette.ColorRole.Window, QColor("#123456"))
    host.setPalette(pal)
    host.setAutoFillBackground(True)
    host.resize(40, 40)
    glyph = BusyGlyph(host, clock=lambda: 0.0)
    glyph.setGeometry(10, 10, 20, 20)
    assert not glyph.autoFillBackground()
    img = host.grab().toImage()
    assert img.pixelColor(10, 10).rgb() == QColor("#123456").rgb()   # 印の左上の隅（形は真ん中に描く）は親の色
