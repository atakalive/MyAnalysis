"""チャットの数式の TeX → SVG 変換エンジン（MathJax 4 を QtWebEngine で動かす。Issue #123）。

- 単一インスタンス（``engine()``）。``QApplication`` の属性に持たせ、取り出したインスタンスのクラスが
  今のクラスと違えば（Tier 3 でモジュールが差し替わった後）古い方を ``stop()`` して作り直す。
- 数式描画モジュールが有効かつ導入済みのとき、最初に数式を求められた時点で page を作る（数式の無い
  使い方では Chromium のプロセスを起こさない）。QtWebEngine の import もそのときまで遅らせる
  （``_load_webengine``）。
- 保存しないプロファイル上の画面の無い ``QWebEnginePage`` が ``gui/assets/mathjax_host.html`` を読む。
  インターセプタは host の HTML と導入先の版のディレクトリの配下の ``file:`` 以外を拒む。
- ``request(tex, display)`` は ``MathResult`` を返す（ready / pending / error）。pending だった式が
  ready か error に変わったら ``rendered`` を出す。
"""
from __future__ import annotations

import importlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PySide6.QtCore import QObject, QTimer, QUrl, QUrlQuery, Signal
from PySide6.QtWidgets import QApplication

from common import mathjax_install

_logger = logging.getLogger(__name__)

MathKey = tuple[str, str, bool]          # (font, tex, display)

MAX_TEX = 4000                           # これを超える TeX は送らない
READY_TIMEOUT_S = 20.0
READY_POLL_MS = 100
BATCH_POLL_MS = 50
BATCH_TIMEOUT_S = 10.0
SVG_CACHE_BYTES = 32 * 1024 * 1024
_BATCH_MAX = 64

HOST_HTML = Path(__file__).resolve().parent / "assets" / "mathjax_host.html"
_APP_ATTR = "_myanalysis_math_engine"


@dataclass(frozen=True)
class MathResult:
    status: Literal["ready", "pending", "error"]
    svg: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class MathPrefs:
    enabled: bool = False
    font: str = mathjax_install.DEFAULT_FONT
    scale: float = mathjax_install.DEFAULT_SCALE
    bold: bool = mathjax_install.DEFAULT_BOLD


def load_prefs() -> MathPrefs:
    """ui_prefs の math_render / math_font / math_scale / math_bold。知らない値は既定に戻す。"""
    from llm_bridge.paths import read_ui_pref
    enabled = read_ui_pref("math_render", False)
    font = read_ui_pref("math_font", mathjax_install.DEFAULT_FONT)
    scale = read_ui_pref("math_scale", mathjax_install.DEFAULT_SCALE)
    bold = read_ui_pref("math_bold", mathjax_install.DEFAULT_BOLD)
    if not isinstance(enabled, bool):
        enabled = False
    if font not in mathjax_install.FONTS:
        font = mathjax_install.DEFAULT_FONT
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) \
            or not any(abs(float(scale) - s) < 1e-9 for s in mathjax_install.SCALES):
        scale = mathjax_install.DEFAULT_SCALE
    if not isinstance(bold, bool):
        bold = mathjax_install.DEFAULT_BOLD
    return MathPrefs(enabled, font, float(scale), bold)


def _load_webengine():
    """QtWebEngine を読む（page を作る直前だけ）。テストが ImportError を投げる関数に差し替える。"""
    return importlib.import_module("PySide6.QtWebEngineCore")


def is_allowed_url(url: str, *, host: Path, root: Path) -> bool:
    """host の HTML そのもの（クエリ・断片は無視）と、root の配下の file: URL だけを許す。"""
    q = QUrl(url)
    if q.scheme() != "file" or not q.isLocalFile():
        return False
    local = q.toLocalFile()
    if not local:
        return False
    try:
        p = Path(local).resolve()
        if p == Path(host).resolve():
            return True
        r = Path(root).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return p == r or r in p.parents


_classes: dict[str, type] = {}


def _webengine_classes(mod):
    """QtWebEngine の型に依存する派生クラスを、読み込んだ後に作る。"""
    if "page" in _classes:
        return _classes["interceptor"], _classes["page"]

    class _Interceptor(mod.QWebEngineUrlRequestInterceptor):
        def __init__(self, host: Path, root: Path, engine: "MathEngine"):
            super().__init__()
            self._host, self._root, self._engine = host, root, engine

        def interceptRequest(self, info):  # noqa: N802 — Qt の名前
            url = info.requestUrl().toString()
            if not is_allowed_url(url, host=self._host, root=self._root):
                info.block(True)
                _logger.warning("math engine: blocked %s", url)
                self._engine.rejected.append(url)

    class _Page(mod.QWebEnginePage):
        def __init__(self, profile, engine: "MathEngine"):
            super().__init__(profile)
            self._engine = engine

        def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
            lv = mod.QWebEnginePage.JavaScriptConsoleMessageLevel
            if level == lv.ErrorMessageLevel:
                _logger.warning("math engine console error: %s (%s:%s)", message, source, line)
                self._engine.console_errors.append(message)
            elif level == lv.WarningMessageLevel:
                _logger.warning("math engine console warning: %s (%s:%s)", message, source, line)

    _classes["interceptor"], _classes["page"] = _Interceptor, _Page
    return _Interceptor, _Page


class MathEngine(QObject):
    rendered = Signal()
    stateChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.prefs = load_prefs()
        self._cache: OrderedDict[MathKey, tuple[str, str]] = OrderedDict()   # key → ("svg"|"error", 値)
        self._cache_bytes = 0
        self._queue: list[MathKey] = []
        self._queued: set[MathKey] = set()
        self._batch: dict | None = None
        self._batch_seq = 0
        self._state = "idle"          # idle / starting / ready / failed / unavailable
        self.last_error: str | None = None
        self._page = None
        self._profile = None
        self._interceptor = None
        self._use = None
        self._gen = 0                 # page の世代（古いコールバックを捨てる）
        self._ready_deadline = 0.0
        self.rejected: list[str] = []
        self.console_errors: list[str] = []
        self._ready_timer = QTimer(self)
        self._ready_timer.setInterval(READY_POLL_MS)
        self._ready_timer.timeout.connect(self._poll_ready)
        self._batch_timer = QTimer(self)
        self._batch_timer.setInterval(BATCH_POLL_MS)
        self._batch_timer.timeout.connect(self._poll_batch)

    # ---------------------------------------------------------------- 状態

    @property
    def state(self) -> str:
        return self._state

    @property
    def running(self) -> bool:
        return self._page is not None

    def reload_prefs(self) -> MathPrefs:
        """ui_prefs を読み直す。有効・フォントが変われば止めて失敗の記録を消す。無効にしたらキャッシュを捨てる。"""
        old = self.prefs
        self.prefs = load_prefs()
        if (old.enabled, old.font) != (self.prefs.enabled, self.prefs.font):
            self.stop()
            self.reset()
        if old.enabled and not self.prefs.enabled:
            self.clear_cache()
        return self.prefs

    def reset(self) -> None:
        """「使えない」の記録を消し、次の request から起動を試す（設定から有効にし直したとき）。"""
        if self._page is None:
            self._state = "idle"
            self.last_error = None
            self.stateChanged.emit()

    def webengine_ok(self) -> bool:
        """QtWebEngine が読めるか（page は作らない）。読めなければ「使えない」状態にする。"""
        if self._state == "unavailable":
            return False
        try:
            _load_webengine()
        except (ImportError, OSError) as e:
            self._fail(f"QtWebEngine: {type(e).__name__}: {e}", state="unavailable")
            return False
        return True

    def clear_cache(self) -> None:
        self._cache.clear()
        self._cache_bytes = 0

    # ---------------------------------------------------------------- 変換

    def request(self, tex: str, display: bool, *, force: bool = False) -> MathResult:
        """force は設定のダイアログのプレビュー用（描画が無効でも、導入済みなら変換する）。"""
        prefs = self.prefs
        key: MathKey = (prefs.font, tex, bool(display))
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            kind, val = hit
            return MathResult("ready", svg=val) if kind == "svg" else MathResult("error", error=val)
        if (not prefs.enabled and not force) or self._state in ("failed", "unavailable"):
            return MathResult("error", error="unavailable")
        if len(tex) > MAX_TEX:
            self._put(key, "error", "too_long")
            return MathResult("error", error="too_long")
        if key not in self._queued and not self._in_batch(key):
            self._queue.append(key)
            self._queued.add(key)
        if self._page is None:
            self._start()
            if self._state in ("failed", "unavailable"):
                return MathResult("error", error="unavailable")
        else:
            self._dispatch()
        return MathResult("pending")

    def is_pending(self, key: MathKey) -> bool:
        return key in self._queued or self._in_batch(key)

    def _in_batch(self, key: MathKey) -> bool:
        b = self._batch
        return b is not None and key in b["items"][b["received"]:]

    def _put(self, key: MathKey, kind: str, value: str) -> None:
        old = self._cache.pop(key, None)
        if old is not None:
            self._cache_bytes -= len(old[1]) + len(key[1])
        self._cache[key] = (kind, value)
        self._cache_bytes += len(value) + len(key[1])
        while self._cache_bytes > SVG_CACHE_BYTES and len(self._cache) > 1:
            k, (_, v) = self._cache.popitem(last=False)
            self._cache_bytes -= len(v) + len(k[1])

    # ---------------------------------------------------------------- page

    def _start(self) -> None:
        if self._page is not None or self._state in ("failed", "unavailable"):
            return
        try:
            mod = _load_webengine()
        except (ImportError, OSError) as e:
            self._fail(f"QtWebEngine: {type(e).__name__}: {e}", state="unavailable")
            return
        font = self.prefs.font
        use = mathjax_install.acquire_use(mathjax_install.VERSION, font)
        if use is None:
            self._fail("not installed")
            return
        try:
            root = mathjax_install.version_dir()
            Interceptor, Page = _webengine_classes(mod)
            profile = mod.QWebEngineProfile()             # 保存しないプロファイル
            interceptor = Interceptor(HOST_HTML, root, self)
            profile.setUrlRequestInterceptor(interceptor)
            page = Page(profile, self)
        except Exception as e:  # noqa: BLE001 — 起動の失敗は「使えない」
            use.release()
            self._fail(f"{type(e).__name__}: {e}")
            return
        self._use, self._profile, self._interceptor, self._page = use, profile, interceptor, page
        self._gen += 1
        self._state = "starting"
        url = QUrl.fromLocalFile(str(HOST_HTML))
        q = QUrlQuery()
        q.addQueryItem("root", QUrl.fromLocalFile(str(root)).toString())
        q.addQueryItem("font", font)
        q.addQueryItem("minScale", str(mathjax_install.MIN_SCALE))
        url.setQuery(q)
        page.load(url)
        self._ready_deadline = time.monotonic() + READY_TIMEOUT_S
        self._ready_timer.start()
        self.stateChanged.emit()

    def _js(self, code: str, callback) -> None:
        page = self._page
        if page is None:
            return
        gen = self._gen

        def cb(result):
            if gen != self._gen or self._page is None:
                return
            callback(result)
        page.runJavaScript(code, 0, cb)

    def _poll_ready(self) -> None:
        if self._page is None:
            self._ready_timer.stop()
            return
        if time.monotonic() > self._ready_deadline:
            self._ready_timer.stop()
            self._fail(f"not ready in {READY_TIMEOUT_S:.0f} s")
            QTimer.singleShot(0, self.stop)
            return
        self._js("JSON.stringify([window.mjReady === true, window.mjFailed || null])", self._on_ready_probe)

    def _on_ready_probe(self, result) -> None:
        try:
            ready, failed = json.loads(result) if isinstance(result, str) else (False, None)
        except (ValueError, TypeError):
            ready, failed = False, None
        if self._state != "starting":
            return
        if failed:
            self._ready_timer.stop()
            self._fail(str(failed))
            QTimer.singleShot(0, self.stop)
            return
        if ready:
            self._ready_timer.stop()
            self._state = "ready"
            self.stateChanged.emit()
            self._dispatch()

    def _fail(self, message: str, *, state: str = "failed") -> None:
        """起動の失敗を確定する。pending だった式はすべて unavailable の扱い（rendered を 1 回出す）。"""
        _logger.warning("math engine unavailable: %s", message)
        self._state = state
        self.last_error = message
        had = bool(self._queue) or self._batch is not None
        self._queue.clear()
        self._queued.clear()
        self._batch = None
        self._batch_timer.stop()
        self.stateChanged.emit()
        if had:
            self.rendered.emit()

    def _dispatch(self) -> None:
        if self._state != "ready" or self._batch is not None or not self._queue:
            return
        items, self._queue = self._queue[:_BATCH_MAX], self._queue[_BATCH_MAX:]
        for k in items:
            self._queued.discard(k)
        self._batch_seq += 1
        bid = self._batch_seq
        self._batch = {"id": bid, "items": items, "received": 0, "started": time.monotonic()}
        payload = json.dumps([{"tex": k[1], "display": k[2]} for k in items])
        self._js(f"startBatch({bid}, {payload})", lambda _r: None)
        self._batch_timer.start()

    def _poll_batch(self) -> None:
        b = self._batch
        if b is None or self._page is None:
            self._batch_timer.stop()
            return
        if time.monotonic() - b["started"] > BATCH_TIMEOUT_S:
            self._timeout_batch()
            return
        bid = b["id"]
        self._js(f"takeBatch({bid})", lambda r: self._on_batch(bid, r))

    def _on_batch(self, bid: int, result) -> None:
        b = self._batch
        if b is None or b["id"] != bid:
            return
        try:
            data = json.loads(result) if isinstance(result, str) else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return
        got = False
        for item in data.get("items") or []:
            if b["received"] >= len(b["items"]):
                break
            key = b["items"][b["received"]]
            b["received"] += 1
            svg = item.get("svg") if isinstance(item, dict) else None
            if isinstance(svg, str) and svg:
                self._put(key, "svg", svg)
            else:
                err = item.get("error") if isinstance(item, dict) else None
                self._put(key, "error", str(err or "error"))
            got = True
        if data.get("done") or b["received"] >= len(b["items"]):
            # 届かなかった分（done なのに足りない）は待ち行列へ戻す
            rest = b["items"][b["received"]:]
            self._batch = None
            self._batch_timer.stop()
            for k in reversed(rest):
                if k not in self._cache and k not in self._queued:
                    self._queue.insert(0, k)
                    self._queued.add(k)
            self._dispatch()
        if got:
            self.rendered.emit()

    def _timeout_batch(self) -> None:
        """処理中だった 1 件を timeout にし、残りを待ち行列に戻して page を作り直す。"""
        b = self._batch
        self._batch = None
        self._batch_timer.stop()
        if b is None:
            return
        rest = b["items"][b["received"]:]
        if rest:
            self._put(rest[0], "error", "timeout")
            for k in reversed(rest[1:]):
                if k not in self._queued:
                    self._queue.insert(0, k)
                    self._queued.add(k)
        self.rendered.emit()
        QTimer.singleShot(0, self._restart_page)

    def _restart_page(self) -> None:
        queue, queued = list(self._queue), set(self._queued)
        self.stop()
        self._queue, self._queued = queue, queued
        if self._queue:
            self._start()

    # ---------------------------------------------------------------- 後始末

    def stop(self) -> None:
        """page と profile をその場で消し、使用中の印を外す。何度呼んでもよい。

        page のシグナル・runJavaScript のコールバックの中から直接呼ばない（QTimer.singleShot(0, …)）。"""
        self._ready_timer.stop()
        self._batch_timer.stop()
        page, profile = self._page, self._profile
        interceptor, use = self._interceptor, self._use
        self._page = self._profile = self._interceptor = self._use = None
        self._gen += 1
        self._batch = None
        self._queue.clear()
        self._queued.clear()
        if self._state in ("starting", "ready"):
            self._state = "idle"
        if page is not None or profile is not None:
            import shiboken6
            if page is not None:
                try:
                    page.disconnect()
                except (RuntimeError, TypeError):
                    pass
                if shiboken6.isValid(page):
                    shiboken6.delete(page)
            if profile is not None and shiboken6.isValid(profile):
                shiboken6.delete(profile)
            if interceptor is not None and shiboken6.isValid(interceptor):
                shiboken6.delete(interceptor)
        if use is not None:
            use.release()
        if page is not None:
            self.stateChanged.emit()


def engine() -> MathEngine:
    """単一インスタンス（QApplication の属性）。クラスが違えば古い方を止めて作り直す。"""
    app = QApplication.instance()
    old = getattr(app, _APP_ATTR, None) if app is not None else None
    if old is not None and type(old) is MathEngine:
        return old
    if old is not None:
        try:
            old.stop()
        except Exception:  # noqa: BLE001 — 古いクラスの後始末の失敗で新しい方を止めない
            pass
    eng = MathEngine()
    if app is not None:
        setattr(app, _APP_ATTR, eng)
        app.aboutToQuit.connect(eng.stop)
    return eng


def current() -> MathEngine | None:
    """作られていれば今のインスタンス（作らない）。"""
    app = QApplication.instance()
    eng = getattr(app, _APP_ATTR, None) if app is not None else None
    return eng if type(eng) is MathEngine else None
