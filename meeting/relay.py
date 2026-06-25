"""Host-side meeting relay client (Issue #42).

``MeetingRelay`` (QObject, GUI thread) owns the meeting lifecycle, the GUI-thread
capture timer (tab.grab → view PNG, session/tab snapshots), and the echo/echo-gate
logic. Continuous network I/O (poll/POST/PUT) runs in ``_RelayWorker`` (QThread);
GUI ⇄ worker communication is Qt queued signals + a mutex-guarded outbox deque.

THREAD SAFETY (critical):
  - tab.grab(), window.tab_names(), window.tabs(), and ChatWidget reads/writes are
    GUI-thread ONLY. Touching a QWidget from another thread segfaults.
  - The capture QTimer runs on the GUI thread: it grabs the view, hashes it, and
    pushes bytes to the worker outbox. The worker NEVER calls tab.grab().
  - EVERY urllib call uses an explicit timeout=5.0 (no timeout → infinite block →
    the worker can't stop → forced thread kill → segfault). Stop is requestInterruption()
    + wait(); the thread is never force-killed.

CURSOR / LOOKBACK (loss-free, bounded): cursors are built from the WORKER clock
(server_now_ms) only — never local time. Loss-free invariant:
MAX_BUCKETS*60000 >= LOOKBACK_MS + max_poll_interval (120000 >= 15000 + 30000);
poll interval is clamped [1s, 30s].
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from threading import Lock

from PySide6.QtCore import (
    QBuffer, QByteArray, QIODevice, QObject, QThread, QTimer, Qt, Signal,
)
from PySide6.QtGui import QImage

from common.i18n import tr

_LOOKBACK_MS = 15000
_REQ_TIMEOUT = 5.0
# Cloudflare's edge bot protection (error 1010) rejects the default
# "Python-urllib/x" UA with a 403 before the request reaches the Worker. Send a
# browser-like UA so host requests pass the signature check (guests use a real
# browser and are unaffected).
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_HEARTBEAT_SEC = 20.0
_PRESENCE_POLL_SEC = 5.0
_FAIL_THRESHOLD = 5
_VIEW_MAX_EDGE = 1920


def _q(s: str) -> str:
    """encodeURIComponent-equivalent path segment quoting."""
    return urllib.parse.quote(str(s), safe="")


def _qimage_digest(img: "QImage") -> bytes | None:
    """sha1 of an RGBA8888 QImage's raw bytes, or None if constBits() fails.

    `constBits()` can return an int (not a sip buffer) on some PySide builds, in
    which case `memoryview(...)` raises — caller falls back to hashing PNG bytes.
    """
    try:
        mv = memoryview(img.constBits()).cast("B")[:img.sizeInBytes()]
        return hashlib.sha1(mv).digest()
    except Exception:
        return None


class _RelayWorker(QThread):
    """Continuous network I/O for one channel. All urllib has timeout=5.0."""

    sig_inbound = Signal(str, str, str)   # (sid, name, text)  — from in: only
    sig_presence = Signal(object)         # list[{pid,name,sid,tab,last}]
    sig_state = Signal(str)               # "expired" | "disconnected" | "ok"

    def __init__(self, base_url: str, admin_key: str, channel: str,
                 poll_ms: int = 3000, parent=None):
        super().__init__(parent)
        self._base = base_url.rstrip("/")
        self._admin = admin_key
        self._ch = channel
        self._poll_ms = poll_ms
        self._outbox: deque = deque()
        self._lock = Lock()
        self._latest_seen_ts = 0       # worker-clock cursor (ms)
        self._reanchor = True          # first poll omits `since`
        self._seen: set[str] = set()
        self._fail_streak = 0
        self._disconnected = False
        self._last_hb = 0.0
        self._last_presence = 0.0

    # ---- public (GUI thread) ----

    def enqueue(self, item: dict) -> None:
        with self._lock:
            self._outbox.append(item)

    # ---- run loop ----

    def run(self) -> None:
        while not self.isInterruptionRequested():
            self._drain_outbox()
            self._do_inbound()
            self._maybe_heartbeat()
            self._maybe_presence()
            self._sleep_interruptible(self._poll_ms)

    def _sleep_interruptible(self, ms: int) -> None:
        ms = max(1000, min(30000, int(ms)))   # clamp [1s, 30s] (loss-free invariant)
        waited = 0
        while waited < ms and not self.isInterruptionRequested():
            self.msleep(100)
            waited += 100

    # ---- HTTP ----

    def _req(self, method: str, path: str, data=None, is_png: bool = False):
        url = self._base + path
        headers = {"Authorization": "Bearer " + self._admin, "User-Agent": _UA}
        body = None
        if is_png:
            headers["Content-Type"] = "image/png"
            body = data
        elif data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        return urllib.request.urlopen(req, timeout=_REQ_TIMEOUT)

    def _note_ok(self) -> None:
        self._fail_streak = 0
        if self._disconnected:
            self._disconnected = False
            self.sig_state.emit("ok")

    def _note_fail(self) -> None:
        self._fail_streak += 1
        if self._fail_streak >= _FAIL_THRESHOLD and not self._disconnected:
            self._disconnected = True
            self.sig_state.emit("disconnected")

    def _emit_expired(self) -> None:
        self.sig_state.emit("expired")
        self.requestInterruption()

    def _drain_outbox(self) -> None:
        while not self.isInterruptionRequested():
            with self._lock:
                if not self._outbox:
                    return
                item = self._outbox.popleft()
            self._send(item)

    def _send(self, item: dict) -> None:
        kind = item.get("kind")
        try:
            if kind == "out":
                resp = self._req("POST", f"/out/{_q(self._ch)}/{_q(item['sid'])}", data=item["body"])
            elif kind == "sessions":
                resp = self._req("PUT", f"/sessions/{_q(self._ch)}", data=item["data"])
            elif kind == "tabs":
                resp = self._req("PUT", f"/tabs/{_q(self._ch)}", data=item["data"])
            elif kind == "view":
                resp = self._req("PUT", f"/view/{_q(self._ch)}/{_q(item['tab'])}",
                                 data=item["png"], is_png=True)
            else:
                return
            # Drain the body and close: release the socket/FD promptly (1s capture
            # cadence) and let urllib reuse the connection (Keep-Alive). reviewer R1.
            with resp:
                resp.read()
            self._note_ok()
        except urllib.error.HTTPError as e:
            if e.code in (401, 410):
                self._emit_expired()
            else:
                self._note_fail()
        except Exception:
            self._note_fail()

    def _do_inbound(self) -> None:
        try:
            if self._reanchor:
                res = self._req("GET", f"/inbound/{_q(self._ch)}")
            else:
                floor = max(0, self._latest_seen_ts - _LOOKBACK_MS)
                since = f"{floor:013d}-{'0' * 13}"
                res = self._req("GET", f"/inbound/{_q(self._ch)}?since={_q(since)}")
            with res:
                payload = json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 410):
                self._emit_expired()
            else:
                self._note_fail()
            return     # cursor not advanced on failure (re-fetched next poll)
        except Exception:
            self._note_fail()
            return
        self._note_ok()
        server_now = int(payload.get("server_now_ms", 0) or 0)
        if self._reanchor:
            # Adopt the worker clock — never local time (clock-skew safe).
            self._latest_seen_ts = server_now
            self._reanchor = False
        for m in payload.get("messages", []):
            mid = m.get("mid")
            if not mid or mid in self._seen:
                continue
            self._seen.add(mid)
            self.sig_inbound.emit(str(m.get("sid", "")), str(m.get("name", "")),
                                  str(m.get("text", "")))
            ts = self._mid_ts(mid)
            if ts > self._latest_seen_ts:
                self._latest_seen_ts = ts
        if server_now > self._latest_seen_ts:
            self._latest_seen_ts = server_now
        cutoff = self._latest_seen_ts - 2 * _LOOKBACK_MS
        self._seen = {x for x in self._seen if self._mid_ts(x) >= cutoff}

    @staticmethod
    def _mid_ts(mid: str) -> int:
        head = mid[:13]
        return int(head) if head.isdigit() else 0

    def _maybe_heartbeat(self) -> None:
        now = time.monotonic()
        if now - self._last_hb < _HEARTBEAT_SEC:
            return
        self._last_hb = now
        try:
            # read()+close via context manager: release FD + allow Keep-Alive. reviewer R1.
            with self._req("PUT", f"/heartbeat/{_q(self._ch)}") as r:
                r.read()
            self._note_ok()
        except urllib.error.HTTPError as e:
            if e.code in (401, 410):
                self._emit_expired()
            else:
                self._note_fail()
        except Exception:
            self._note_fail()

    def _maybe_presence(self) -> None:
        now = time.monotonic()
        if now - self._last_presence < _PRESENCE_POLL_SEC:
            return
        self._last_presence = now
        try:
            res = self._req("GET", f"/presence/{_q(self._ch)}")
            with res:
                data = json.loads(res.read().decode("utf-8"))
            if isinstance(data, list):
                self.sig_presence.emit(data)
            self._note_ok()
        except Exception:
            self._note_fail()


class MeetingRelay(QObject):
    """Meeting lifecycle + capture timer + echo gates (GUI thread)."""

    channelStateChanged = Signal(str)
    participantsUpdated = Signal(object)
    remoteMessageReceived = Signal(str, str, str)

    def __init__(self, window, parent=None):
        # Parent to a QObject for lifetime management. In production `window` is
        # the ToolWindow (a QObject); under tests it may be a plain mock, in
        # which case we stay unparented rather than crash QObject.__init__.
        if parent is None and isinstance(window, QObject):
            parent = window
        super().__init__(parent)
        self._window = window
        self._worker: _RelayWorker | None = None
        self._sharing = False
        self._channel: str | None = None
        self._secret: str | None = None
        self._expires_at = 0
        self._poll_ms = 3000
        self._host_name = tr("meeting.host_name_default")
        self._published_session_ids: set[str] = set()
        self._meeting_start_ids: set[str] = set()
        self._published_tabs: set[str] = set()
        self._participants: list = []
        self._last_sessions_json: str | None = None
        self._last_tabs_json: str | None = None
        self._view_hashes: dict[str, bytes] = {}
        self._base_url = os.environ.get("RELAY_BASE_URL", "") or ""
        self._admin_key = os.environ.get("RELAY_ADMIN_KEY", "") or ""

        cw = window.chat_widget() if hasattr(window, "chat_widget") else None
        if cw is not None and hasattr(cw, "messageAdded"):
            cw.messageAdded.connect(self._on_message_added)

        self._capture_timer = QTimer(self)
        self._capture_timer.setInterval(1000)
        self._capture_timer.timeout.connect(self._on_capture_tick)

    # ---- config / accessors ----

    def is_configured(self) -> bool:
        return bool(self._base_url and self._admin_key)

    def expires_at(self) -> int:
        return self._expires_at

    @property
    def host_name(self) -> str:
        return self._host_name

    def set_host_name(self, name: str) -> None:
        self._host_name = (name or "").strip() or tr("meeting.host_name_default")

    def is_sharing(self) -> bool:
        return self._sharing

    def channel(self) -> str | None:
        return self._channel

    def base_url(self) -> str:
        return self._base_url

    def participants(self) -> list:
        return list(self._participants)

    def published_session_ids(self) -> set[str]:
        return set(self._published_session_ids)

    def published_tabs(self) -> set[str]:
        return set(self._published_tabs)

    def meeting_start_ids(self) -> set[str]:
        return set(self._meeting_start_ids)

    def set_poll_interval(self, seconds: float) -> None:
        self._poll_ms = int(max(1, min(30, seconds)) * 1000)
        if self._worker is not None:
            self._worker._poll_ms = self._poll_ms

    def set_published_sessions(self, ids) -> None:
        new = set(ids)
        removed = self._published_session_ids - new
        self._published_session_ids = new
        # Drop any pending remote turns queued for sessions just opted out, so a
        # later FIFO drain can't inject into a now-private session.
        if removed:
            cw = self._window.chat_widget() if hasattr(self._window, "chat_widget") else None
            pend = getattr(cw, "_pending_remote", None)
            if isinstance(pend, dict):
                for sid in removed:
                    pend.pop(sid, None)

    def set_published_tabs(self, names) -> None:
        self._published_tabs = set(names)

    # ---- lifecycle ----

    def meeting_start(self, ttl_sec: int) -> str:
        """Create the channel, start the worker + capture timer, return a token.

        The default published set is a SNAPSHOT of ALL sessions (every dataset)
        at start time. This set is NOT recomputed on dataset switch, so in-flight
        replies keep flowing even after the host moves to another dataset, and
        sessions can still be deselected per-session from the share window.
        """
        ttl = max(3600, min(86400, int(ttl_sec)))
        ch = secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        secret_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()

        cw = self._window.chat_widget()
        summaries = cw.session_summaries() if cw is not None else []
        self._published_session_ids = {s["id"] for s in summaries}
        self._meeting_start_ids = set(self._published_session_ids)
        self._published_tabs = set(self._window.tab_names())

        body = {"ch": ch, "ttl_sec": ttl, "secret_hash": secret_hash}
        req = urllib.request.Request(
            self._base_url.rstrip("/") + "/admin/channel",
            data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + self._admin_key,
                     "Content-Type": "application/json", "User-Agent": _UA},
        )
        with urllib.request.urlopen(req, timeout=_REQ_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self._expires_at = int(data.get("expires_at", 0) or 0)

        self._channel = ch
        self._secret = secret
        self._sharing = True
        self._last_sessions_json = None
        self._last_tabs_json = None
        self._view_hashes = {}

        self._worker = _RelayWorker(self._base_url, self._admin_key, ch, poll_ms=self._poll_ms)
        self._worker.sig_inbound.connect(self._on_remote_message)
        self._worker.sig_presence.connect(self._on_participants)
        self._worker.sig_state.connect(self._on_state)
        self._worker.start()
        self._capture_timer.start()
        # Push the initial snapshots immediately (don't wait for the first tick).
        self._on_capture_tick()

        token_obj = {"base_url": self._base_url, "channel": ch, "secret": secret}
        raw = base64.urlsafe_b64encode(
            json.dumps(token_obj).encode("utf-8")
        ).decode("ascii").rstrip("=")
        return raw

    def meeting_stop(self) -> None:
        ch = self._channel
        if ch and self._base_url and self._admin_key:
            try:
                req = urllib.request.Request(
                    self._base_url.rstrip("/") + "/admin/channel/" + _q(ch),
                    method="DELETE",
                    headers={"Authorization": "Bearer " + self._admin_key,
                             "User-Agent": _UA},
                )
                urllib.request.urlopen(req, timeout=_REQ_TIMEOUT).close()
            except Exception:
                pass    # best-effort; TTL + heartbeat-grace are the hard backstop
        self.stop()

    def stop(self) -> None:
        """Idempotent. requestInterruption() + wait(); never force-kills the thread."""
        self._sharing = False
        self._capture_timer.stop()
        w = self._worker
        if w is not None:
            w.requestInterruption()
            w.wait(8000)
            self._worker = None

    # ---- signal handlers (GUI thread) ----

    def _on_state(self, state: str) -> None:
        if state == "expired":
            self._sharing = False
            self._capture_timer.stop()
        self.channelStateChanged.emit(state)

    def _on_participants(self, data) -> None:
        self._participants = data if isinstance(data, list) else []
        self.participantsUpdated.emit(self._participants)

    def _on_remote_message(self, sid: str, name: str, text: str) -> None:
        # in: receive gate — symmetric to the out: send gate. KV is eventually
        # consistent, so a guest may POST to a just-opted-out sid within the
        # propagation window; drop it here so a private session never drives the
        # agent (tool-operation rights).
        if sid not in self._published_session_ids:
            return
        cw = self._window.chat_widget()
        if cw is not None:
            cw.inject_remote_message(text, name, session_id=sid)
        self.remoteMessageReceived.emit(sid, name, text)

    def _on_message_added(self, sid: str, role: str, content: str, origin: str) -> None:
        # out: send gate — publish ONLY host-local messages of a published
        # session. origin=="remote" (guest user re-emit) and non-published
        # sessions never reach out: (echo suppression + no TTL-window leak).
        if not (self._sharing and origin == "local" and sid in self._published_session_ids):
            return
        if not content or not content.strip():
            return
        name = tr("meeting.assistant_name_default") if role == "assistant" else self._host_name
        body = {"text": content, "name": name, "role": role}
        if self._worker is not None:
            self._worker.enqueue({"kind": "out", "sid": sid, "body": body})

    # ---- capture (GUI thread) ----

    def _on_capture_tick(self) -> None:
        if not self._sharing or self._worker is None:
            return
        win = self._window
        cw = win.chat_widget()

        # Sessions: publish only ids in (_published_session_ids ∩ existing) so a
        # deleted session drops out and dataset switch can't leak others.
        summaries = cw.session_summaries() if cw is not None else []
        existing = {s["id"] for s in summaries}
        self._published_session_ids &= existing
        pub = [
            {"id": s["id"], "title": s["title"], "busy": s["busy"]}
            for s in summaries if s["id"] in self._published_session_ids
        ]
        sj = json.dumps(pub, sort_keys=True, ensure_ascii=False)
        if sj != self._last_sessions_json:
            self._last_sessions_json = sj
            self._worker.enqueue({"kind": "sessions", "data": pub})

        # Tabs: published tabs only.
        all_tabs = win.tab_names()
        pub_tabs = [t for t in all_tabs if t in self._published_tabs]
        tj = json.dumps(pub_tabs, sort_keys=True, ensure_ascii=False)
        if tj != self._last_tabs_json:
            self._last_tabs_json = tj
            self._worker.enqueue({"kind": "tabs", "data": pub_tabs})

        # Views: grab published tabs (GUI thread), hash-gate, enqueue PNG bytes.
        for tab in win.tabs():
            name = getattr(tab, "name", None)
            if name is None or name not in self._published_tabs:
                continue
            png = self._capture_tab(tab, name)
            if png is not None:
                self._worker.enqueue({"kind": "view", "tab": name, "png": png})

    def _capture_tab(self, tab, name: str) -> bytes | None:
        try:
            pixmap = tab.grab()
        except Exception:
            return None
        if pixmap is None or pixmap.isNull() or pixmap.width() <= 0 or pixmap.height() <= 0:
            return None
        digest = None
        try:
            img = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
            digest = _qimage_digest(img)
        except Exception:
            digest = None
        if digest is not None and self._view_hashes.get(name) == digest:
            return None    # unchanged (fast path, no PNG encode)
        if max(pixmap.width(), pixmap.height()) > _VIEW_MAX_EDGE:
            pixmap = pixmap.scaled(
                _VIEW_MAX_EDGE, _VIEW_MAX_EDGE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        png = self._pixmap_png(pixmap)
        if png is None:
            return None
        if digest is None:
            # constBits failed — fall back to hashing the PNG bytes (correctness
            # kept; only the no-encode fast path is lost).
            digest = hashlib.sha1(png).digest()
            if self._view_hashes.get(name) == digest:
                return None
        self._view_hashes[name] = digest
        return png

    @staticmethod
    def _pixmap_png(pixmap) -> bytes | None:
        try:
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            pixmap.save(buf, "PNG")
            buf.close()
            return bytes(ba.data())
        except Exception:
            return None
