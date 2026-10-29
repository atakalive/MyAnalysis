"""common.mathmd.split_math — チャットの数式の切り出し（Issue #123）。

``SAMPLE_MD`` は数式描画のテスト共通の例の文章（ほかのテストは ``from test_mathmd import SAMPLE_MD``）。
"""
from __future__ import annotations

import os
import re

import pytest

from common.mathmd import MathSpan, math_key, split_math

SAMPLE_MD = r"""推定量は $\hat{\boldsymbol\beta} = (X^\top X)^{-1} X^\top y$ で、群の平均は $\mu_k = \frac{1}{N_k}\sum_{i \in C_k} x_i$ です。

$$
\hat{\boldsymbol\beta} = \begin{pmatrix} \hat\beta_0 \\ \hat\beta_1 \end{pmatrix}, \quad
I_{\rm box} = \tfrac{1}{2}\lVert x - \hat x \rVert^2
$$

\begin{equation}
f(x) = \begin{cases} 1 & x \ge 0 \\ 0 & x < 0 \end{cases}
\end{equation}

$$\begin{aligned} a &= b + c \\ &= d \end{aligned}$$
"""


def _texs(md, **kw):
    return [(s.tex, s.display) for s in split_math(md, **kw)[1]]


def test_sample_md_counts_and_no_leftovers():
    md, spans = split_math(SAMPLE_MD)
    assert sum(not s.display for s in spans) == 2
    assert sum(s.display for s in spans) == 3
    for tok in ("$", "\\begin", "\\end"):
        assert tok not in md
    assert spans[2].tex.startswith(r"\hat{\boldsymbol\beta} = \begin{pmatrix}")
    assert spans[3].tex.startswith(r"\begin{equation}")
    assert spans[3].tex.endswith(r"\end{equation}")


def test_key_is_sha1_of_kind_and_tex():
    import hashlib
    assert math_key("x", True) == hashlib.sha1(b"dx").hexdigest()[:16]
    assert math_key("x", False) == hashlib.sha1(b"ix").hexdigest()[:16]
    md, spans = split_math("$x$")
    assert spans == [MathSpan(math_key("x", False), "x", False)]
    assert md == f"![m](math:i/{spans[0].key})"


@pytest.mark.parametrize("src,expected", [
    ("a $x$ b", [("x", False)]),
    (r"a \(x\) b", [("x", False)]),
    (r"a \[x\] b", [("x", True)]),
    ("a $$x$$ b", [("x", True)]),
    ("$$\nx\n$$", [("x", True)]),
    ("\\begin{align*}\na &= b\n\\end{align*}", [("\\begin{align*}\na &= b\n\\end{align*}", True)]),
    ("\\begin{gather}\nx\n\\end{gather}", [("\\begin{gather}\nx\n\\end{gather}", True)]),
    ("\\begin{multline}\nx\n\\end{multline}", [("\\begin{multline}\nx\n\\end{multline}", True)]),
    ("$$\\begin{aligned} a &= b \\end{aligned}$$", [("\\begin{aligned} a &= b \\end{aligned}", True)]),
])
def test_delimiters(src, expected):
    assert _texs(src) == expected


def test_aligned_without_dollars_is_not_math():
    # aligned は素の \begin の対象外（$$ の中で使う）
    assert _texs("\\begin{aligned}\na\n\\end{aligned}") == []


@pytest.mark.parametrize("src", [
    "$5 and $10",
    "$ x$",
    "$x $",
    "$x$5",
    r"\$x\$",
    r"\$x$ y$",
    r"\\[x\\]",
    r"\\(x\\)",
])
def test_pandoc_rules_and_escapes(src):
    assert _texs(src) == []
    assert split_math(src)[0] == src


def test_odd_backslashes_before_bracket_is_delimiter():
    assert _texs(r"\\\[x\]") == [("x", True)]


def test_code_links_tool_lines_untouched():
    src = "`$a$` and ``$b$`` [t](http://h/$c$) <http://h/$d$>\n```\n$e$\n```\n~~~\n$f$\n~~~\n$g$"
    md, spans = split_math(src)
    assert [s.tex for s in spans] == ["g"]
    assert md.startswith("`$a$` and ``$b$`` [t](http://h/$c$) <http://h/$d$>\n```\n$e$\n```")
    tool = "🔧 Bash: echo $a $b$\nok $x$"
    md, spans = split_math(tool, skip_line=lambda ln: ln.startswith("🔧 "))
    assert [s.tex for s in spans] == ["x"]
    assert md.startswith("🔧 Bash: echo $a $b$\n")


def test_unclosed_and_blank_lines():
    assert _texs("$$\nx") == []
    assert _texs("$$a\n\nb$$") == []
    assert _texs("$a\n\nb$") == []
    assert _texs("\\[a\n\nb\\]") == []
    assert _texs("\\begin{equation}\na\n\n\\end{equation}") == []
    assert _texs("> $$a\n>\n> b$$") == []      # 引用の接頭辞だけの行も空行
    # 閉じ忘れの $$ が後ろの数式を呑み込まない
    assert _texs("$$x\n\nthen $y$") == [("y", False)]


def test_mid_line_display_is_inline_image():
    md, spans = split_math("text $$x$$ more")
    assert md == f"text ![m](math:d/{spans[0].key}) more"


def test_quote_display_prefix_carried_and_stripped():
    md, spans = split_math("> $$\n> a \\\\\n> b\n> $$")
    assert spans[0].tex == "a \\\\\nb"
    assert md == f">\n> ![m](math:d/{spans[0].key})\n>"
    md, spans = split_math("> - $$\n>   x\n>   $$")
    assert spans[0].tex == "x"
    assert md == f"> - ![m](math:d/{spans[0].key})"


def test_list_continuation_display():
    md, spans = split_math("- a\n  $$x$$\n- b")
    assert md == f"- a\n\n  ![m](math:d/{spans[0].key})\n\n- b"


# ---- setMarkdown しても項目の構造が変わらない ----

def _qt():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _struct(md):
    from PySide6.QtGui import QTextDocument, QTextFormat
    d = QTextDocument()
    d.setMarkdown(md)
    items, quotes, math_quotes = [], [], []
    b = d.begin()
    while b.isValid():
        is_math = b.text() in ("X", "\ufffc")
        level = b.blockFormat().property(QTextFormat.Property.BlockQuoteLevel)
        tl = b.textList()
        if tl is not None:
            items.append((tl.itemNumber(b), tl.format().indent()))
        if is_math:
            math_quotes.append(level)
        else:
            quotes.append(level)
        b = b.next()
    return items, quotes, math_quotes


@pytest.mark.parametrize("src", [
    "- $$x$$",
    "1. a\n2. $$x$$\n3. c",
    "- a\n  $$x$$\n- b",
    "- a\n  - $$x$$",
    "> $$x$$",
    "> - $$x$$",
    "- $$\n  x\n  $$",
])
def test_list_and_quote_structure_preserved(src):
    _qt()
    md, _ = split_math(src)
    x = re.sub(r"\$\$.*?\$\$", "X", src, flags=re.S)
    got, want = _struct(md), _struct(x)
    assert got[:2] == want[:2]
    # 数式の段落も、元と同じ引用の深さに留まる
    assert set(got[2]) <= set(want[2]) | set(want[1]) | {None}
