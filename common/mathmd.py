"""チャットの Markdown から数式を切り出す（Qt 非依存。Issue #123）。

Qt の Markdown（``QTextDocument.setMarkdown``）は数式を扱わず、LaTeX を壊す（``\\(`` → ``(``、
``\\\\`` → ``\\``、``$a *b* c$`` の中が斜体になる）。そこで ``setMarkdown`` の前に数式を切り出し、
その場所に画像の参照 ``![m](math:i/<key>)`` / ``![m](math:d/<key>)`` を置く。描くか LaTeX の
ソースで見せるかは ``gui/math_render.py`` が決める（描画モジュールの有無に関係なく、ここは常に通す）。

認識する区切り: ``$$…$$``・``\\[…\\]``・``\\(…\\)``・``$…$``（pandoc の規則）と、行頭の素の
``\\begin{equation|align|gather|multline}``（``*`` 付きも）… ``\\end{…}``。どれも空行を跨がない。
直前のバックスラッシュの数が奇数の区切りは区切りではない。フェンス付きコードブロック・コードスパン・
リンク先 ``](…)``・自動リンク ``<…>``・``skip_line`` が真の行・閉じていない区切りは触らない。
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass

__all__ = ["MathSpan", "math_key", "split_math", "MATH_SCHEME"]

MATH_SCHEME = "math:"


@dataclass(frozen=True)
class MathSpan:
    key: str
    tex: str
    display: bool


def math_key(tex: str, display: bool) -> str:
    return hashlib.sha1((("d" if display else "i") + tex).encode("utf-8")).hexdigest()[:16]


_BLANK_RE = re.compile(r"[\s>]*")
_QUOTE_RE = re.compile(r"(?:[ \t]*>)*")
_MARKER_RE = re.compile(r"([ \t]*)([-*+]|[0-9]{1,9}[.)])([ \t]{1,4})")
_FENCE_RE = re.compile(r"(`{3,}|~{3,})(.*)")
_ENV_RE = re.compile(r"\\begin\{(equation|align|gather|multline)(\*?)\}")
_AUTOLINK_RE = re.compile(
    r"<(?:[A-Za-z][A-Za-z0-9+.\-]{1,31}:[^\s<>]*|[A-Za-z0-9.!#$%&'*+/=?^_`{|}~\-]+@[A-Za-z0-9.\-]+)>"
)


def _strip_quote(line: str) -> str:
    """行頭の引用の接頭辞を外した本文。"""
    return line[_QUOTE_RE.match(line).end():]


def _line_kinds(lines: list[str], skip_line) -> list[bool]:
    """各行が「区切りを跨がせない壁」（空行・フェンスの中・skip_line）か。"""
    barrier = [False] * len(lines)
    fence: tuple[str, int] | None = None
    for i, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        body = _strip_quote(line).lstrip(" \t")
        if fence is not None:
            barrier[i] = True
            m = _FENCE_RE.match(body)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] \
                    and not m.group(2).strip():
                fence = None
            continue
        m = _FENCE_RE.match(body)
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence = (m.group(1)[0], len(m.group(1)))
            barrier[i] = True
            continue
        if _BLANK_RE.fullmatch(line):
            barrier[i] = True
            continue
        if skip_line is not None:
            try:
                if skip_line(line):
                    barrier[i] = True
            except Exception:  # noqa: BLE001 — 判定の失敗は「触らない」側へ
                barrier[i] = True
    return barrier


def _backslashes_before(s: str, i: int) -> int:
    n = 0
    while i - 1 - n >= 0 and s[i - 1 - n] == "\\":
        n += 1
    return n


def _split_prefix(prefix: str) -> tuple[str, str, bool] | None:
    """開きの前の行頭部分を (Q, M か I, M か) に分ける。接頭辞だけでなければ None。"""
    q = _QUOTE_RE.match(prefix).group(0)
    rest = prefix[len(q):]
    if q and rest.startswith(" "):
        q += " "
        rest = rest[1:]
    if _MARKER_RE.fullmatch(rest):
        return q, rest, True
    if rest.strip(" \t") == "":
        return q, rest, False
    return None


class _Scanner:
    def __init__(self, text: str, starts: list[int], first: int, last: int):
        self.s = text            # 1 ブロック（壁で区切られた連続する行）の文字列
        self.starts = starts     # ブロック内の各行の開始位置（ブロック内の相対位置）
        self.first = first       # ブロックの先頭行の、全体での行番号
        self.last = last

    def line_of(self, pos: int) -> int:
        lo, hi = 0, len(self.starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def line_end(self, li: int) -> int:
        """行 li の改行を除いた終わり。"""
        end = self.starts[li + 1] if li + 1 < len(self.starts) else len(self.s)
        while end > self.starts[li] and self.s[end - 1] in "\r\n":
            end -= 1
        return end


def _find_unescaped(s: str, needle: str, start: int) -> int:
    i = start
    while True:
        j = s.find(needle, i)
        if j < 0:
            return -1
        if _backslashes_before(s, j) % 2 == 0:
            return j
        i = j + 1


def _find_closer_backslash(s: str, ch: str, start: int) -> int:
    r"""``\)`` / ``\]`` の閉じ（その ``\`` の前のバックスラッシュが偶数）。"""
    i = start
    while True:
        j = s.find("\\" + ch, i)
        if j < 0:
            return -1
        if _backslashes_before(s, j) % 2 == 0:
            return j
        i = j + 1


def _find_inline_closer(s: str, start: int) -> int:
    i = start
    while True:
        j = s.find("$", i)
        if j < 0:
            return -1
        if _backslashes_before(s, j) % 2 == 1:
            i = j + 1
            continue
        prev = s[j - 1] if j > 0 else ""
        nxt = s[j + 1] if j + 1 < len(s) else ""
        if j > start and prev and not prev.isspace() and not nxt.isdigit():
            return j
        i = j + 1


def _skip_code_span(s: str, i: int) -> int:
    """i から始まるバッククォートの並びの後ろ（閉じがあればそのさらに後ろ）。"""
    k = i
    while k < len(s) and s[k] == "`":
        k += 1
    run = k - i
    j = k
    while True:
        j = s.find("`" * run, j)
        if j < 0:
            return k                       # 閉じが無い → 文字どおりのバッククォート
        e = j + run
        if (j == 0 or s[j - 1] != "`") and (e >= len(s) or s[e] != "`"):
            return e
        while j < len(s) and s[j] == "`":
            j += 1


def _skip_link_dest(s: str, i: int) -> int:
    """i は ``](`` の ``(`` の位置。対応する ``)`` の後ろ（同じ行の中）。無ければ i+1。"""
    depth = 0
    j = i
    while j < len(s) and s[j] not in "\r\n":
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return i + 1


def _strip_continuation(tex: str, q: str, width: int) -> str:
    lines = tex.split("\n")
    out = [lines[0]]
    for ln in lines[1:]:
        if q and ln.startswith(q.rstrip()):
            ln = ln[len(q):] if ln.startswith(q) else ln[len(q.rstrip()):]
        elif q:
            ln = ln[_QUOTE_RE.match(ln).end():]
        n = 0
        while n < width and n < len(ln) and ln[n] in " \t":
            n += 1
        out.append(ln[n:])
    return "\n".join(out)


def split_math(md: str, *, skip_line: Callable[[str], bool] | None = None
               ) -> tuple[str, list[MathSpan]]:
    """md の数式を画像の参照に置き換えた Markdown と、切り出した数式の一覧を返す。"""
    if "$" not in md and "\\" not in md:
        return md, []
    lines = md.splitlines(keepends=True)
    barrier = _line_kinds(lines, skip_line)
    offsets = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln)
    out: list[str] = []
    spans: list[MathSpan] = []
    seen: set[str] = set()
    i = 0
    while i < len(lines):
        if barrier[i]:
            out.append(lines[i])
            i += 1
            continue
        j = i
        while j + 1 < len(lines) and not barrier[j + 1]:
            j += 1
        block = "".join(lines[i:j + 1])
        starts = [offsets[k] - offsets[i] for k in range(i, j + 1)]
        out.append(_scan_block(_Scanner(block, starts, i, j), spans, seen))
        i = j + 1
    return "".join(out), spans


def _scan_block(sc: _Scanner, spans: list[MathSpan], seen: set[str]) -> str:
    s = sc.s
    out: list[str] = []
    last = 0          # s[last:] はまだ out に写していない
    p = 0

    def add(tex: str, display: bool) -> str:
        key = math_key(tex, display)
        if key not in seen:
            seen.add(key)
            spans.append(MathSpan(key, tex, display))
        return key

    while p < len(s):
        c = s[p]
        if c == "`":
            p = _skip_code_span(s, p)
            continue
        if c == "]" and s.startswith("](", p):
            p = _skip_link_dest(s, p + 1)
            continue
        if c == "<":
            m = _AUTOLINK_RE.match(s, p)
            p = m.end() if m else p + 1
            continue
        found = None    # (open_start, content_start, content_end, close_end, display, is_env)
        if c == "\\":
            k = p
            while k < len(s) and s[k] == "\\":
                k += 1
            n = k - p
            nxt = s[k] if k < len(s) else ""
            if n % 2 == 1 and nxt in "([":
                o = k - 1                      # 区切りの "\" の位置
                closer = ")" if nxt == "(" else "]"
                e = _find_closer_backslash(s, closer, k + 1)
                if e >= 0 and s[k + 1:e].strip():
                    found = (o, k + 1, e, e + 2, nxt == "[", False)
            elif n == 1 and nxt == "b":
                li = sc.line_of(p)
                m = _ENV_RE.match(s, p)
                if m and _split_prefix(s[sc.starts[li]:p]) is not None:
                    end_tok = "\\end{" + m.group(1) + m.group(2) + "}"
                    e = s.find(end_tok, m.end())
                    if e >= 0:
                        found = (p, p, e + len(end_tok), e + len(end_tok), True, True)
            if found is None:
                p = k if not (n % 2 == 1 and nxt == "$") else k + 1
                continue
        elif c == "$":
            if s.startswith("$$", p):
                e = _find_unescaped(s, "$$", p + 2)
                if e >= 0 and s[p + 2:e].strip():
                    found = (p, p + 2, e, e + 2, True, False)
                else:
                    p += 2
                    continue
            else:
                nxt = s[p + 1] if p + 1 < len(s) else ""
                if nxt and not nxt.isspace():
                    e = _find_inline_closer(s, p + 1)
                    if e >= 0:
                        found = (p, p + 1, e, e + 1, False, False)
                if found is None:
                    p += 1
                    continue
        if found is None:
            p += 1
            continue
        o, cs, ce, close_end, display, is_env = found
        raw = s[o:close_end] if is_env else s[cs:ce]
        li_open = sc.line_of(o)
        li_close = sc.line_of(max(close_end - 1, o))
        prefix = s[sc.starts[li_open]:o]
        tail = s[close_end:sc.line_end(li_close)]
        whole = display and not tail.strip() and _split_prefix(prefix) is not None
        if whole:
            q, mi, has_marker = _split_prefix(prefix)
            tex = _strip_continuation(raw.replace("\r\n", "\n"), q, len(mi)).strip()
        else:
            tex = raw.replace("\r\n", "\n").strip()
        if not tex:
            p = close_end
            continue
        key = add(tex, display)
        if whole:
            line_start = sc.starts[li_open]
            out.append(s[last:line_start])
            img = f"![m]({MATH_SCHEME}d/{key})"
            if has_marker:
                out.append(q + mi + img)
            else:
                pad = (q + mi).rstrip()
                out.append(f"{pad}\n{q}{mi}{img}\n{pad}")
            last = sc.line_end(li_close)
            p = last
        else:
            out.append(s[last:o])
            out.append(f"![m]({MATH_SCHEME}{'d' if display else 'i'}/{key})")
            last = close_end
            p = close_end
    out.append(s[last:])
    return "".join(out)
