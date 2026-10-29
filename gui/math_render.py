"""数式の画像化と、Markdown の一時文書への組込み（Issue #123）。

``build_markdown_doc`` は ``split_math`` → ``setMarkdown`` → ``math:`` 画像の差し替えを行う。描けた式は
SVG をラスタにした画像（ベースラインを本文に揃える透明な余白付き）、描けない式（モジュールが無効・
変換中・エラー・エンジンが使えない・上限超え）は等幅・灰色の LaTeX ソースにする。
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import OrderedDict
from collections.abc import Callable

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import (
    QColor, QFont, QFontDatabase, QFontInfo, QFontMetrics, QFontMetricsF, QGuiApplication, QImage,
    QPainter, QTextBlock, QTextCharFormat, QTextCursor, QTextDocument,
    QTextFormat, QTextImageFormat,
)
from PySide6.QtSvg import QSvgRenderer

from common import mathjax_install
from common.mathmd import MATH_SCHEME, split_math
from gui import math_engine as _me
from gui.math_engine import MathKey

TEX_PROP = int(QTextFormat.Property.UserProperty)           # 数式の TeX（画像・ソース表示の両方）
DISPLAY_PROP = int(QTextFormat.Property.UserProperty) + 1   # display か
IMG_SCHEME = "mathimg:"
SOURCE_COLOR = "#9a9a9a"
MAX_RASTER_PX = 4096
LINE_SEP = "\u2028"          # display のソース表示で TeX の改行に使う行区切り
DISPLAY_MIN_SHRINK = 0.6
RASTER_CACHE_BYTES = 64 * 1024 * 1024

_VIEWBOX_RE = re.compile(r'viewBox="\s*([-\d.eE]+)[\s,]+([-\d.eE]+)[\s,]+([-\d.eE]+)[\s,]+([-\d.eE]+)\s*"')
_OBJ = "￼"


def baseline_pads(above: float, depth: float, x_height: float) -> tuple[float, float]:
    """数式のベースラインを本文のベースラインに揃える、画像の上と下の余白（px）。

    AlignMiddle の画像（高さ H）は、行の中で ascent が (H + x_height/2)/2 になる（Qt の
    QTextDocumentLayout::resizeInlineObject）。揃う条件は top − bottom = depth − above + x_height/2。"""
    s = depth - above + x_height / 2
    return (s, 0.0) if s >= 0 else (0.0, -s)


def svg_viewbox(svg: str) -> tuple[float, float, float, float] | None:
    m = _VIEWBOX_RE.search(svg)
    if not m:
        return None
    try:
        vb = tuple(float(x) for x in m.groups())
    except ValueError:
        return None
    return vb if vb[2] > 0 and vb[3] > 0 else None


def prepare_svg(svg: str, color: str, bold: bool) -> str:
    """currentColor を文字色に置き換え、太めなら輪郭を足して viewBox を四方に広げる。"""
    out = svg.replace("currentColor", color)
    if bold:
        w = mathjax_install.BOLD_STROKE
        out = out.replace('stroke-width="0"',
                          f'stroke-width="{w}" stroke-linejoin="round" stroke-linecap="round"')
        vb = svg_viewbox(out)
        if vb is not None:
            h = w / 2
            x, y, vw, vh = vb
            out = _VIEWBOX_RE.sub(f'viewBox="{x - h:g} {y - h:g} {vw + w:g} {vh + w:g}"', out, count=1)
    return out


class _Raster:
    """余白込みの画像と論理の大きさ（px）。above / depth は数式のベースラインの上と下、top は上の余白。"""
    __slots__ = ("image", "width", "height", "above", "depth", "top", "nbytes")

    def __init__(self, image: QImage, width: float, height: float,
                 above: float, depth: float, top: float):
        self.image, self.width, self.height = image, width, height
        self.above, self.depth, self.top = above, depth, top
        self.nbytes = image.sizeInBytes()


_raster_cache: OrderedDict[tuple, _Raster] = OrderedDict()
_raster_bytes = 0


def clear_raster_cache() -> None:
    global _raster_bytes
    _raster_cache.clear()
    _raster_bytes = 0


def _cache_put(key: tuple, r: _Raster) -> None:
    global _raster_bytes
    _raster_cache[key] = r
    _raster_bytes += r.nbytes
    while _raster_bytes > RASTER_CACHE_BYTES and len(_raster_cache) > 1:
        _, old = _raster_cache.popitem(last=False)
        _raster_bytes -= old.nbytes


def raster_caps(dpr: float) -> tuple[float, int]:
    """論理 px での幅の上限と、整数の高さの上限。どちらも ``round(x * dpr) <= MAX_RASTER_PX`` になる。"""
    return MAX_RASTER_PX / dpr, max(1, math.floor(MAX_RASTER_PX / dpr + 1e-9))


def rasterize(svg: str, *, em_px: float, color: str, bold: bool, dpr: float,
              x_height: float, max_width: float | None = None) -> _Raster | None:
    """SVG を、ベースラインの余白付きの QImage（devicePixelRatio 付き）にする。読めなければ None。"""
    src = prepare_svg(svg, color, bold)
    vb = svg_viewbox(src)
    if vb is None:
        return None
    _, miny, vw, vh = vb
    scale = em_px / 1000.0
    if max_width is not None and vw * scale > max_width > 0:
        scale *= max(DISPLAY_MIN_SHRINK, max_width / (vw * scale))
    w, above, depth = vw * scale, -miny * scale, (vh + miny) * scale
    top, bottom = baseline_pads(above, depth, x_height)
    h = top + above + depth + bottom
    # 一辺 4096 px（dpr 込み）に収める。高さは下で整数に切り上げるので、上限は「物理で 4096 px を
    # 超えない最大の整数の論理の高さ」で掛ける（切り上げた後の round(h * dpr) も 4096 以下になる）
    w_cap, h_cap = raster_caps(dpr)
    fit = min(1.0, w_cap / max(w, 1e-9), h_cap / max(h, 1e-9))
    w, h, top, above, depth = w * fit, h * fit, top * fit, above * fit, depth * fit
    # Qt は行の中の画像の高さを整数に丸めてから ascent を出すので、高さを整数にする（上下に同じだけ
    # 足すので top − bottom は変わらない）
    extra = math.ceil(h - 1e-6) - h
    h += extra
    top += extra / 2
    renderer = QSvgRenderer(QByteArray(src.encode("utf-8")))
    if not renderer.isValid():
        return None
    img = QImage(max(1, round(w * dpr)), max(1, round(h * dpr)), QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(p, QRectF(0, top * dpr, w * dpr, (above + depth) * dpr))
    p.end()
    img.setDevicePixelRatio(dpr)
    return _Raster(img, w, h, above, depth, top)


def _font_px(font: QFont) -> float:
    info = QFontInfo(font)
    px = info.pixelSize()
    return float(px) if px > 0 else QFontMetricsF(font).height() * 0.75


def _mono_format(font: QFont, color: QColor | str, tex: str, display: bool) -> QTextCharFormat:
    fmt = QTextCharFormat()
    mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    mono.setPixelSize(max(1, round(_font_px(font))))
    fmt.setFont(mono)
    fmt.setForeground(QColor(SOURCE_COLOR))
    fmt.setProperty(TEX_PROP, tex)
    fmt.setProperty(DISPLAY_PROP, display)
    fmt.setToolTip(tex)
    return fmt


def _dpr() -> float:
    app = QGuiApplication.instance()
    try:
        return float(app.devicePixelRatio()) if app is not None else 1.0
    except Exception:  # noqa: BLE001
        return 1.0


def build_markdown_doc(
    md: str, *, font: QFont, color: QColor, max_width: float,
    block_color: Callable[[QTextBlock], QColor | None] | None = None,
    skip_line: Callable[[str], bool] | None = None,
    engine=None,
) -> tuple[QTextDocument, dict[str, QImage], set[MathKey]]:
    """md を数式付きで一時文書にする。戻りは (一時 doc, resources（URL の文字列 → 画像）, pending)。

    呼び出し側は挿入先の doc へ addResource してから insertFragment する（逆だと QTextBrowser の
    loadResource が壊れた画像をキャッシュする）。"""
    text, spans = split_math(md, skip_line=skip_line)
    by_key = {s.key: s for s in spans}
    doc = QTextDocument()
    doc.setDefaultFont(font)
    doc.setMarkdown(text, QTextDocument.MarkdownFeature.MarkdownDialectGitHub)
    resources: dict[str, QImage] = {}
    pending: set[MathKey] = set()
    if not spans:
        return doc, resources, pending
    eng = engine if engine is not None else _me.engine()
    prefs = eng.prefs
    found: list[tuple[int, str, bool]] = []       # (位置, key, display)
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            fmt = frag.charFormat()
            if fmt.isImageFormat():
                name = fmt.toImageFormat().name()
                if name.startswith(MATH_SCHEME) and len(name) > len(MATH_SCHEME) + 2:
                    kind, key = name[len(MATH_SCHEME)], name[len(MATH_SCHEME) + 2:]
                    if key in by_key:
                        for off, ch in enumerate(frag.text()):
                            if ch == _OBJ:
                                found.append((frag.position() + off, key, kind == "d"))
            it += 1
        block = block.next()
    em_px = _font_px(font) * prefs.scale
    # Qt の QTextDocumentLayout::resizeInlineObject は整数の QFontMetrics の xHeight を使う
    x_height = float(QFontMetrics(font).xHeight())
    dpr = _dpr()
    for pos, key, display in reversed(found):
        span = by_key[key]
        blk = doc.findBlock(pos)
        c = block_color(blk) if block_color is not None else None
        col = QColor(c if c is not None else color).name()
        res = eng.request(span.tex, display)
        cur = QTextCursor(doc)
        cur.setPosition(pos)
        cur.setPosition(pos + 1, QTextCursor.MoveMode.KeepAnchor)
        raster = None
        if res.status == "ready" and prefs.enabled and res.svg:
            mw = max_width if display else None
            rkey = (prefs.font, key, round(em_px, 2), dpr, col, prefs.bold, round(mw or 0, 1))
            raster = _raster_cache.get(rkey)
            if raster is None:
                raster = rasterize(res.svg, em_px=em_px, color=col, bold=prefs.bold, dpr=dpr,
                                   x_height=x_height, max_width=mw)
                if raster is not None:
                    _cache_put(rkey, raster)
            else:
                _raster_cache.move_to_end(rkey)
            if raster is not None:
                url = IMG_SCHEME + hashlib.sha1(repr(rkey).encode("utf-8")).hexdigest()[:20]
                resources[url] = raster.image
                f = QTextImageFormat()
                f.setName(url)
                f.setWidth(raster.width)
                f.setHeight(raster.height)
                f.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignMiddle)
                f.setFont(font)
                f.setProperty(TEX_PROP, span.tex)
                f.setProperty(DISPLAY_PROP, display)
                f.setToolTip(span.tex)
                cur.insertImage(f)
                continue
        if res.status == "pending":
            pending.add((prefs.font, span.tex, display))
        _insert_source(cur, span.tex, display, font)
    _center_display_blocks(doc)
    return doc, resources, pending


def _insert_source(cur: QTextCursor, tex: str, display: bool, font: QFont) -> None:
    fmt = _mono_format(font, SOURCE_COLOR, tex, display)
    if not display:
        cur.insertText(f"${tex}$", fmt)
        return
    # TeX の改行は行区切り（U+2028）に置き換える。段落を割らずに改行して見せる（toPlainText では "\n" に戻る）
    body = "$$" + tex.replace("\n", LINE_SEP) + "$$"
    cur.insertText(body, fmt)
    bf = cur.blockFormat()
    bf.setAlignment(Qt.AlignmentFlag.AlignLeft)
    cur.setBlockFormat(bf)


def _center_display_blocks(doc: QTextDocument) -> None:
    """display の数式の画像だけでできた段落を中央に揃える。"""
    block = doc.begin()
    while block.isValid():
        text = block.text()
        if text and set(text) == {_OBJ}:
            it = block.begin()
            ok = True
            while not it.atEnd():
                fmt = it.fragment().charFormat()
                if not (fmt.isImageFormat() and fmt.property(DISPLAY_PROP) is True):
                    ok = False
                    break
                it += 1
            if ok:
                c = QTextCursor(block)
                bf = block.blockFormat()
                bf.setAlignment(Qt.AlignmentFlag.AlignHCenter)
                c.setBlockFormat(bf)
        block = block.next()


def math_at(fmt: QTextCharFormat) -> tuple[str, bool] | None:
    """書式が数式（画像かソース表示）なら (tex, display)。"""
    tex = fmt.property(TEX_PROP)
    if isinstance(tex, str) and tex:
        return tex, bool(fmt.property(DISPLAY_PROP))
    return None


__all__ = [
    "build_markdown_doc", "baseline_pads", "prepare_svg", "rasterize", "svg_viewbox",
    "math_at", "clear_raster_cache", "TEX_PROP", "DISPLAY_PROP",
]
