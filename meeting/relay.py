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
# Watched-DS view rendering (Issue #78): a background DS is view-rendered only
# while a guest is actively watching it (presence-derived). _WATCH_WARM_GRACE_SEC
# keeps a DS "warm" for several presence polls (_PRESENCE_POLL_SEC) after the last
# sighting so a transient presence gap doesn't drop its view. _MAX_WATCHED_RENDER
# caps the simultaneously-rendered background DSs (active is always rendered) so a
# crowd each opening a different DS can't fan the host out to every open DS per tick.
_WATCH_WARM_GRACE_SEC = 45.0
_MAX_WATCHED_RENDER = 8


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
    sig_new_session = Signal(str, str)     # (ds, name) — guest-requested new chat (Issue #81)
    sig_presence = Signal(object)         # list[{pid,name,sid,tab,last}]
    sig_state = Signal(str)               # "expired" | "disconnected" | "ok"
    sig_sendfail = Signal(str, str, str)  # (kind, ds, tab) of a dropped outbox item (no retry)

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
        # Issue #81: /newsessions keeps its OWN cursor + dedup so it can never
        # consume or skew the /inbound message cursor. No hot-reload getattr guard
        # here on purpose — the new (ds, name) Signal added to this class changes
        # the class's Signal set, which devtools/hotreload.py flags as
        # "requires scope=app", and a running pre-#81 run() frame has no
        # _do_new_sessions() call site in its bytecode, so these fields can never be
        # reached un-initialised.
        self._ns_latest_seen_ts = 0
        self._ns_reanchor = True
        self._ns_seen: set[str] = set()
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
            self._do_new_sessions()
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
                resp = self._req("PUT", f"/view/{_q(self._ch)}/{_q(item['tab'])}"
                                 f"?ds={_q(item.get('ds') or '')}",
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
                self.sig_sendfail.emit(str(kind or ""), str(item.get("ds") or ""),
                                       str(item.get("tab") or ""))
        except Exception:
            self._note_fail()
            self.sig_sendfail.emit(str(kind or ""), str(item.get("ds") or ""),
                                   str(item.get("tab") or ""))

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

    def _do_new_sessions(self) -> None:
        """Poll guest new-chat requests (Issue #81) — the /inbound counterpart.

        Independent cursor/dedup (_ns_*). Reanchor semantics mirror _do_inbound:
        the first poll omits `since`, so the server's 15s lookback floor applies.
        That cannot replay a stale request in practice — meeting_start always mints
        a FRESH channel (secrets.token_hex) and a fresh worker, so the queue this
        worker first reads is always empty.
        """
        try:
            if self._ns_reanchor:
                res = self._req("GET", f"/newsessions/{_q(self._ch)}")
            else:
                floor = max(0, self._ns_latest_seen_ts - _LOOKBACK_MS)
                since = f"{floor:013d}-{'0' * 13}"
                res = self._req("GET", f"/newsessions/{_q(self._ch)}?since={_q(since)}")
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
        if self._ns_reanchor:
            self._ns_latest_seen_ts = server_now    # adopt the worker clock
            self._ns_reanchor = False
        for m in payload.get("requests", []):
            mid = m.get("mid")
            if not mid or mid in self._ns_seen:
                continue
            self._ns_seen.add(mid)
            self.sig_new_session.emit(str(m.get("ds", "")), str(m.get("name", "")))
            ts = self._mid_ts(mid)
            if ts > self._ns_latest_seen_ts:
                self._ns_latest_seen_ts = ts
        if server_now > self._ns_latest_seen_ts:
            self._ns_latest_seen_ts = server_now
        cutoff = self._ns_latest_seen_ts - 2 * _LOOKBACK_MS
        self._ns_seen = {x for x in self._ns_seen if self._mid_ts(x) >= cutoff}

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
        # (dataset, tab-name) pairs — all open DSs are default-shared (Issue #78),
        # so a bare name would collide across datasets (dsA/dsB "overview").
        self._published_tabs: set[tuple[str, str]] = set()
        self._tab_known: set[tuple[str, str]] = set()
        # Auto-share new chat sessions (Issue #48): default on, persisted in ui_prefs.json.
        from llm_bridge.paths import read_ui_pref
        pref = read_ui_pref("auto_share_new_sessions", True)
        self._auto_share_new_sessions = pref if isinstance(pref, bool) else True
        self._session_known: set[str] = set()   # session counterpart of _tab_known
        self._participants: list = []
        self._last_sessions_json: str | None = None
        self._last_tabs_json: str | None = None
        # Per-DS view render demand (Issue #78): ds-key ("" = null group) → monotonic
        # expiry. A DS is view-rendered while active or while a guest watches it
        # (presence-derived, warm-graced). Keyed by (ds, tab) so same-named tabs in
        # different datasets don't share a dedupe entry.
        self._watched_ds_until: dict[str, float] = {}
        self._view_hashes: dict[tuple[str, str], bytes] = {}
        # cacheKey() of the last captured pixmap per (ds, tab). full_pixmap() returns
        # the same shared QPixmap until the figure is swapped, so an unchanged cacheKey
        # lets us skip the expensive toImage()+SHA1 entirely (grab() fallback always
        # mints a fresh cacheKey, so it harmlessly falls through to the hash gate).
        self._view_cachekeys: dict[tuple[str, str], int] = {}
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

    def published_tabs(self) -> set[tuple[str, str]]:
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
        # Record every session the host explicitly decided (published OR opted
        # out) as "known" so a hidden dataset's session that was explicitly
        # deselected is NOT treated as an undecided-new item and re-absorbed
        # (default-shared) on a later dataset switch — "explicit deselection
        # persists across switches" (Issue #51 / reviewer code P2 R3). hasattr guard:
        # a mid-meeting hot-reload can patch this onto an instance predating
        # _session_known.
        if not hasattr(self, "_session_known"):
            self._session_known = set(self._published_session_ids)
        self._session_known |= new | removed
        # Drop any pending remote turns queued for sessions just opted out, so a
        # later FIFO drain can't inject into a now-private session.
        if removed:
            cw = self._window.chat_widget() if hasattr(self._window, "chat_widget") else None
            pend = getattr(cw, "_pending_remote", None)
            if isinstance(pend, dict):
                for sid in removed:
                    pend.pop(sid, None)

    def set_published_tabs(self, pairs: "set[tuple[str, str]] | list[tuple[str, str]]") -> None:
        # Symmetric to set_published_sessions (Issue #51 / reviewer code P2 R3):
        # record every explicitly-decided (dataset, name) tab (published OR
        # deselected) as known so a deselected tab is not re-absorbed
        # (default-shared) on a later capture tick. Since Issue #78 all open
        # datasets' tabs are shareable, so `pairs` spans every open dataset.
        new = set(pairs)
        removed = self._published_tabs - new
        if not hasattr(self, "_tab_known"):
            self._tab_known = set(self._published_tabs)
        self._tab_known |= new | removed
        self._published_tabs = new

    def _active_dataset(self):
        """The window's currently-selected dataset (meeting publish scope, B5)."""
        return getattr(self._window, "current_dataset", None)

    def _tab_dataset(self, tab):
        return (getattr(tab, "session_spec", None) or {}).get("dataset")

    def _all_ds_tab_pairs(self) -> set[tuple[str, str]]:
        """(ds-key, tab-name) pairs for EVERY open dataset (Issue #78). ds None is
        coerced to "" (null group). Names are None-safe (a non-name tab is skipped).
        This is the all-DS successor to the pre-#78 active-dataset-only helper."""
        out: set[tuple[str, str]] = set()
        for tab in self._window.tabs():
            n = getattr(tab, "name", None)
            if n is None:
                continue
            ds = self._tab_dataset(tab)
            out.add(("" if ds is None else ds, n))
        return out

    def _shareable_ds_keys(self, sessions) -> list[str]:
        """DS wire keys a guest can see, in guest DS-bar order ("" = null group).

        SSOT for the two callers that MUST agree (Issue #81): _on_capture_tick
        publishes this list as `datasets` (the guest's clickable DS chips), and
        _on_new_session_request accepts a "+" only for a key in it. If they drift,
        a guest can click a chip the host then silently redirects elsewhere.

        Order: open datasets, then extras contributed by `sessions`, then by
        published tabs. Only the TAB-DERIVED TAIL is unstable: `_published_tabs`
        is a set, so its iteration order varies with PYTHONHASHSEED. The
        open-dataset prefix (window._groups is an insertion-ordered dict = display
        order) and the session-derived middle (`sessions` is a list) are both
        deterministic. That tail instability is a pre-existing property of the
        inline code this replaces — just don't write a test that compares that
        tail verbatim with more than one key in it.
        The last two sources matter because `close_dataset` is
        "close != forget": a closed dataset's chats stay in ChatWidget._sessions
        and its (ds, name) pairs stay in _published_tabs (only an explicit
        set_published_tabs opt-out removes them), so its chip keeps showing.

        `sessions` is the PUBLISHED session subset — an opted-out session's dataset
        contributes no chip, so it must not widen the accepted set either.

        getattr on open_dataset_keys tolerates a pre-#78 window double; the old
        inline form raised there and _on_capture_tick swallowed the whole tick.
        """
        getter = getattr(self._window, "open_dataset_keys", None)
        keys = list(getter()) if callable(getter) else []
        for s in sessions:
            k = "" if s.get("dataset") is None else s["dataset"]
            if k not in keys:
                keys.append(k)
        for (ds_key, _name) in self._published_tabs:
            if ds_key not in keys:
                keys.append(ds_key)
        return keys

    def absorb_new_tabs(self) -> list[tuple[str, str]]:
        """Auto-share tabs that appeared after the meeting started, across ALL open
        datasets (Issue #78).

        Tabs default to shared: a tab opened mid-meeting in any open dataset joins
        the published set without the host having to click. Keyed by (ds, name) so
        same-named tabs in different datasets are distinct. Explicitly deselected
        tabs stay in `_tab_known` and are not re-added. Returns the (ds, name)
        pairs newly absorbed (for logging/UI), [] if idle.
        """
        if not self._sharing:
            return []
        new = [p for p in self._all_ds_tab_pairs() if p not in self._tab_known]
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
        # Decide (mark observed / auto-publish) sessions across ALL open datasets
        # (Issue #78): every open DS is default-shared, so a new session in any DS
        # auto-joins. Explicit deselection persists via _session_known.
        new = [s for s in summaries if s["id"] not in self._session_known]
        if not new:
            return []
        # always mark observed (privacy invariant, Issue #48): a session created
        # while the auto-share toggle was OFF stays known → not retroactively
        # published on OFF→ON.
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

        The default published set is EVERY open dataset's sessions/tabs at start
        time (Issue #78: guests independently browse and drive any dataset, so the
        host default-shares all of them). ``_session_known``/``_tab_known`` seed
        from all open datasets too. Explicit per-item deselection makes an item
        private and persists across dataset switches (deselected items stay in
        ``_known`` and are not re-absorbed). Sessions/tabs opened mid-meeting in
        any dataset auto-absorb (default-shared) on the next capture tick.

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
        # Publish AND "know" EVERY open dataset's sessions/tabs (Issue #78:
        # default-share all open datasets so guests can browse/drive any of them;
        # explicit opt-out is the only thing that makes an item private).
        # absorb_new_tabs/absorb_new_sessions decide all open datasets too, so an
        # item opened mid-meeting in any dataset auto-absorbs on the next tick.
        all_ids = {s["id"] for s in summaries}
        self._published_session_ids = set(all_ids)
        self._backfilled_ids = set()   # fresh channel → re-stage backlog per session
        self._meeting_start_ids = set(self._published_session_ids)
        self._session_known = set(all_ids)
        all_pairs = self._all_ds_tab_pairs()
        self._published_tabs = set(all_pairs)
        self._tab_known = set(all_pairs)
        self._channel = ch
        self._secret = secret
        self._sharing = True
        self._last_sessions_json = None
        self._last_tabs_json = None
        self._watched_ds_until = {}
        self._view_hashes = {}
        self._view_cachekeys = {}

        self._worker = _RelayWorker(self._host_base_url, self._admin_key, ch,
                                    poll_ms=self._poll_ms)
        self._worker.sig_inbound.connect(self._on_remote_message)
        self._worker.sig_new_session.connect(self._on_new_session_request)
        self._worker.sig_presence.connect(self._on_participants)
        self._worker.sig_state.connect(self._on_state)
        self._worker.sig_sendfail.connect(self._on_sendfail)
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
        # Derive watched-DS demand from presence (Issue #78): a guest actively
        # viewing a DS re-stamps its expiry every presence poll (~5s); a DS left
        # unwatched naturally expires after _WATCH_WARM_GRACE_SEC. Only count
        # entries that actually carry "ds" (bootstrap/legacy guests omit it — an
        # absent key must not register as the null group). hasattr guard: a
        # mid-meeting hot-reload can patch this onto an instance predating the field.
        if not hasattr(self, "_watched_ds_until"):
            self._watched_ds_until = {}
        now = time.monotonic()
        for p in self._participants:
            if isinstance(p, dict) and "ds" in p:
                self._watched_ds_until[str(p.get("ds") or "")] = now + _WATCH_WARM_GRACE_SEC
        self.participantsUpdated.emit(self._participants)

    def _on_remote_message(self, sid: str, name: str, text: str) -> None:
        # in: receive gate — symmetric to the out: send gate. KV is eventually
        # consistent, so a guest may POST to a just-opted-out sid within the
        # propagation window; drop it here so a private session never drives the
        # agent (tool-operation rights). Gated on _published_session_ids only:
        # all open DSs are shared now (Issue #78), so a guest may legitimately
        # inject into a background-DS session.
        if sid not in self._published_session_ids:
            return
        cw = self._window.chat_widget()
        if cw is not None:
            cw.inject_remote_message(text, name, session_id=sid)
        self.remoteMessageReceived.emit(sid, name, text)

    def _on_new_session_request(self, ds: str, name: str) -> None:
        """Guest-requested new chat session (Issue #81). GUI thread (queued signal).

        `ds` is the relay's DS wire key ("" = the null/dataset-less group), already
        canonicalised by RelayState._ds_key (an absent `?ds=` resolved to the host's
        active dataset).

        Accepted DS set = exactly the chips a guest can see, i.e. what
        _on_capture_tick publishes as `datasets` — both go through
        _shareable_ds_keys() with the same (published) session subset, so the two
        can never drift apart. A key outside that set falls back to the host's
        current dataset and is logged at WARNING with BOTH the requested and the
        actual ds (an INFO line carrying only the rewritten value would erase the
        fact that a fallback happened).

        The null group ("") is never a creation target (see the Issue's
        "null グループを作成対象から外す"): such a session is skipped by
        _save_chat_sessions and would be auto-adopted away by _start_turn.

        The session is published EXPLICITLY (both _published_session_ids and
        _session_known) regardless of the auto-share toggle: the guest asked for it.
        Adding to _session_known first is what stops absorb_new_sessions from
        re-deciding it on the next capture tick.
        """
        if not self._sharing:
            return
        cw = self._window.chat_widget() if hasattr(self._window, "chat_widget") else None
        create = getattr(cw, "create_remote_session", None)
        if not callable(create):
            return    # hot-reload: a ChatWidget predating create_remote_session
        if ds == "":
            # Rejected BEFORE the fallback: falling back to the active dataset here
            # would silently create the chat somewhere the guest did not ask for.
            # テスト12 はこの WARNING を rec.getMessage() の部分一致
            # ("the null group is not a creatable target") で拾う。隣接リテラルは
            # コンパイル時に連結されるので折り返し位置は自由だが、境界の空白を
            # 落とすと文言が変わってテストが落ちる。テスト18 が拾う
            # "the host has no current dataset to fall back to" も同じ制約。
            _log.warning(
                "guest %r new-chat request dropped: "
                "the null group is not a creatable target "
                "(such a session is never persisted)", name,
            )
            return
        summaries = cw.session_summaries()
        pub = [s for s in summaries if s["id"] in self._published_session_ids]
        allowed = {k for k in self._shareable_ds_keys(pub) if k}
        if ds not in allowed:
            cur = self._active_dataset()
            if cur is None:
                _log.warning(
                    "guest %r new-chat request dropped: ds=%r is not shareable and "
                    "the host has no current dataset to fall back to", name, ds,
                )
                return
            _log.warning(
                "guest %r new-chat request: ds=%r is not shareable, creating in %r "
                "instead", name, ds, cur,
            )
            ds = cur
        sid = create(dataset=ds)
        if not sid:
            return
        if not hasattr(self, "_session_known"):
            self._session_known = set(self._published_session_ids)
        self._published_session_ids.add(sid)
        self._session_known.add(sid)
        # Audit trail: the host can tell WHICH guest minted the chat. The session
        # title stays the _DEFAULT_TITLE sentinel so _start_turn's auto-title still
        # fires on the first message — that is why `name` is logged, not titled.
        _log.info("guest %r created chat session %s (ds=%r)", name, sid, ds)

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
        # out: send only host-local messages of a published session. All open DSs
        # are shared now (Issue #78), so the gate is _published_session_ids only;
        # stream_id pop above runs regardless of the gate.
        if not (self._sharing and origin == "local"
                and sid in self._published_session_ids):
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
        # _on_message_added (published only, Issue #78), throttled to
        # _STREAM_MIN_INTERVAL per session so a fast token stream doesn't flood the
        # relay. The final full text still arrives via _on_message_added, so a
        # throttled-away partial is loss-free.
        if not (self._sharing and origin == "local"
                and sid in self._published_session_ids):
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

    def _on_sendfail(self, kind: str, ds: str = "", tab: str = "") -> None:
        # _send drops a failed item without retry, but the change-detect latch was
        # already advanced at enqueue time — a lost tabs PUT would leave the server
        # on the old published set. Reset the latch so the next capture tick
        # re-enqueues at the correct FIFO position. (Re-enqueueing inside _send
        # instead would reorder the PUT after same-tick view items.)
        #
        # A dropped view PUT must roll back just that (ds, tab)'s dedupe entry:
        # _capture_tab latches them at CAPTURE time, decoupled from send success,
        # and a static figure never mints a new hash — so a dropped view PUT would
        # otherwise leave guests on 204 + placeholder forever. The old wholesale
        # cache wipe on a tabs failure is gone (Issue #78 removed the server's
        # dataset-switch view wipe, so there is nothing to recover from there).
        if kind == "tabs":
            self._last_tabs_json = None
        elif kind == "sessions":
            self._last_sessions_json = None
        elif kind == "view":
            self._view_hashes.pop((ds, tab), None)
            self._view_cachekeys.pop((ds, tab), None)

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

            cur = self._active_dataset()
            active_key = "" if cur is None else cur

            # Sessions: publish _published_session_ids ∩ existing across ALL open
            # datasets (Issue #78) — a deleted session drops out via ∩ existing.
            summaries = cw.session_summaries() if cw is not None else []
            existing = {s["id"] for s in summaries}
            self.absorb_new_sessions(summaries)
            self._published_session_ids &= existing
            # Stage each newly-published session's pre-meeting transcript once so
            # guests can fetch history older than meeting start (GET /history).
            # getattr guard: a mid-meeting hot-reload patches new code onto the
            # existing MeetingRelay instance, which lacks _backfilled_ids —
            # recreate it so backfill still runs (next tick) without a fresh meeting.
            if not hasattr(self, "_backfilled_ids"):
                self._backfilled_ids = set()
            if cw is not None:
                for sid in list(self._published_session_ids - self._backfilled_ids):
                    self._backfill_session(sid, cw)
                    self._backfilled_ids.add(sid)
            # Each session carries its dataset (str or None) so guests can bucket
            # the flat all-DS session list by DS (invariant: every session has the
            # key; session_summaries() always provides `dataset`).
            pub = [
                {"id": s["id"], "title": s["title"], "busy": s["busy"],
                 "dataset": s.get("dataset")}
                for s in summaries if s["id"] in self._published_session_ids
            ]
            sj = json.dumps(pub, sort_keys=True, ensure_ascii=False)
            if sj != self._last_sessions_json:
                self._last_sessions_json = sj
                self._worker.enqueue({"kind": "sessions", "data": pub})

            # Tabs: new tabs auto-join across ALL open datasets. Names live per-DS
            # in tabs_by_dataset (same-named tabs in different datasets stay
            # distinct); only (ds, name) in _published_tabs are shared.
            self.absorb_new_tabs()
            tabs_by_dataset: dict[str, list[str]] = {}
            for tab in win.tabs():
                name = getattr(tab, "name", None)
                if name is None:
                    continue
                tds = self._tab_dataset(tab)
                ds_key = "" if tds is None else tds
                if (ds_key, name) not in self._published_tabs:
                    continue
                lst = tabs_by_dataset.setdefault(ds_key, [])
                if name not in lst:                     # DS-internal dedupe, window order
                    lst.append(name)

            # datasets = the guest's clickable DS chips. Computed by
            # _shareable_ds_keys so _on_new_session_request accepts exactly those
            # chips (Issue #81) — the two must never drift apart.
            datasets = self._shareable_ds_keys(pub)

            tabs_payload = {"active_dataset": cur, "datasets": datasets,
                            "tabs_by_dataset": tabs_by_dataset}
            tj = json.dumps(tabs_payload, sort_keys=True, ensure_ascii=False)
            if tj != self._last_tabs_json:
                self._last_tabs_json = tj
                self._worker.enqueue({"kind": "tabs", "data": tabs_payload})

            # Views (demand-driven, Issue #78): render the active DS always, plus
            # DSs a guest is currently watching (presence-derived, warm-graced),
            # capped at _MAX_WATCHED_RENDER (freshest-expiry first) so a crowd on
            # many DSs can't fan the host out to every open DS per tick. Cold DSs
            # get tab NAMES only — their view is skipped (guest sees a placeholder).
            if not hasattr(self, "_watched_ds_until"):
                self._watched_ds_until = {}
            now = time.monotonic()
            live = {ds for ds, until in self._watched_ds_until.items() if until > now}
            watched = sorted(
                live & set(datasets),
                key=lambda d: self._watched_ds_until[d], reverse=True,
            )[:_MAX_WATCHED_RENDER]
            render_ds = {active_key} | set(watched)
            for tab in win.tabs():
                name = getattr(tab, "name", None)
                if name is None:
                    continue
                tds = self._tab_dataset(tab)
                ds_key = "" if tds is None else tds
                if (ds_key, name) not in self._published_tabs:
                    continue
                if ds_key not in render_ds:
                    continue
                png = self._capture_tab(tab, (ds_key, name))
                if png is not None:
                    self._worker.enqueue({"kind": "view", "tab": name,
                                          "ds": ds_key, "png": png})
        except Exception as exc:
            _log.warning("capture tick skipped: %s", exc)

    def _capture_tab(self, tab, key: tuple[str, str]) -> bytes | None:
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
        if cache_key and self._view_cachekeys.get(key) == cache_key:
            return None
        digest = None
        try:
            img = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
            digest = _qimage_digest(img)
        except Exception:
            digest = None
        if digest is not None and self._view_hashes.get(key) == digest:
            self._view_cachekeys[key] = cache_key   # remember so we short-circuit next tick
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
                    self._view_cachekeys[key] = cache_key
                return None
        if digest is None:
            # constBits failed — fall back to hashing the PNG bytes (correctness
            # kept; only the no-encode fast path is lost).
            digest = hashlib.sha1(png).digest()
            if self._view_hashes.get(key) == digest:
                self._view_cachekeys[key] = cache_key
                return None
        self._view_hashes[key] = digest
        self._view_cachekeys[key] = cache_key
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
