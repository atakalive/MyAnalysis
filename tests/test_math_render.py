"""gui/math_render.py — 数式の画像化と一時文書への組込み（Issue #123）。偽のエンジンで動かす。"""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontInfo, QTextCursor, QTextDocument, QTextFormat
from PySide6.QtWidgets import QApplication

from common import mathjax_install
from common.mathmd import split_math
from gui import math_render
from gui.math_engine import MathEngine, MathPrefs, MathResult
from gui.math_render import (
    DISPLAY_PROP, TEX_PROP, baseline_pads, build_markdown_doc, prepare_svg, rasterize,
)

# viewBox: 幅 1000、ベースラインの上 750・下 250（1000 = 1em）
SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -750 1000 1000">'
       '<g stroke="currentColor" fill="currentColor" stroke-width="0" transform="scale(1,-1)">'
       '<rect x="100" y="-200" width="800" height="900"></rect></g></svg>')
WIDE = SVG.replace('viewBox="0 -750 1000 1000"', 'viewBox="0 -750 40000 1000"')


@pytest.fixture(autouse=True)
def _app():
    app = QApplication.instance() or QApplication([])
    font_ids = []
    try:
        # Windows の offscreen は OS のフォントを列挙せず、Qt 自体もフォントを同梱しない。
        if not QFontDatabase.families():
            from matplotlib import get_data_path
            font_dir = Path(get_data_path()) / "fonts" / "ttf"
            for name in ("DejaVuSans.ttf", "DejaVuSansMono.ttf"):
                font_id = QFontDatabase.addApplicationFont(str(font_dir / name))
                assert font_id >= 0, f"could not load test font: {font_dir / name}"
                font_ids.append(font_id)
                assert QFontDatabase.applicationFontFamilies(font_id), name
        assert QFontInfo(_font()).pixelSize() == 20, "test font must resolve to 20 px"
        yield app
    finally:
        for font_id in reversed(font_ids):
            assert QFontDatabase.removeApplicationFont(font_id), font_id


class FakeEngine(QObject):
    rendered = Signal()

    def __init__(self, prefs=None, results=None):
        super().__init__()
        self.prefs = prefs or MathPrefs(enabled=True, font="mathjax-dejavu", scale=1.0, bold=False)
        self.results = results if results is not None else {}
        self.calls = []

    def request(self, tex, display, *, force=False):
        self.calls.append((tex, display))
        return self.results.get((tex, display), MathResult("pending"))

    def is_pending(self, key):
        return self.request(key[1], key[2]).status == "pending"


def _font(px=20):
    f = QFont()
    f.setPixelSize(px)
    return f


def _build(md, eng, **kw):
    kw.setdefault("font", _font())
    kw.setdefault("color", QColor("#dcdcdc"))
    kw.setdefault("max_width", 2000.0)
    return build_markdown_doc(md, engine=eng, **kw)


def _objects(doc):
    """(位置, 書式) の一覧（画像とソース表示の両方）。"""
    out = []
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            fmt = frag.charFormat()
            if fmt.property(TEX_PROP):
                out.append((frag.position(), fmt, frag.text(), block))
            it += 1
        block = block.next()
    return out


def test_svg_becomes_image_sized_by_viewbox():
    eng = FakeEngine(results={("x", False): MathResult("ready", SVG)})
    doc, res, pending = _build("a $x$ b", eng)
    assert pending == set()
    (pos, fmt, text, _), = _objects(doc)
    assert fmt.isImageFormat() and text == "￼"
    img = fmt.toImageFormat()
    assert img.name() in res
    assert fmt.property(TEX_PROP) == "x" and fmt.property(DISPLAY_PROP) is False
    assert img.width() == pytest.approx(20.0)              # 1000/1000 em × 20 px
    from PySide6.QtGui import QFontMetrics
    top, bottom = baseline_pads(15.0, 5.0, QFontMetrics(_font()).xHeight())
    import math
    assert img.height() == pytest.approx(math.ceil(20.0 + top + bottom - 1e-6))   # 高さは整数に揃える
    assert img.verticalAlignment() == fmt.VerticalAlignment.AlignMiddle
    assert img.toolTip() == "x"


@pytest.mark.parametrize("results,enabled,want_pending", [
    ({("x", False): MathResult("ready", SVG)}, False, False),          # 無効（キャッシュの SVG も使わない）
    ({("x", False): MathResult("error", None, "Undefined")}, True, False),
    ({}, True, True),                                                 # 変換中
    ({("x", False): MathResult("error", None, "too_long")}, True, False),
    ({("x", False): MathResult("error", None, "unavailable")}, True, False),
])
def test_source_fallback(results, enabled, want_pending):
    eng = FakeEngine(MathPrefs(enabled=enabled), results)
    doc, res, pending = _build("a $x$ b", eng)
    assert res == {}
    assert doc.toPlainText() == "a $x$ b"
    (_, fmt, text, _), = _objects(doc)
    assert text == "$x$" and not fmt.isImageFormat()
    mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    assert fmt.font().family() == mono.family()
    assert fmt.font().pixelSize() == 20
    assert pending == ({("mathjax-dejavu", "x", False)} if want_pending else set())


def test_display_centered_and_source_left_with_newlines():
    md = "$$\na \\\\\nb\n$$"
    tex = split_math(md)[1][0].tex
    eng = FakeEngine(results={(tex, True): MathResult("ready", SVG)})
    doc, _, _ = _build(md, eng)
    (_, fmt, _, block), = _objects(doc)
    assert fmt.isImageFormat()
    assert block.blockFormat().alignment() & Qt.AlignmentFlag.AlignHCenter
    eng2 = FakeEngine(MathPrefs(enabled=False))
    doc, _, _ = _build(md, eng2)
    (_, fmt, text, block), = _objects(doc)
    assert text == "$$a \\\\\u2028b$$"                  # 段落は割らず、行区切りで改行する
    assert "$$a \\\\\nb$$" in doc.toPlainText()          # プレーンテキストでは改行に戻る
    assert block.blockFormat().alignment() & Qt.AlignmentFlag.AlignLeft
    assert not block.blockFormat().alignment() & Qt.AlignmentFlag.AlignHCenter


def test_block_color_used():
    eng = FakeEngine(results={("x", False): MathResult("ready", SVG)})
    doc, res, _ = _build("> 💭 $x$", eng, block_color=lambda b: QColor("#ff0000"))
    img = next(iter(res.values()))
    colors = {img.pixelColor(x, y).name() for x in range(img.width()) for y in range(img.height())
              if img.pixelColor(x, y).alpha() == 255}
    assert "#ff0000" in colors
    doc, res2, _ = _build("> 💭 $x$", eng)
    assert set(res2) != set(res)


def test_zoom_hits_svg_cache(monkeypatch):
    from llm_bridge.paths import update_ui_pref
    update_ui_pref("math_render", True)
    eng = MathEngine()
    starts = []
    monkeypatch.setattr(eng, "_start", lambda: starts.append(1))
    eng._put((eng.prefs.font, "x", False), "svg", SVG)
    _, res1, p1 = _build("$x$", eng, font=_font(20))
    _, res2, p2 = _build("$x$", eng, font=_font(30))
    assert starts == [] and p1 == p2 == set()
    i1, i2 = next(iter(res1.values())), next(iter(res2.values()))
    assert i2.width() > i1.width()
    eng.stop()


def test_scale_bold_font_change_image_and_key():
    r = {("x", False): MathResult("ready", SVG)}
    base = _build("$x$", FakeEngine(MathPrefs(True, "mathjax-dejavu", 1.0, False), r))[1]
    big = _build("$x$", FakeEngine(MathPrefs(True, "mathjax-dejavu", 1.35, False), r))[1]
    bold = _build("$x$", FakeEngine(MathPrefs(True, "mathjax-dejavu", 1.0, True), r))[1]
    other = _build("$x$", FakeEngine(MathPrefs(True, "mathjax-fira", 1.0, False), r))[1]
    (k0, i0), = base.items()
    (k1, i1), = big.items()
    (k2, i2), = bold.items()
    (k3, _), = other.items()
    assert len({k0, k1, k2, k3}) == 4
    assert i1.width() > i0.width()
    assert i2 != i0


def test_bold_grows_by_stroke():
    em = 100.0
    plain = rasterize(SVG, em_px=em, color="#ffffff", bold=False, dpr=1.0, x_height=10.0)
    bold = rasterize(SVG, em_px=em, color="#ffffff", bold=True, dpr=1.0, x_height=10.0)
    step = mathjax_install.BOLD_STROKE / 1000 * em
    assert bold.width - plain.width == pytest.approx(step)
    assert (bold.above + bold.depth) - (plain.above + plain.depth) == pytest.approx(step)
    assert bold.above - plain.above == pytest.approx(step / 2)
    assert bold.depth - plain.depth == pytest.approx(step / 2)
    src = prepare_svg(SVG, "#123456", True)
    assert 'stroke-width="15" stroke-linejoin="round" stroke-linecap="round"' in src
    assert "currentColor" not in src and "#123456" in src


@pytest.mark.parametrize("dpr", [1.0, 1.25, 1.5, 2.0, 2.5, 3.0])
def test_raster_physical_size_within_cap_after_rounding(dpr):
    tall = SVG.replace('viewBox="0 -750 1000 1000"', 'viewBox="0 -75000 1000 100000"')
    r = rasterize(tall, em_px=100.0, color="#ffffff", bold=True, dpr=dpr, x_height=10.0)
    assert r.image.width() <= 4096 and r.image.height() <= 4096
    assert r.height == int(r.height)                     # 行の中の画像の高さは整数
    assert r.image.height() == round(r.height * dpr)


def test_raster_cap_when_rounding_up_crosses_limit():
    # 余白込みの高さが 1500·1.2 − 323.2/2 = 1638.4 px。dpr 2.5 で 4096 px ちょうど（縮めない）だが、
    # 整数へ切り上げると 1639 px → 4098 px になって上限を超える。上限は切り上げた後の大きさで守る
    r = rasterize(SVG, em_px=1200.0, color="#ffffff", bold=False, dpr=2.5, x_height=323.2)
    assert r.image.height() <= 4096 and r.image.width() <= 4096
    assert r.height == 1638


def test_raster_limits():
    r = rasterize(WIDE, em_px=200.0, color="#ffffff", bold=False, dpr=2.0, x_height=10.0)
    assert max(r.image.width(), r.image.height()) <= 4096
    eng = FakeEngine(results={("w", True): MathResult("ready", WIDE)})
    doc, _, _ = _build("$$w$$", eng, max_width=100.0)
    (_, fmt, _, _), = _objects(doc)
    # 0.6 倍までしか縮めない（それ以上は横スクロールに任せる）
    assert fmt.toImageFormat().width() == pytest.approx(40 * 20 * 0.6)


@pytest.mark.parametrize("above,depth,xh,want", [
    (10.0, 8.0, 6.0, (1.0, 0.0)),        # s = 8 − 10 + 3 = 1
    (10.0, 7.0, 6.0, (0.0, 0.0)),        # s = 0
    (20.0, 2.0, 6.0, (0.0, 15.0)),       # s = −15
    (12.0, -3.0, 6.0, (0.0, 12.0)),      # depth < 0: s = −3 − 12 + 3 = −12
])
def test_baseline_pads(above, depth, xh, want):
    assert baseline_pads(above, depth, xh) == want


@pytest.mark.parametrize("svg", [SVG, SVG.replace('viewBox="0 -750 1000 1000"', 'viewBox="0 -1400 1000 1600"'),
                                 SVG.replace('viewBox="0 -750 1000 1000"', 'viewBox="0 -900 1000 700"')])
def test_baseline_alignment_in_layout(svg):
    from PySide6.QtGui import QFontMetrics
    font = _font(20)
    xh = QFontMetrics(font).xHeight()     # Qt の resizeInlineObject と同じ（整数の QFontMetrics）
    r = rasterize(svg, em_px=40.0, color="#ffffff", bold=False, dpr=1.0, x_height=xh)
    doc = QTextDocument()
    doc.setDefaultFont(font)
    doc.addResource(QTextDocument.ResourceType.ImageResource, "mathimg:t", r.image)
    from PySide6.QtGui import QTextCharFormat, QTextImageFormat
    c = QTextCursor(doc)
    c.insertText("x")
    f = QTextImageFormat()
    f.setName("mathimg:t")
    f.setWidth(r.width)
    f.setHeight(r.height)
    f.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignMiddle)
    f.setFont(font)
    c.insertImage(f)
    doc.setTextWidth(1000)
    doc.documentLayout()
    asc = doc.begin().layout().lineAt(0).ascent()
    assert asc == pytest.approx(r.top + r.above, abs=0.01)


def test_markers_are_untouched_in_tool_lines():
    eng = FakeEngine(results={("x", False): MathResult("ready", SVG)})
    doc, _, _ = _build("🔧 Bash: echo $x$\n\nok $x$", eng,
                       skip_line=lambda ln: ln.startswith("🔧 "))
    assert "echo $x$" in doc.toPlainText()
    assert len(_objects(doc)) == 1


def test_property_constants_distinct():
    assert TEX_PROP == int(QTextFormat.Property.UserProperty)
    assert DISPLAY_PROP == TEX_PROP + 1
    assert math_render.math_at(QTextFormat().toCharFormat()) is None
