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
import logging
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
from meeting import local_relay
from meeting.local_relay import LocalRelayServer
from meeting.tunnel import Tunnel

_log = logging.getLogger(__name__)

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
# Full-extent view PNGs (full figures, not viewport crops) can be large. Cap by
# total area (megapixels) — not max-edge — so long/wide figures keep their long
# axis (and thus legible labels) instead of having the short axis crushed. A hard
# per-edge ceiling guards browser max-texture/decode limits; a byte cap bounds
# host+relay memory. RELAY_VIEW_MAX_MP lets a thin-uplink host dial the area down.
_VIEW_MAX_MEGAPIXELS = float(os.environ.get("RELAY_VIEW_MAX_MP", "8") or 8)
_VIEW_MAX_EDGE = 8192
_VIEW_MAX_PNG_BYTES = 6 * 1024 * 1024
# Min seconds between partial /out posts per session while an assistant streams.
# The guest polls at ~1s when busy, so finer-grained posts are wasted; the final
# full text is unthrottled (messageAdded), so a throttled-away partial is loss-free.
_STREAM_MIN_INTERVAL = 0.7


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
            # Supersede any still-queued partial for the same stream so a slow
            # network can't accumulate stale partials — each partial carries the
            # full cumulative text, so dropping older ones is loss-free. The final
            # (partial=False) is never superseded and always sent.
            sid_stream = item.get("stream_id")
            if sid_stream and item.get("kind") == "out" and item.get("partial"):
                self._outbox = deque(
                    it for it in self._outbox
                    if not (it.get("kind") == "out" and it.get("partial")
                            and it.get("stream_id") == sid_stream)
                )
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
            elif kind == "backlog":
                resp = self._req("PUT", f"/backlog/{_q(self._ch)}/{_q(item['sid'])}", data=item["data"])
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


class _TunnelStarter(QThread):
    """Starts the public tunnel (provider per RELAY_TUNNEL) off the GUI thread.
    Tags every signal with its own generation so MeetingRelay can drop stale
    results after a stop / re-start."""

    sig_ready = Signal(int, str)    # (gen, url)
    sig_failed = Signal(int, str)   # (gen, error_message)

    def __init__(self, tunnel: "Tunnel", port: int, gen: int, parent=None):
        super().__init__(parent)
        self._tunnel = tunnel
        self._port = port
        self._gen = gen

    def run(self) -> None:
        try:
            url = self._tunnel.start(self._port)
            self.sig_ready.emit(self._gen, url)
        except Exception as e:
            self.sig_failed.emit(self._gen, repr(e))


class MeetingRelay(QObject):
    """Meeting lifecycle + capture timer + echo gates (GUI thread)."""

    channelStateChanged = Signal(str)
    participantsUpdated = Signal(object)
    remoteMessageReceived = Signal(str, str, str)
    tokenReady = Signal(str)        # final token, after the guest URL is resolved

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
        self._backfilled_ids: set[str] = set()   # sids whose pre-meeting transcript was staged
        self._meeting_start_ids: set[str] = set()
        self._published_tabs: set[str] = set()
        self._tab_known: set[str] = set()
        # Auto-share new chat sessions (Issue #48): default on, persisted in ui_prefs.json.
        from llm_bridge.paths import read_ui_pref
        pref = read_ui_pref("auto_share_new_sessions", True)
        self._auto_share_new_sessions = pref if isinstance(pref, bool) else True
        self._session_known: set[str] = set()   # session counterpart of _tab_known
        self._participants: list = []
        self._last_sessions_json: str | None = None
        self._last_tabs_json: str | None = None
        self._view_hashes: dict[str, bytes] = {}
        # cacheKey() of the last captured pixmap per tab. full_pixmap() returns the
        # same shared QPixmap until the figure is swapped, so an unchanged cacheKey
        # lets us skip the expensive toImage()+SHA1 entirely (grab() fallback always
        # mints a fresh cacheKey, so it harmlessly falls through to the hash gate).
        self._view_cachekeys: dict[str, int] = {}
        self._admin_key = os.environ.get("RELAY_ADMIN_KEY", "") or ""
        # Legacy remote-relay override: when set, no local server / tunnel is
        # started and this URL is the shared host+guest base_url.
        self._remote_base = os.environ.get("RELAY_BASE_URL", "") or ""
        self._host_base_url = ""        # host's own admin/worker target
        self._guest_base_url = ""       # URL baked into the guest token
        self._local_server: LocalRelayServer | None = None
        self._tunnel: Tunnel | None = None
        self._tunnel_starter: _TunnelStarter | None = None
        self._gen = 0                   # tunnel-start generation (stale-signal guard)
        self._last_tunnel_failed = False

        # Streaming relay state: _stream_ids maps a session id to the stream_id of
        # its in-flight assistant turn so the final messageAdded reuses it (replace
        # the in-flight entry in place, no duplicate bubble); _stream_last_pub
        # throttles partial posts per session.
        self._stream_ids: dict[str, str] = {}
        self._stream_last_pub: dict[str, float] = {}

        cw = window.chat_widget() if hasattr(window, "chat_widget") else None
        if cw is not None and hasattr(cw, "messageAdded"):
            cw.messageAdded.connect(self._on_message_added)
        if cw is not None and hasattr(cw, "messageStreaming"):
            cw.messageStreaming.connect(self._on_message_streaming)

        self._capture_timer = QTimer(self)
        self._capture_timer.setInterval(1000)
        self._capture_timer.timeout.connect(self._on_capture_tick)

    # ---- config / accessors ----

    def is_configured(self) -> bool:
        return bool(self._admin_key)

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
        return self._guest_base_url

    def share_status(self) -> str:
        if self._sharing:
            return "sharing" if self._guest_base_url else "starting"
        return "tunnel_failed" if self._last_tunnel_failed else "idle"

    def current_token(self) -> str:
        if self._sharing and self._guest_base_url and self._channel and self._secret:
            return self._make_token()
        return ""

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

    def _active_dataset(self):
        """The window's currently-selected dataset (meeting publish scope, B5)."""
        return getattr(self._window, "current_dataset", None)

    def _tab_dataset(self, tab):
        return (getattr(tab, "session_spec", None) or {}).get("dataset")

    def _active_ds_tab_names(self) -> set[str]:
        """Tab names belonging to the active dataset (None-safe filter, B5)."""
        cur = self._active_dataset()
        out: set[str] = set()
        for tab in self._window.tabs():
            if self._tab_dataset(tab) == cur:
                n = getattr(tab, "name", None)
                if n is not None:
                    out.add(n)
        return out

    def absorb_new_tabs(self) -> list[str]:
        """Auto-share tabs that appeared after the meeting started, limited to the
        ACTIVE dataset (Issue #51 B5).

        Tabs default to shared: a tab opened mid-meeting in the active dataset
        joins the published set without the host having to click. A same-named
        tab in a hidden dataset is NOT absorbed (its view PNG would otherwise
        overwrite the active one under the shared bare-name relay id).
        Explicitly deselected tabs stay in `_tab_known` and are not re-added.
        Returns the names newly absorbed (for logging/UI), [] if idle.
        """
        if not self._sharing:
            return []
        active_names = self._active_ds_tab_names()
        new = [
            t for t in self._window.tab_names()
            if t not in self._tab_known and t in active_names
        ]
        if new:
            self._published_tabs.update(new)
            self._tab_known.update(new)
        return new

    def auto_share_new_sessions(self) -> bool:
        # getattr ガード: 会議中 hot-reload で本フィールド未保持の旧インスタンスでも安全。
        return getattr(self, "_auto_share_new_sessions", True)

    def set_auto_share_new_sessions(self, enabled: bool) -> None:
        from llm_bridge.paths import update_ui_pref
        enabled = bool(enabled)
        if not hasattr(self, "_session_known"):
            self._session_known = set(self._published_session_ids)
        # Atomic OFF→ON transition (Issue #48 P1, reviewer R2): snapshot ALL
        # currently-existing session ids into _session_known BEFORE flipping the
        # flag on. This must NOT depend on a capture/refresh tick having already
        # observed them — the toggle can be flipped within the same second a
        # session was created, and _on_auto_share_toggled enables the flag and
        # only THEN calls _refresh_lists()→absorb_new_sessions(). Without this
        # snapshot, that first absorb would see the OFF-period session as new+ON
        # and publish it, violating 1-f. After the snapshot, only sessions created
        # AFTER this point are auto-shared.
        turning_on = enabled and not getattr(self, "_auto_share_new_sessions", True)
        if turning_on and self._sharing:
            cw = self._window.chat_widget()
            if cw is not None:
                self._session_known.update(s["id"] for s in cw.session_summaries())
        self._auto_share_new_sessions = enabled
        update_ui_pref("auto_share_new_sessions", enabled)

    def absorb_new_sessions(self, summaries=None) -> list[str]:
        """Mark chat sessions created after meeting start as observed, and (only
        when the auto-share toggle is on) auto-publish them. Returns the ids newly
        PUBLISHED this call (=[] when the toggle is off or nothing is new).

        ``summaries`` lets the caller pass an already-fetched session-summary list
        (capture tick / share-window refresh both have one) to avoid a second
        GUI-thread ``session_summaries()`` fetch; when None we fetch it here.

        Privacy invariant (Issue #48 P1): while sharing we ALWAYS add observed
        sessions to ``_session_known`` regardless of the toggle, so flipping the
        toggle OFF→ON does NOT retroactively publish sessions that were created
        while it was OFF. The toggle gates ``_published_session_ids`` only. A
        session the host explicitly deselects stays in ``_session_known`` and is
        never re-absorbed (ids are uuid4 hex — never reused, so stale ids are
        harmless).
        """
        # Hot-reload guard: a mid-meeting scope=patch reload can patch this method
        # onto an instance that predates these fields. Recreate them here (called
        # from both the capture tick and the share-window timer), mirroring the
        # _backfilled_ids guard in _on_capture_tick.
        if not hasattr(self, "_auto_share_new_sessions"):
            self._auto_share_new_sessions = True
        if not hasattr(self, "_session_known"):
            self._session_known = set(self._published_session_ids)
        if not self._sharing:
            return []
        if summaries is None:
            cw = self._window.chat_widget()
            if cw is None:
                return []
            summaries = cw.session_summaries()
        # Decide (mark observed / auto-publish) only sessions in the ACTIVE
        # dataset, mirroring absorb_new_tabs (Issue #51 B5 / reviewer code P1). A
        # hidden dataset's sessions stay "undecided" (not marked known) so that
        # switching to that dataset later default-shares them; publishing a
        # hidden dataset's chat would also leak it to guests.
        cur = self._active_dataset()
        new = [
            s for s in summaries
            if s["id"] not in self._session_known and s.get("dataset") == cur
        ]
        if not new:
            return []
        # always mark observed (privacy invariant, Issue #48): a session created
        # while the auto-share toggle was OFF stays known → not retroactively
        # published on OFF→ON. Scoped to the active dataset per the filter above.
        self._session_known.update(s["id"] for s in new)
        if not self._auto_share_new_sessions:
            return []                            # toggle off: known but NOT published
        to_pub = [s["id"] for s in new]
        self._published_session_ids.update(to_pub)
        return to_pub

    # ---- lifecycle ----

    def _make_token(self) -> str:
        token_obj = {"base_url": self._guest_base_url, "channel": self._channel,
                     "secret": self._secret}
        return base64.urlsafe_b64encode(
            json.dumps(token_obj).encode("utf-8")
        ).decode("ascii").rstrip("=")

    def meeting_start(self, ttl_sec: int) -> None:
        """Create the channel, start the worker + capture timer; deliver the token
        asynchronously via ``tokenReady`` (the tunnel URL resolves off-thread).

        The default published set is the ACTIVE dataset's sessions/tabs at start
        time. ``_session_known``/``_tab_known`` are scoped to the active dataset
        too, so a hidden dataset's items stay "undecided" and are auto-absorbed
        (default-shared) when the host switches to that dataset (Issue #51 B5:
        the shared set swaps to the newly-active dataset). Explicit per-item
        deselection persists across switches (deselected items stay in ``_known``
        and are not re-absorbed).

        No ``self`` state is mutated until the ``admin/channel`` POST succeeds, so
        a POST failure propagates cleanly with ``_sharing`` still False.
        """
        if self._sharing:
            return   # re-entrancy guard: don't clobber a live tunnel starter / channel
        self._last_tunnel_failed = False

        ttl = max(3600, min(86400, int(ttl_sec)))
        ch = secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        secret_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()

        # base_url: the only `self` writes before the POST (the POST needs them).
        if self._remote_base:
            self._host_base_url = self._guest_base_url = self._remote_base
        else:
            if self._local_server is None:
                self._local_server = local_relay.start_server(self._admin_key)
            self._host_base_url = f"http://127.0.0.1:{self._local_server.port}"

        body = {"ch": ch, "ttl_sec": ttl, "secret_hash": secret_hash}
        req = urllib.request.Request(
            self._host_base_url.rstrip("/") + "/admin/channel",
            data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + self._admin_key,
                     "Content-Type": "application/json", "User-Agent": _UA},
        )
        with urllib.request.urlopen(req, timeout=_REQ_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        # POST succeeded → now commit the sharing state.
        self._expires_at = int(data.get("expires_at", 0) or 0)
        cw = self._window.chat_widget()
        summaries = cw.session_summaries() if cw is not None else []
        # Publish AND "know" only the ACTIVE dataset's sessions/tabs (Issue #51
        # B5 / reviewer code P1). Scoping _known to the active dataset (not ALL
        # ids/names) is what lets a hidden dataset's items be treated as "new"
        # and auto-absorbed (default-shared) when the host switches to that
        # dataset — absorb_new_tabs/absorb_new_sessions both only decide the
        # active dataset, mirroring each other.
        cur = self._active_dataset()
        active_ids = {s["id"] for s in summaries if s.get("dataset") == cur}
        self._published_session_ids = set(active_ids)
        self._backfilled_ids = set()   # fresh channel → re-stage backlog per session
        self._meeting_start_ids = set(self._published_session_ids)
        self._session_known = set(active_ids)
        self._published_tabs = self._active_ds_tab_names()
        self._tab_known = self._active_ds_tab_names()
        self._channel = ch
        self._secret = secret
        self._sharing = True
        self._last_sessions_json = None
        self._last_tabs_json = None
        self._view_hashes = {}
        self._view_cachekeys = {}

        self._worker = _RelayWorker(self._host_base_url, self._admin_key, ch,
                                    poll_ms=self._poll_ms)
        self._worker.sig_inbound.connect(self._on_remote_message)
        self._worker.sig_presence.connect(self._on_participants)
        self._worker.sig_state.connect(self._on_state)
        self._worker.start()
        self._capture_timer.start()
        # Push the initial snapshots immediately (don't wait for the first tick).
        self._on_capture_tick()

        if self._remote_base:
            # _guest_base_url already resolved → token is ready synchronously.
            self.tokenReady.emit(self._make_token())
        else:
            self._gen += 1
            gen = self._gen
            self.channelStateChanged.emit("starting")
            self._tunnel = Tunnel()
            self._tunnel_starter = _TunnelStarter(self._tunnel, self._local_server.port, gen)
            self._tunnel_starter.sig_ready.connect(self._on_tunnel_ready)
            self._tunnel_starter.sig_failed.connect(self._on_tunnel_failed)
            self._tunnel_starter.start()
        return None

    def _on_tunnel_ready(self, gen: int, url: str) -> None:
        if gen != self._gen or not self._sharing:
            return    # stale signal after stop / re-start
        self._guest_base_url = url
        self.tokenReady.emit(self._make_token())

    def _on_tunnel_failed(self, gen: int, msg: str) -> None:
        if gen != self._gen:
            return
        # Order matters: channelStateChanged is delivered synchronously on a
        # same-thread connection, so the GUI slot runs mid-emit. Flip _sharing
        # off FIRST (via meeting_stop) so the GUI sees is_sharing()==False and
        # renders idle, not a stale "starting".
        self.meeting_stop()
        self._last_tunnel_failed = True
        self.channelStateChanged.emit("tunnel_failed")

    def meeting_stop(self) -> None:
        ch = self._channel
        if ch and self._host_base_url and self._admin_key:
            try:
                req = urllib.request.Request(
                    self._host_base_url.rstrip("/") + "/admin/channel/" + _q(ch),
                    method="DELETE",
                    headers={"Authorization": "Bearer " + self._admin_key,
                             "User-Agent": _UA},
                )
                urllib.request.urlopen(req, timeout=_REQ_TIMEOUT).close()
            except Exception:
                pass    # best-effort; TTL + heartbeat-grace are the hard backstop
        self.stop()

    def stop(self) -> None:
        """Idempotent. requestInterruption() + wait(); never force-kills threads."""
        self._sharing = False
        self._gen += 1                 # invalidate in-flight tunnel-starter signals
        self._capture_timer.stop()
        w = self._worker
        if w is not None:
            w.requestInterruption()
            w.wait(8000)
            self._worker = None
        # Stop the tunnel BEFORE waiting on the starter: cloudflared/pinggy
        # start() blocks waiting for the tunnel to come up, so t.stop() must
        # terminate that process first to unblock the starter thread we then join
        # (it also flips _stop_requested so a concurrent start() can't re-enable).
        t = self._tunnel
        if t is not None:
            t.stop()
        ts = self._tunnel_starter
        if ts is not None:
            ts.requestInterruption()
            ts.wait(8000)
            self._tunnel_starter = None
        self._tunnel = None
        self._guest_base_url = ""
        # _host_base_url and _local_server are app-scoped (reused across shares).

    def shutdown(self) -> None:
        """App-teardown path (idempotent): stop sharing AND close the local server."""
        self.meeting_stop()
        if self._local_server is not None:
            self._local_server.shutdown()
            self._local_server = None

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
        # Pop the in-flight stream_id regardless of the gate so a later turn can't
        # inherit a stale id (the gate can flip mid-turn). getattr guards: a
        # mid-meeting scope=patch reload can patch this method onto an instance
        # predating these fields (mirrors the _backfilled_ids guard below).
        stream_ids = getattr(self, "_stream_ids", None)
        sid_stream = stream_ids.pop(sid, None) if stream_ids is not None else None
        last_pub = getattr(self, "_stream_last_pub", None)
        if last_pub is not None:
            last_pub.pop(sid, None)
        if not (self._sharing and origin == "local" and sid in self._published_session_ids):
            return
        if not content or not content.strip():
            return
        name = tr("meeting.assistant_name_default") if role == "assistant" else self._host_name
        body = {"text": content, "name": name, "role": role}
        item = {"kind": "out", "sid": sid, "body": body}
        # Final of a streamed assistant turn: reuse its stream_id with partial=False
        # so the relay replaces the in-flight entry in place (no duplicate bubble).
        if role == "assistant" and sid_stream:
            body["stream_id"] = sid_stream
            body["partial"] = False
            item["stream_id"] = sid_stream
            item["partial"] = False
        if self._worker is not None:
            self._worker.enqueue(item)

    def _on_message_streaming(self, sid: str, content: str, origin: str,
                              stream_id: str) -> None:
        # Live partial of an in-flight assistant turn — same out: gate as
        # _on_message_added, throttled to _STREAM_MIN_INTERVAL per session so a
        # fast token stream doesn't flood the relay. The final full text still
        # arrives via _on_message_added, so a throttled-away partial is loss-free.
        if not (self._sharing and origin == "local" and sid in self._published_session_ids):
            return
        if not content or not content.strip() or not stream_id:
            return
        # getattr guard: mid-meeting scope=patch reload may patch this onto an
        # instance predating these fields (see _on_message_added).
        if not hasattr(self, "_stream_ids"):
            self._stream_ids = {}
        if not hasattr(self, "_stream_last_pub"):
            self._stream_last_pub = {}
        self._stream_ids[sid] = stream_id
        now = time.monotonic()
        if now - self._stream_last_pub.get(sid, 0.0) < _STREAM_MIN_INTERVAL:
            return
        self._stream_last_pub[sid] = now
        name = tr("meeting.assistant_name_default")
        body = {"text": content, "name": name, "role": "assistant",
                "stream_id": stream_id, "partial": True}
        if self._worker is not None:
            self._worker.enqueue({"kind": "out", "sid": sid, "body": body,
                                  "stream_id": stream_id, "partial": True})

    def _backfill_session(self, sid: str, cw) -> None:
        """Stage a published session's existing transcript into the relay so guests
        can fetch history older than meeting start (GET /history). Called once per
        session per meeting (caller guards with _backfilled_ids). No duplication
        with live out: — backfill captures only messages present now; later messages
        flow through _on_message_added with newer (live) mids."""
        get = getattr(cw, "_session_by_id", None)
        if get is None or self._worker is None:
            return
        sess = get(sid)
        msgs = list(getattr(sess, "messages", []) or []) if sess is not None else []
        data = []
        for m in msgs:
            role = getattr(m, "role", "")
            if role not in ("user", "assistant"):
                continue
            content = getattr(m, "content", None)
            if not content or not str(content).strip():
                continue
            name = tr("meeting.assistant_name_default") if role == "assistant" else self._host_name
            data.append({"text": str(content), "name": name, "role": role})
        self._worker.enqueue({"kind": "backlog", "sid": sid, "data": data})

    # ---- capture (GUI thread) ----

    def _on_capture_tick(self) -> None:
        if not self._sharing or self._worker is None:
            return
        if self._expires_at and time.time() >= self._expires_at:
            # Host-side expiry: the local admin routes don't check expiry (worker.js
            # SSOT), so writing past TTL would re-create metadata-less channels that
            # _evict_expired_channels can't reap. Self-stop + DELETE so memory frees,
            # symmetric to the guest 410.
            self.meeting_stop()
            self.channelStateChanged.emit("expired")
            return
        # This runs on the GUI thread as a QTimer slot; an unhandled exception
        # from a Qt slot can tear down the whole app in PySide6. Guard the whole
        # body and skip this tick on failure (next tick recovers); keep sharing
        # alive. Worker-thread paths have their own try/except.
        try:
            win = self._window
            cw = win.chat_widget()

            # Active-dataset scope (Issue #51 B5): the effective publish set is the
            # host's selection ∩ the ACTIVE dataset. absorb_new_* only decide the
            # active dataset, so switching to another dataset auto-absorbs (default-
            # shares) its tabs/sessions on the next tick, while previously-active
            # ones drop out of the ∩ (still selected, just not the active scope).
            cur = self._active_dataset()

            # Sessions: publish only ids in (_published_session_ids ∩ existing ∩
            # active-ds) so a deleted session drops out and a hidden dataset's
            # chat never leaks.
            summaries = cw.session_summaries() if cw is not None else []
            existing = {s["id"] for s in summaries}
            active_sids = {s["id"] for s in summaries if s.get("dataset") == cur}
            self.absorb_new_sessions(summaries)
            self._published_session_ids &= existing
            eff_sids = self._published_session_ids & active_sids
            # Stage each newly-published session's pre-meeting transcript once so
            # guests can fetch history older than meeting start (GET /history).
            # getattr guard: a mid-meeting hot-reload patches new code onto the
            # existing MeetingRelay instance, which lacks the new _backfilled_ids
            # field — recreate it so backfill still runs (next tick) without a
            # fresh meeting.
            if not hasattr(self, "_backfilled_ids"):
                self._backfilled_ids = set()
            if cw is not None:
                for sid in list(eff_sids - self._backfilled_ids):
                    self._backfill_session(sid, cw)
                    self._backfilled_ids.add(sid)
            pub = [
                {"id": s["id"], "title": s["title"], "busy": s["busy"]}
                for s in summaries if s["id"] in eff_sids
            ]
            sj = json.dumps(pub, sort_keys=True, ensure_ascii=False)
            if sj != self._last_sessions_json:
                self._last_sessions_json = sj
                self._worker.enqueue({"kind": "sessions", "data": pub})

            # Tabs: new tabs auto-join the published set (default-share, active DS
            # only); publish the selection ∩ active-ds tabs. Build the guest list
            # from the active dataset's own tab objects (dataset-checked, deduped,
            # in window order) — NOT the flat bare-name tab_names(), which can hold
            # a hidden dataset's same-named tab and duplicate it (reviewer code P2).
            self.absorb_new_tabs()
            seen_tab: set[str] = set()
            pub_tabs: list[str] = []
            for tab in win.tabs():
                n = getattr(tab, "name", None)
                if n is None or n in seen_tab:
                    continue
                if n in self._published_tabs and self._tab_dataset(tab) == cur:
                    seen_tab.add(n)
                    pub_tabs.append(n)
            tj = json.dumps(pub_tabs, sort_keys=True, ensure_ascii=False)
            if tj != self._last_tabs_json:
                self._last_tabs_json = tj
                self._worker.enqueue({"kind": "tabs", "data": pub_tabs})

            # Views: grab published tabs in the ACTIVE dataset only (GUI thread),
            # hash-gate, enqueue PNG bytes. Restricting to the active DS keeps a
            # same-named tab in a hidden dataset from overwriting the shared view.
            for tab in win.tabs():
                name = getattr(tab, "name", None)
                if name is None or name not in self._published_tabs:
                    continue
                if self._tab_dataset(tab) != cur:
                    continue
                png = self._capture_tab(tab, name)
                if png is not None:
                    self._worker.enqueue({"kind": "view", "tab": name, "png": png})
        except Exception as exc:
            _log.warning("capture tick skipped: %s", exc)

    def _capture_tab(self, tab, name: str) -> bytes | None:
        try:
            # Full content extent (decoupled from host zoom/pan/scroll) when the
            # tab supports it; else the on-screen viewport. getattr guard tolerates
            # a non-AnalysisTab entry in window.tabs().
            grab_full = getattr(tab, "grab_full", None)
            pixmap = grab_full() if callable(grab_full) else tab.grab()
        except Exception:
            return None
        if pixmap is None or pixmap.isNull() or pixmap.width() <= 0 or pixmap.height() <= 0:
            return None
        # Fast path: a full_pixmap-backed view returns the same shared QPixmap until
        # the figure changes, so an unchanged cacheKey skips toImage()+SHA1 entirely.
        cache_key = pixmap.cacheKey()
        if cache_key and self._view_cachekeys.get(name) == cache_key:
            return None
        digest = None
        try:
            img = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
            digest = _qimage_digest(img)
        except Exception:
            digest = None
        if digest is not None and self._view_hashes.get(name) == digest:
            self._view_cachekeys[name] = cache_key   # remember so we short-circuit next tick
            return None    # unchanged (fast path, no PNG encode)
        pixmap = self._fit_for_wire(pixmap)
        png = self._pixmap_png(pixmap)
        if png is None:
            return None
        if len(png) > _VIEW_MAX_PNG_BYTES:
            # One corrective downscale; 0.95 fudge for PNG nonlinearity. Still over →
            # skip this frame (leaving the prior good frame on guests).
            s = (_VIEW_MAX_PNG_BYTES / len(png)) ** 0.5 * 0.95
            pixmap = pixmap.scaled(
                max(1, round(pixmap.width() * s)), max(1, round(pixmap.height() * s)),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            png = self._pixmap_png(pixmap)
            if png is None or len(png) > _VIEW_MAX_PNG_BYTES:
                # Record cacheKey so an identical oversized pixmap short-circuits
                # next tick (no re-encode loop); a changed figure mints a new key.
                if cache_key:
                    self._view_cachekeys[name] = cache_key
                return None
        if digest is None:
            # constBits failed — fall back to hashing the PNG bytes (correctness
            # kept; only the no-encode fast path is lost).
            digest = hashlib.sha1(png).digest()
            if self._view_hashes.get(name) == digest:
                self._view_cachekeys[name] = cache_key
                return None
        self._view_hashes[name] = digest
        self._view_cachekeys[name] = cache_key
        return png

    @staticmethod
    def _fit_for_wire(pixmap):
        """Downscale to the megapixel/edge budget, preserving aspect exactly."""
        w, h = pixmap.width(), pixmap.height()
        area = w * h
        s_area = (_VIEW_MAX_MEGAPIXELS * 1e6 / area) ** 0.5 if area > _VIEW_MAX_MEGAPIXELS * 1e6 else 1.0
        s_edge = min(1.0, _VIEW_MAX_EDGE / max(w, h))
        s = min(s_area, s_edge)
        if s >= 1.0:
            return pixmap
        return pixmap.scaled(
            max(1, round(w * s)), max(1, round(h * s)),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

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
