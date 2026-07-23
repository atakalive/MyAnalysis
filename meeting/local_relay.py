"""In-memory local relay (Issue #44).

Re-implements every route of ``relay-worker/worker.js`` against a process-local,
``threading.Lock``-guarded in-memory store instead of Cloudflare KV/R2. The host
runs this on ``127.0.0.1:<ephemeral>`` and exposes it to guests through a
Tailscale Funnel (see ``meeting/tunnel.py``).

``relay-worker/worker.js`` documents the pre-#51 wire format: response shapes,
``server_now_ms``, the ``ts13-rand13`` mid format, and the ``{"error": "..."}``
error bodies originate there. Since the dataset layer (Issue #51 follow-up) the
protocol has DIVERGED on ``PUT /tabs`` (object body with ``active_dataset``),
``GET /poll`` (``active_dataset`` field) and ``GET /history`` (``has_more`` on
unpublished sids) — for those routes THIS module is the single source of truth;
worker.js is kept as a pre-DS reference only.

Two routes are local-relay-only extensions, NOT present in worker.js (the Worker
path is no longer in the data path — the runtime is this module, the HTML is
served by GitLab Pages + this server):
  PUT /backlog/{ch}/{sid}   (admin)  — stage a session's pre-meeting transcript
  GET /history/{ch}/{sid}   (guest)  — backward, turn-paginated history fetch
Together they let a guest fetch the full chat history (incl. before sharing
started), 5 turns at a time, on demand.

The routing core ``RelayState.handle()`` is socket-independent so tests can drive
it directly without a socket / cloudflared / network.
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import re
import secrets
import threading
import time
import urllib.parse
from pathlib import Path

from common.paths import repo_root

# Constants (same values as worker.js). BUCKET_MS / MAX_BUCKETS are intentionally
# absent: there is no KV list() mechanism, so no message bucketing is needed.
LOOKBACK_MS = 15000
PRES_TTL = 90          # presence entry liveness (seconds)
HB_GRACE = 120         # heartbeat liveness grace (seconds)
TTL_MIN = 3600         # channel ttl clamp min (1h)
TTL_MAX = 86400        # channel ttl clamp max (24h)
MSG_TTL = 86400        # message liveness (seconds) = 24h
MAX_NEW_SESSIONS = 20  # guest new-chat request queue cap (abuse backstop, Issue #81)

_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"

# Sentinel for "body was not valid JSON" — distinct from a valid JSON ``null``
# (which decodes to ``None``). worker.js: ``request.json()`` throws ONLY on invalid
# JSON (-> 400 "bad json"); a valid non-object body is accepted and field access
# on it yields ``undefined`` -> coerced defaults. We mirror that: parse failure ->
# ``_BAD`` (400), valid-but-non-dict -> ``{}`` so ``.get()`` yields defaults.
_BAD = object()

_CORS = {
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET, POST, PUT, DELETE, OPTIONS",
    "access-control-allow-headers": "authorization, content-type",
    "access-control-max-age": "86400",
}

_BEARER_RE = re.compile(r"^Bearer\s+(.+)$")


def now_sec() -> int:
    return int(time.time())


def now_ms() -> int:
    return int(time.time() * 1000)


def _b36(n: int) -> str:
    if n == 0:
        return "0"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(_B36[r])
    return "".join(reversed(out))


def gen_mid(now_ms_val: int) -> str:
    """worker.js genMid: "<ts13>-<rand13>", both fixed width (lexical=time order)."""
    rand = int.from_bytes(secrets.token_bytes(8), "big")
    rand13 = _b36(rand).rjust(13, "0")[-13:]
    return f"{now_ms_val:013d}-{rand13}"


def _header_get(headers, name: str) -> str:
    """Case-insensitive header lookup over a plain dict or a Message-like mapping."""
    get = getattr(headers, "get", None)
    if get is not None:
        # email.message.Message.get is already case-insensitive.
        val = headers.get(name)
        if val is not None:
            return val
        # Plain dict fallback: scan case-insensitively.
        low = name.lower()
        for k in headers:
            if isinstance(k, str) and k.lower() == low:
                v = headers[k]
                if v is not None:
                    return v
    return ""


class RelayState:
    """Socket-independent routing core. Drive via ``handle()``."""

    def __init__(self, admin_key: str, html_path: "Path | None" = None) -> None:
        self._admin = admin_key or ""
        self._html_path = html_path  # None -> repo_root()/relay-worker/chatdock.html
        self._lock = threading.Lock()
        self._ch: dict[str, dict] = {}

    # ---- time / since (worker.js parity) ----

    @staticmethod
    def compute_since(since: "str | None", now_ms_val: int) -> str:
        if not since or since == "RESET":
            floor = max(0, now_ms_val - LOOKBACK_MS)
            return f"{floor:013d}-{'0' * 13}"
        return since

    # ---- channel state ----

    def _ensure_channel(self, ch: str, now_sec_val: int) -> dict:
        cs = self._ch.setdefault(ch, {
            "meta": {},
            "hb": now_sec_val,
            "sessions": [],
            "tabs": [],        # legacy flat store (migrated to per-DS on upgrade)
            "dataset": None,   # active dataset the published tabs belong to (DS layer)
            "datasets": [],    # ordered DS keys ("" = null/dataset-less group) (Issue #78)
            "tabs_by_ds": {},  # ds-key -> [tab, ...]      (Issue #78)
            "views_by_ds": {}, # ds-key -> {tab: png}      (Issue #78)
            "vv_by_ds": {},    # ds-key -> {tab: version}  (Issue #78)
            "in_msgs": [],
            "out_msgs": [],
            "new_sessions": [],  # guest new-chat requests (Issue #81)
            "backlog": {},     # sid -> [msg, ...] pre-meeting transcript (no TTL compaction)
            "views": {},       # legacy flat store (migrated to per-DS on upgrade)
            "vv": {},          # legacy flat store (migrated to per-DS on upgrade)
            "presence": {},
        })
        self._upgrade_channel(cs)
        return cs

    @staticmethod
    def _upgrade_channel(cs: dict) -> None:
        """Idempotently ensure a channel carries the per-DS namespace keys and,
        once, migrate any legacy flat ``tabs``/``views``/``vv`` into the active
        DS's slice (Issue #78). A mid-meeting host hot-reload leaves existing
        channel dicts (created by pre-#78 code) without the per-DS keys, so both
        write and read paths call this after fetching ``cs``.
        """
        cs.setdefault("datasets", [])
        cs.setdefault("tabs_by_ds", {})
        cs.setdefault("views_by_ds", {})
        cs.setdefault("vv_by_ds", {})
        # One-time flat→per-DS migration: only when legacy flat state exists AND
        # the per-DS store is still empty. After migrating we blank the flat store
        # so this guard can never fire twice (post-#78 nothing writes flat).
        legacy = cs.get("tabs") or cs.get("views") or cs.get("vv")
        if legacy and not cs["tabs_by_ds"] and not cs["views_by_ds"] and not cs["vv_by_ds"]:
            key = cs.get("dataset") or ""
            cs["tabs_by_ds"][key] = list(cs.get("tabs") or [])
            cs["views_by_ds"][key] = dict(cs.get("views") or {})
            cs["vv_by_ds"][key] = dict(cs.get("vv") or {})
            if key not in cs["datasets"]:
                cs["datasets"].append(key)
            cs["tabs"] = []
            cs["views"] = {}
            cs["vv"] = {}

    @staticmethod
    def _ds_key(cs: dict, ds_raw: "str | None") -> str:
        """Canonicalise a `ds` query value to a per-DS store key (Issue #78).

        ``ds_raw`` is ``None`` (absent → host-active slice), ``""`` (explicit null
        group), or a dataset name. Absent resolves to the active dataset's key
        (``""`` when the active dataset is the null group)."""
        return (cs.get("dataset") or "") if ds_raw is None else ds_raw

    def _session_scope_ok(self, cs: dict, sid: str, ds_raw: "str | None") -> bool:
        """True iff ``sid`` is published AND bound to the caller's DS scope
        (Issue #78). ``ds`` absent (legacy guest) → the active dataset only;
        ``ds`` present (new guest) → the sessions whose ``dataset`` equals ``ds``.
        This keeps a legacy guest a true presenter-mirror (no background-DS chat
        exposure) across messages, /msg, and /history."""
        scope = self._ds_key(cs, ds_raw)
        sess = next((s for s in cs["sessions"] if s.get("id") == sid), None)
        if sess is None:
            return False
        return ("" if sess.get("dataset") is None else sess.get("dataset")) == scope

    def _evict_expired_channels(self, now_sec_val: int) -> None:
        # Reap (a) meta'd channels past expires_at and (b) metadata-less channels
        # whose hb is HB_GRACE stale. (b) covers the abandoned-channel case: a stray
        # admin write (or pre-meta GET) can resurrect a popped channel with empty
        # meta; a normal channel always sets meta right after POST /admin/channel, so
        # an empty-meta + stale-hb channel is an orphan and safe to reap. Without (b)
        # such channels would never be evicted (empty meta is falsy). (reviewer P2)
        for ch in list(self._ch):
            cs = self._ch[ch]
            meta = cs.get("meta")
            expired = bool(meta) and now_sec_val >= meta.get("expires_at", 0)
            orphan = not meta and now_sec_val - cs.get("hb", 0) >= HB_GRACE
            if expired or orphan:
                del self._ch[ch]

    def _scan(self, msgs: list, since_cmp: str, now_ms_val: int, sid: "str | None") -> list:
        ttl_cmp = f"{max(0, now_ms_val - MSG_TTL * 1000):013d}-{'0' * 13}"
        msgs[:] = [m for m in msgs if m["mid"] >= ttl_cmp]   # in-place compaction
        out = [m for m in msgs if m["mid"] >= since_cmp and (sid is None or m["sid"] == sid)]
        out.sort(key=lambda m: m["mid"])
        return out

    # ---- auth helpers (worker.js parity) ----

    def _bearer(self, headers) -> str:
        m = _BEARER_RE.match(_header_get(headers, "Authorization") or "")
        return m.group(1) if m else ""

    def _is_admin(self, headers) -> bool:
        return bool(self._admin) and hmac.compare_digest(self._bearer(headers), self._admin)

    @staticmethod
    def _sha256hex(s: str) -> str:
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    def _check_guest(self, headers, ch: str, now_sec_val: int) -> "tuple[bool, int]":
        cs = self._ch.get(ch)
        if cs is None:
            return (False, 410)
        meta = cs.get("meta")
        if not meta:
            return (False, 410)
        bearer = self._bearer(headers)
        if not hmac.compare_digest(self._sha256hex(bearer), meta.get("secret_hash", "")):
            return (False, 401)
        if now_sec_val >= meta.get("expires_at", 0):
            return (False, 410)
        if now_sec_val - cs.get("hb", 0) >= HB_GRACE:
            return (False, 503)
        return (True, 200)

    # ---- responses ----

    @staticmethod
    def _json(obj, status: int = 200) -> "tuple[int, dict, bytes]":
        headers = {"content-type": "application/json; charset=utf-8", **_CORS}
        return (status, headers, json.dumps(obj).encode("utf-8"))

    # ---- routing ----

    def handle(self, method: str, path: str, headers, body: bytes) -> "tuple[int, dict, bytes]":
        # CORS preflight: answer before any routing/auth.
        if method == "OPTIONS":
            return (204, dict(_CORS), b"")

        split = urllib.parse.urlsplit(path)
        # keep_blank_values: an empty `?ds=` must survive as "" (the null/dataset-
        # less group key), distinct from an absent `ds` (None → host-active). The
        # default drops blank values, collapsing "absent" and "empty" to the same
        # None and making the null group un-addressable. Only `ds` distinguishes
        # the two; every other query consumer coerces "" to its default anyway.
        query = urllib.parse.parse_qs(split.query, keep_blank_values=True)
        seg = [urllib.parse.unquote(s) for s in split.path.split("/") if s]

        # GET / -> serve the guest HTML (file I/O outside the lock).
        if len(seg) == 0 and method == "GET":
            return self._serve_html()

        now_s = now_sec()
        now_m = now_ms()

        with self._lock:
            self._evict_expired_channels(now_s)
            return self._route(method, seg, query, headers, body, now_s, now_m)

    def _route(self, method, seg, query, headers, body, now_s, now_m):
        # ---- host (admin) routes ----
        if seg[:2] == ["admin", "channel"]:
            if method == "POST" and len(seg) == 2:
                if not self._is_admin(headers):
                    return self._json({"error": "unauthorized"}, 401)
                obj, err = self._json_obj(body)
                if err:
                    return err
                ch = str(obj.get("ch") or "")
                if not ch:
                    return self._json({"error": "missing ch"}, 400)
                try:
                    ttl_raw = int(obj.get("ttl_sec", TTL_MIN))
                except (TypeError, ValueError):
                    ttl_raw = TTL_MIN
                ttl = min(TTL_MAX, max(TTL_MIN, ttl_raw))
                cs = self._ensure_channel(ch, now_s)
                expires_at = now_s + ttl
                cs["meta"] = {"secret_hash": str(obj.get("secret_hash") or ""),
                              "expires_at": expires_at}
                cs["hb"] = now_s
                return self._json({"expires_at": expires_at, "server_now_ms": now_m})
            if method == "DELETE" and len(seg) == 3:
                if not self._is_admin(headers):
                    return self._json({"error": "unauthorized"}, 401)
                self._ch.pop(seg[2], None)
                return self._json({"ok": True})

        if seg[:1] == ["sessions"] and len(seg) == 2 and method == "PUT":
            if not self._is_admin(headers):
                return self._json({"error": "unauthorized"}, 401)
            obj = self._parse_json(body)
            if obj is _BAD:
                return self._json({"error": "bad json"}, 400)
            cs = self._ensure_channel(seg[1], now_s)
            cs["sessions"] = obj if isinstance(obj, list) else []
            return self._json({"ok": True})

        if seg[:1] == ["tabs"] and len(seg) == 2 and method == "PUT":
            if not self._is_admin(headers):
                return self._json({"error": "unauthorized"}, 401)
            # _parse_json (not _json_obj): a pre-DS host sends a bare name list,
            # which must stay accepted as active_dataset=None — _json_obj would
            # coerce it to {} and silently unpublish every tab.
            obj = self._parse_json(body)
            if obj is _BAD:
                return self._json({"error": "bad json"}, 400)
            cs = self._ensure_channel(seg[1], now_s)
            if isinstance(obj, dict) and isinstance(obj.get("tabs_by_dataset"), dict):
                # New multi-DS shape: full per-DS namespace in one PUT (Issue #78).
                tbd = obj["tabs_by_dataset"]
                cs["tabs_by_ds"] = {
                    str(k): [str(t) for t in v]
                    for k, v in tbd.items() if isinstance(v, list)
                }
                raw_ds = obj.get("datasets")
                cs["datasets"] = (
                    [str(d) for d in raw_ds] if isinstance(raw_ds, list)
                    else list(cs["tabs_by_ds"])
                )
                active = obj.get("active_dataset")
                cs["dataset"] = active if isinstance(active, str) else None
            else:
                # Legacy dict{active_dataset, tabs} OR bare-list (pre-DS host):
                # a single-DS publish keyed by the active dataset (None → "").
                active = obj.get("active_dataset") if isinstance(obj, dict) else None
                active = active if isinstance(active, str) else None
                raw = obj.get("tabs") if isinstance(obj, dict) else obj
                tabs = [str(t) for t in raw] if isinstance(raw, list) else []
                key = active if isinstance(active, str) else ""
                cs["tabs_by_ds"][key] = tabs
                cs["datasets"] = [key]
                cs["dataset"] = active
            # Prune slices for datasets no longer published (replaces the old
            # dataset-switch view wipe). Walk the UNION of all three per-DS dicts
            # so a DS that opted every tab out but still has a lingering view/vv
            # slice is cleaned too. Open-but-unpublished DSs (empty slice present
            # in `datasets`) are kept.
            for k in (set(cs["tabs_by_ds"]) | set(cs["views_by_ds"]) | set(cs["vv_by_ds"])):
                if k not in cs["datasets"]:
                    cs["tabs_by_ds"].pop(k, None)
                    cs["views_by_ds"].pop(k, None)
                    cs["vv_by_ds"].pop(k, None)
            return self._json({"ok": True})

        if seg[:1] == ["heartbeat"] and len(seg) == 2 and method == "PUT":
            if not self._is_admin(headers):
                return self._json({"error": "unauthorized"}, 401)
            cs = self._ensure_channel(seg[1], now_s)
            cs["hb"] = now_s
            return self._json({"ok": True})

        # POST /out/{ch}/{sid} (host, admin)
        if seg[:1] == ["out"] and len(seg) == 3 and method == "POST":
            if not self._is_admin(headers):
                return self._json({"error": "unauthorized"}, 401)
            ch, sid = seg[1], seg[2]
            obj, err = self._json_obj(body)
            if err:
                return err
            text = str(obj.get("text") or "")
            if not text.strip():
                return self._json({"error": "empty text"}, 400)
            cs = self._ensure_channel(ch, now_s)
            mid = gen_mid(now_m)
            # Streaming (guest live partials): a message carrying a stream_id is the
            # in-flight assistant turn. Re-mint its mid on every update so it stays
            # inside the lookback window (the since-floor keeps advancing) and
            # re-sorts to the bottom, and replace the SAME entry in place so
            # out_msgs stays one-per-turn. The final post (partial=False) overwrites
            # the same entry with the complete text. No stream_id -> legacy append.
            stream_id = str(obj.get("stream_id") or "")
            partial = bool(obj.get("partial"))
            if stream_id:
                existing = next(
                    (m for m in cs["out_msgs"]
                     if m.get("stream_id") == stream_id and m["sid"] == sid),
                    None,
                )
                if existing is not None:
                    existing["text"] = text
                    existing["name"] = str(obj.get("name") or "")
                    existing["mid"] = mid
                    existing["partial"] = partial
                    return self._json({"mid": mid})
                cs["out_msgs"].append({
                    "text": text, "name": str(obj.get("name") or ""),
                    "role": str(obj.get("role") or "assistant"),
                    "origin": "host", "mid": mid, "sid": sid,
                    "stream_id": stream_id, "partial": partial,
                })
                return self._json({"mid": mid})
            cs["out_msgs"].append({
                "text": text, "name": str(obj.get("name") or ""),
                "role": str(obj.get("role") or "assistant"),
                "origin": "host", "mid": mid, "sid": sid,
            })
            return self._json({"mid": mid})

        # PUT /backlog/{ch}/{sid} (host, admin) — full pre-meeting transcript for a
        # session, served only via GET /history (never /poll). Replaces any prior
        # backlog for sid (idempotent). Order-only mids "{0:013d}-{i:013d}" sort
        # strictly before every live mid (ts13 > 0) and are exempt from MSG_TTL
        # compaction (a separate list, never passed through _scan).
        if seg[:1] == ["backlog"] and len(seg) == 3 and method == "PUT":
            if not self._is_admin(headers):
                return self._json({"error": "unauthorized"}, 401)
            ch, sid = seg[1], seg[2]
            arr = self._parse_json(body)
            if arr is _BAD:
                return self._json({"error": "bad json"}, 400)
            cs = self._ensure_channel(ch, now_s)
            items = arr if isinstance(arr, list) else []
            backlog = []
            for it in items:
                d = it if isinstance(it, dict) else {}
                text = str(d.get("text") or "")
                if not text.strip():
                    continue
                backlog.append({
                    "text": text, "name": str(d.get("name") or ""),
                    "role": str(d.get("role") or "assistant"),
                    "origin": "host", "mid": f"{0:013d}-{len(backlog):013d}", "sid": sid,
                })
            # setdefault (not cs["backlog"]) so a channel created before "backlog"
            # was added to _ensure_channel (e.g. after a mid-meeting hot-reload) is
            # upgraded in place instead of raising KeyError.
            cs.setdefault("backlog", {})[sid] = backlog
            return self._json({"ok": True, "count": len(backlog)})

        # GET /inbound/{ch}?since= (host) — in: only, all sid.
        # Read routes use _ch.get (NOT _ensure_channel): worker.js never creates KV
        # on a read, and creating an empty channel here would leak orphans. (reviewer P2)
        if seg[:1] == ["inbound"] and len(seg) == 2 and method == "GET":
            if not self._is_admin(headers):
                return self._json({"error": "unauthorized"}, 401)
            cs = self._ch.get(seg[1])
            since = self.compute_since(self._q1(query, "since"), now_m)
            messages = self._scan(cs["in_msgs"], since, now_m, None) if cs else []
            return self._json({"messages": messages, "server_now_ms": now_m})

        # GET /newsessions/{ch}?since= (host) — drain guest new-chat requests
        # (Issue #81). Symmetric to GET /inbound: _ch.get (never _ensure_channel) so
        # a read can't resurrect an evicted channel. _scan is called with sid=None,
        # which short-circuits before touching m["sid"] — queue items carry no sid.
        if seg[:1] == ["newsessions"] and len(seg) == 2 and method == "GET":
            if not self._is_admin(headers):
                return self._json({"error": "unauthorized"}, 401)
            cs = self._ch.get(seg[1])
            since = self.compute_since(self._q1(query, "since"), now_m)
            requests = (
                self._scan(cs.setdefault("new_sessions", []), since, now_m, None)
                if cs else []
            )
            return self._json({"requests": requests, "server_now_ms": now_m})

        # GET /presence/{ch} (host) — bare array.
        if seg[:1] == ["presence"] and len(seg) == 2 and method == "GET":
            if not self._is_admin(headers):
                return self._json({"error": "unauthorized"}, 401)
            cs = self._ch.get(seg[1])
            if cs is None:
                return self._json([])
            self._evict_presence(cs, now_s)
            return self._json(list(cs["presence"].values()))

        # ---- guest (secret) routes ----

        # POST /msg/{ch}/{sid} (guest)
        if seg[:1] == ["msg"] and len(seg) == 3 and method == "POST":
            ch, sid = seg[1], seg[2]
            ok, status = self._check_guest(headers, ch, now_s)
            if not ok:
                return self._json({"error": "not live"}, status)
            cs = self._ch[ch]
            if not self._session_scope_ok(cs, sid, self._q1(query, "ds")):
                return self._json({"error": "session not in scope"}, 403)
            obj, err = self._json_obj(body)
            if err:
                return err
            text = str(obj.get("text") or "")
            if not text.strip():
                return self._json({"error": "empty text"}, 400)
            name = str(obj.get("name") or "").strip() or "Guest"
            mid = gen_mid(now_m)
            cs["in_msgs"].append({
                "text": text, "name": name, "role": "user",
                "origin": "guest", "mid": mid, "sid": sid,
            })
            return self._json({"mid": mid})

        # POST /newsession/{ch}?ds= (guest) — ask the HOST to mint a new chat
        # session in `ds`. The guest never supplies a session id: write_session_file
        # writes <work_dir>/chat_sessions/<id>.json, so a guest-chosen id would be a
        # path-traversal vector — the host mints a uuid4, publishes it, and the guest
        # discovers it by diffing the published session list (Issue #81).
        # The singular/plural split is deliberate: POST /newsession enqueues one,
        # GET /newsessions (admin) drains the queue. `ds` is canonicalised but NOT
        # validated here — only the host knows which datasets are live.
        if seg[:1] == ["newsession"] and len(seg) == 2 and method == "POST":
            ch = seg[1]
            ok, status = self._check_guest(headers, ch, now_s)
            if not ok:
                return self._json({"error": "not live"}, status)
            cs = self._ch[ch]
            obj, err = self._json_obj(body)
            if err:
                return err
            name = str(obj.get("name") or "").strip() or "Guest"
            # _ds_key canonicalises here (NOT host-side): absent `ds` → the host's
            # active DS key, "" stays the null group. Keeping the wire value a plain
            # str is what lets the worker's new signal be Signal(str, str).
            ds = self._ds_key(cs, self._q1(query, "ds"))
            mid = gen_mid(now_m)
            # setdefault (not cs["new_sessions"]): a channel created by pre-#81 code
            # before a mid-meeting hot-reload lacks the key (mirrors "backlog").
            q = cs.setdefault("new_sessions", [])
            q.append({"ds": ds, "name": name, "mid": mid})
            if len(q) > MAX_NEW_SESSIONS:
                del q[:len(q) - MAX_NEW_SESSIONS]   # drop oldest — abuse backstop
            return self._json({"ok": True, "mid": mid})

        # POST /presence/{ch} (guest)
        if seg[:1] == ["presence"] and len(seg) == 2 and method == "POST":
            ch = seg[1]
            ok, status = self._check_guest(headers, ch, now_s)
            if not ok:
                return self._json({"error": "not live"}, status)
            cs = self._ch[ch]
            obj, err = self._json_obj(body)
            if err:
                return err
            pid = str(obj.get("pid") or "")
            if not pid:
                return self._json({"error": "missing pid"}, 400)
            entry = {
                "pid": pid,
                "name": str(obj.get("name") or "").strip() or "Guest",
                "sid": str(obj.get("sid") or ""),
                "tab": str(obj.get("tab") or ""),
                "last": now_s,
            }
            # Only record `ds` when the guest actually sent it: a bootstrap/legacy
            # guest omits it, and the host's watched-DS derivation keys on "ds" in
            # p — an absent key must not be mistaken for the null group ("") (Issue #78).
            if "ds" in obj:
                entry["ds"] = str(obj.get("ds") or "")
            cs["presence"][pid] = entry
            return self._json({"ok": True})

        # PUT/GET /view/{ch}/{tab}?ds=
        if seg[:1] == ["view"] and len(seg) == 3:
            ch, tab = seg[1], seg[2]
            if method == "PUT":
                if not self._is_admin(headers):
                    return self._json({"error": "unauthorized"}, 401)
                cs = self._ensure_channel(ch, now_s)
                ds = self._ds_key(cs, self._q1(query, "ds"))
                if tab not in cs["tabs_by_ds"].get(ds, []):
                    return self._json({"error": "tab not published"}, 403)
                cs["views_by_ds"].setdefault(ds, {})[tab] = body
                cs["vv_by_ds"].setdefault(ds, {})[tab] = now_m
                return self._json({"ok": True})
            if method == "GET":
                ok, status = self._check_guest(headers, ch, now_s)
                if not ok:
                    return self._json({"error": "not live"}, status)
                cs = self._ch[ch]
                self._upgrade_channel(cs)
                ds = self._ds_key(cs, self._q1(query, "ds"))
                if tab not in cs["tabs_by_ds"].get(ds, []):
                    return self._json({"error": "tab not published"}, 403)
                png = cs["views_by_ds"].get(ds, {}).get(tab)
                if not png:
                    return (204, dict(_CORS), b"")
                return (200, {"content-type": "image/png", **_CORS}, png)

        # GET /poll/{ch}/{sid}?since= (guest) — unified poll.
        if seg[:1] == ["poll"] and len(seg) == 3 and method == "GET":
            ch, sid = seg[1], seg[2]
            ok, status = self._check_guest(headers, ch, now_s)
            if not ok:
                return self._json({"error": "not live"}, status)
            cs = self._ch[ch]
            self._upgrade_channel(cs)
            ds_raw = self._q1(query, "ds")
            ds = self._ds_key(cs, ds_raw)
            messages = []
            if self._session_scope_ok(cs, sid, ds_raw):
                sc = self.compute_since(self._q1(query, "since"), now_m)
                messages = sorted(
                    self._scan(cs["in_msgs"], sc, now_m, sid)
                    + self._scan(cs["out_msgs"], sc, now_m, sid),
                    key=lambda m: m["mid"],
                )
            # ds absent (legacy guest, no client-side DS filter) → only the active
            # dataset's sessions (true presenter-mirror). ds present (new guest) →
            # the flat all-DS session list, filtered client-side by curDs.
            sessions_out = (
                cs["sessions"] if ds_raw is not None
                else [s for s in cs["sessions"] if s.get("dataset") == cs.get("dataset")]
            )
            return self._json({
                "messages": messages, "sessions": sessions_out,
                "tabs": cs["tabs_by_ds"].get(ds, []),
                "active_dataset": cs.get("dataset"),
                "datasets": list(cs["datasets"]),
                "view_versions": dict(cs["vv_by_ds"].get(ds, {})),
                "alive": True, "server_now_ms": now_m,
            })

        # GET /history/{ch}/{sid}?before=&turns= (guest) — backward pagination.
        # Returns up to `turns` (default 5) conversation turns strictly older than
        # `before`, drawn from backlog + live (in/out) for sid. A turn boundary is a
        # user-role message (a user prompt + its following assistant replies).
        if seg[:1] == ["history"] and len(seg) == 3 and method == "GET":
            ch, sid = seg[1], seg[2]
            ok, status = self._check_guest(headers, ch, now_s)
            if not ok:
                return self._json({"error": "not live"}, status)
            cs = self._ch[ch]
            if not self._session_scope_ok(cs, sid, self._q1(query, "ds")):
                # has_more=True, not False: a dataset switch swaps the published
                # session set (or the sid is out of the caller's DS scope), and an
                # in-flight history fetch landing here would otherwise make the
                # guest latch histExhausted permanently (the backlog is still
                # staged — a retry after switch-back succeeds).
                return self._json({"messages": [], "has_more": True, "server_now_ms": now_m})
            try:
                turns = int(self._q1(query, "turns") or 5)
            except (TypeError, ValueError):
                turns = 5
            turns = max(1, min(50, turns))
            page, has_more = self._history_page(cs, sid, self._q1(query, "before"), turns)
            return self._json({"messages": page, "has_more": has_more, "server_now_ms": now_m})

        return self._json({"error": "not found"}, 404)

    def _history_page(self, cs: dict, sid: str, before: "str | None", turns: int):
        """Last `turns` turns older than `before` (exclusive) for sid, with has_more.

        Turns are delimited by user-role messages: the page starts at the
        `turns`-th-newest user message so each user prompt is grouped with the
        assistant replies that follow it. Fewer than `turns` users -> whole set."""
        full = (
            list(cs.get("backlog", {}).get(sid, []))
            + [m for m in cs["in_msgs"] if m["sid"] == sid]
            + [m for m in cs["out_msgs"] if m["sid"] == sid]
        )
        full.sort(key=lambda m: m["mid"])
        older = [m for m in full if m["mid"] < before] if before else full
        user_idx = [i for i, m in enumerate(older) if m.get("role") == "user"]
        start = 0 if len(user_idx) <= turns else user_idx[-turns]
        return older[start:], start > 0

    # ---- helpers ----

    @staticmethod
    def _q1(query: dict, key: str) -> "str | None":
        vals = query.get(key)
        return vals[0] if vals else None

    @staticmethod
    def _parse_json(body: bytes):
        """Decode a JSON body. Returns ``_BAD`` on parse failure (incl. empty body,
        which ``request.json()`` also rejects), else the decoded value (which may be
        ``None``/list/dict/number/str — callers coerce per route)."""
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return _BAD

    def _json_obj(self, body: bytes):
        """Parse a JSON *object* body for routes that read fields via ``.get()``.

        Returns ``(obj, None)`` on success, or ``(None, error_response)`` on parse
        failure. A valid-but-non-dict body (null/number/array/string) is coerced to
        ``{}`` so field access yields defaults — matching worker.js field access on a
        non-object (undefined -> default), which 400s on the missing field, never 500s.
        """
        obj = self._parse_json(body)
        if obj is _BAD:
            return None, self._json({"error": "bad json"}, 400)
        return (obj if isinstance(obj, dict) else {}), None

    @staticmethod
    def _evict_presence(cs: dict, now_sec_val: int) -> None:
        pres = cs["presence"]
        for pid in list(pres):
            if pres[pid].get("last", 0) + PRES_TTL < now_sec_val:
                del pres[pid]

    def _serve_html(self) -> "tuple[int, dict, bytes]":
        html_path = self._html_path or (repo_root() / "relay-worker" / "chatdock.html")
        try:
            data = Path(html_path).read_bytes()
        except Exception:
            return self._json({"error": "chatdock unavailable"}, 500)
        return (200, {"content-type": "text/html; charset=utf-8", **_CORS}, data)


class LocalRelayServer:
    def __init__(self, httpd: "http.server.ThreadingHTTPServer", state: RelayState,
                 thread: threading.Thread) -> None:
        self._httpd = httpd
        self.state = state
        self._thread = thread
        self.port = httpd.server_address[1]
        self._closed = False

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._httpd.shutdown()
        finally:
            self._httpd.server_close()


def start_server(admin_key: str, *, html_path: "Path | None" = None) -> LocalRelayServer:
    state = RelayState(admin_key, html_path=html_path)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: N802 — suppress access log
            pass

        def _dispatch(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            try:
                body = self.rfile.read(length) if length > 0 else b""
                status, headers, payload = state.handle(
                    self.command, self.path, self.headers, body)
            except Exception:
                status, headers, payload = 500, dict(_CORS), b""
            try:
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)
            except Exception:
                pass

        do_GET = _dispatch
        do_POST = _dispatch
        do_PUT = _dispatch
        do_DELETE = _dispatch
        do_OPTIONS = _dispatch

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return LocalRelayServer(httpd, state, thread)
