"""Hermetic tests for the in-memory local relay (Issue #44).

Drives ``RelayState.handle()`` directly — no socket, cloudflared, or network.
Time-dependent assertions monkeypatch ``local_relay.time.time`` to freeze /
advance the clock so mids, expiry, hb, presence evict, and TTL compaction are
deterministic.
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest

import meeting.local_relay as lr
from meeting.local_relay import RelayState

ADMIN = "ADMIN_KEY"
SECRET = "guest-secret"
SECRET_HASH = hashlib.sha256(SECRET.encode("utf-8")).hexdigest()


@pytest.fixture()
def clock(monkeypatch):
    state = {"t": 1_000_000.0}
    monkeypatch.setattr(lr.time, "time", lambda: state["t"])
    return state


def _admin_h():
    return {"Authorization": "Bearer " + ADMIN}


def _guest_h():
    return {"Authorization": "Bearer " + SECRET}


def _jbody(obj):
    return json.dumps(obj).encode("utf-8")


def _call(st, method, path, headers=None, body=b""):
    status, hdrs, payload = st.handle(method, path, headers or {}, body)
    return status, hdrs, payload


def _json(payload):
    return json.loads(payload.decode("utf-8"))


def _new_channel(st, ch="ch1", ttl=3600):
    return _call(st, "POST", "/admin/channel", _admin_h(),
                 _jbody({"ch": ch, "ttl_sec": ttl, "secret_hash": SECRET_HASH}))


def _publish_session(st, ch="ch1", sid="s1"):
    _call(st, "PUT", f"/sessions/{ch}", _admin_h(), _jbody([{"id": sid, "title": "T", "busy": False}]))


def _publish_tab(st, ch="ch1", tab="t1"):
    _call(st, "PUT", f"/tabs/{ch}", _admin_h(), _jbody([tab]))


# 1. admin auth
def test_admin_auth(clock):
    st = RelayState(ADMIN)
    status, _, _ = _call(st, "POST", "/admin/channel", {}, _jbody({"ch": "c"}))
    assert status == 401
    status, _, payload = _new_channel(st)
    assert status == 200
    obj = _json(payload)
    assert "expires_at" in obj and "server_now_ms" in obj


# 2. ttl clamp
def test_ttl_clamp(clock):
    st = RelayState(ADMIN)
    _, _, p = _new_channel(st, ch="lo", ttl=10)
    assert _json(p)["expires_at"] - int(clock["t"]) == lr.TTL_MIN
    _, _, p = _new_channel(st, ch="hi", ttl=999999)
    assert _json(p)["expires_at"] - int(clock["t"]) == lr.TTL_MAX


# 3. sessions/tabs PUT
def test_sessions_tabs_put(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    status, _, p = _call(st, "PUT", "/sessions/ch1", _admin_h(), _jbody([{"id": "s1"}]))
    assert status == 200 and _json(p) == {"ok": True}
    assert st._ch["ch1"]["sessions"] == [{"id": "s1"}]
    _call(st, "PUT", "/tabs/ch1", _admin_h(), _jbody(["t1"]))
    assert st._ch["ch1"]["tabs"] == ["t1"]


# 4. out → poll, not inbound (echo gate)
def test_out_to_poll_not_inbound(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    _publish_session(st)
    status, _, p = _call(st, "POST", "/out/ch1/s1", _admin_h(), _jbody({"text": "hello"}))
    assert status == 200
    mid = _json(p)["mid"]
    assert re.match(r"^\d{13}-.{13}$", mid)

    _, _, pp = _call(st, "GET", "/poll/ch1/s1", _guest_h())
    assert any(m["text"] == "hello" for m in _json(pp)["messages"])

    _, _, ib = _call(st, "GET", "/inbound/ch1", _admin_h())
    assert all(m["text"] != "hello" for m in _json(ib)["messages"])


# 4b. streaming /out: stream_id replaces the in-flight entry in place + re-mints mid
def test_out_streaming_upsert_remint(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    _publish_session(st)

    def _out(text, partial, stream="S"):
        _, _, p = _call(st, "POST", "/out/ch1/s1", _admin_h(),
                        _jbody({"text": text, "role": "assistant",
                                "stream_id": stream, "partial": partial}))
        return _json(p)["mid"]

    # first partial creates a single in-flight entry
    mid1 = _out("Hel", True)
    out = st._ch["ch1"]["out_msgs"]
    assert len(out) == 1
    assert out[0]["text"] == "Hel" and out[0]["stream_id"] == "S" and out[0]["partial"] is True

    # second partial replaces in place and re-mints the mid (sorts after mid1)
    clock["t"] += 1.0
    mid2 = _out("Hello wor", True)
    assert mid2 > mid1
    assert len(st._ch["ch1"]["out_msgs"]) == 1
    assert st._ch["ch1"]["out_msgs"][0]["text"] == "Hello wor"

    # final (partial=False) overwrites the same entry with the complete text
    clock["t"] += 1.0
    midf = _out("Hello world", False)
    assert midf > mid2
    out = st._ch["ch1"]["out_msgs"]
    assert len(out) == 1 and out[0]["text"] == "Hello world" and out[0]["partial"] is False

    # guest poll returns exactly one message carrying the final text + stream fields
    _, _, pp = _call(st, "GET", "/poll/ch1/s1", _guest_h())
    msgs = _json(pp)["messages"]
    assert len(msgs) == 1
    assert msgs[0]["text"] == "Hello world"
    assert msgs[0]["stream_id"] == "S" and msgs[0]["partial"] is False

    # a different stream_id is an independent in-flight entry (no collision)
    clock["t"] += 1.0
    _out("next", True, stream="S2")
    assert len(st._ch["ch1"]["out_msgs"]) == 2


# 5. msg → inbound
def test_msg_to_inbound(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    _publish_session(st)
    status, _, p = _call(st, "POST", "/msg/ch1/s1", _guest_h(), _jbody({"text": "hi", "name": "Bob"}))
    assert status == 200
    _, _, ib = _call(st, "GET", "/inbound/ch1", _admin_h())
    msgs = _json(ib)["messages"]
    assert any(m["text"] == "hi" and m["name"] == "Bob" for m in msgs)


# 6. since is inclusive
def test_since_inclusive(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    _publish_session(st)
    clock["t"] += 1
    _call(st, "POST", "/msg/ch1/s1", _guest_h(), _jbody({"text": "m1"}))
    clock["t"] += 1
    _call(st, "POST", "/msg/ch1/s1", _guest_h(), _jbody({"text": "m2"}))
    _, _, ib = _call(st, "GET", "/inbound/ch1", _admin_h())
    msgs = _json(ib)["messages"]
    mid1 = next(m["mid"] for m in msgs if m["text"] == "m1")
    mid2 = next(m["mid"] for m in msgs if m["text"] == "m2")

    _, _, r1 = _call(st, "GET", f"/inbound/ch1?since={mid1}", _admin_h())
    texts1 = {m["text"] for m in _json(r1)["messages"]}
    assert texts1 == {"m1", "m2"}

    _, _, r2 = _call(st, "GET", f"/inbound/ch1?since={mid2}", _admin_h())
    texts2 = {m["text"] for m in _json(r2)["messages"]}
    assert texts2 == {"m2"}


# 7. msg sid whitelist + auth errors
def test_msg_sid_whitelist(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    # not published → 403
    status, _, p = _call(st, "POST", "/msg/ch1/s1", _guest_h(), _jbody({"text": "x"}))
    assert status == 403 and _json(p) == {"error": "session not published"}
    _publish_session(st)
    status, _, _ = _call(st, "POST", "/msg/ch1/s1", _guest_h(), _jbody({"text": "x"}))
    assert status == 200
    # secret mismatch → 401
    status, _, p = _call(st, "POST", "/msg/ch1/s1", {"Authorization": "Bearer wrong"}, _jbody({"text": "x"}))
    assert status == 401 and _json(p) == {"error": "not live"}
    # missing channel → 410
    status, _, p = _call(st, "POST", "/msg/none/s1", _guest_h(), _jbody({"text": "x"}))
    assert status == 410 and _json(p) == {"error": "not live"}


# 8. poll unified shape
def test_poll_shape(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    _publish_session(st)
    _publish_tab(st)
    _, _, p = _call(st, "GET", "/poll/ch1/s1", _guest_h())
    obj = _json(p)
    assert set(obj) == {"messages", "sessions", "tabs", "view_versions", "alive", "server_now_ms"}
    assert obj["alive"] is True

    # un-published sid → empty messages but sessions/tabs still flow
    _, _, p2 = _call(st, "GET", "/poll/ch1/other", _guest_h())
    obj2 = _json(p2)
    assert obj2["messages"] == []
    assert obj2["tabs"] == ["t1"]


# 8b. poll global mid order + compaction
def test_poll_global_order_and_compaction(clock, monkeypatch):
    st = RelayState(ADMIN)
    _new_channel(st)
    _publish_session(st)
    # inject one already-stale message into each list (older than MSG_TTL)
    now_m = lr.now_ms()
    stale_mid = f"{max(0, now_m - lr.MSG_TTL * 1000 - 10000):013d}-{'0' * 13}"
    st._ch["ch1"]["in_msgs"].append({"text": "stale", "name": "", "role": "user",
                                     "origin": "guest", "mid": stale_mid, "sid": "s1"})
    st._ch["ch1"]["out_msgs"].append({"text": "stale", "name": "", "role": "assistant",
                                      "origin": "host", "mid": stale_mid, "sid": "s1"})
    clock["t"] += 1
    _call(st, "POST", "/out/ch1/s1", _admin_h(), _jbody({"text": "o1"}))
    clock["t"] += 1
    _call(st, "POST", "/msg/ch1/s1", _guest_h(), _jbody({"text": "m1"}))
    clock["t"] += 1
    _call(st, "POST", "/out/ch1/s1", _admin_h(), _jbody({"text": "o2"}))

    _, _, p = _call(st, "GET", "/poll/ch1/s1", _guest_h())
    texts = [m["text"] for m in _json(p)["messages"]]
    assert texts == ["o1", "m1", "o2"]
    # both lists compacted in place (stale dropped)
    assert all(m["mid"] != stale_mid for m in st._ch["ch1"]["in_msgs"])
    assert all(m["mid"] != stale_mid for m in st._ch["ch1"]["out_msgs"])


# 9. view put/get
def test_view_put_get(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    # tab not published → 403
    status, _, _ = _call(st, "PUT", "/view/ch1/t1", _admin_h(), b"\x89PNG")
    assert status == 403
    _publish_tab(st)
    status, _, _ = _call(st, "PUT", "/view/ch1/t1", _admin_h(), b"\x89PNGDATA")
    assert status == 200
    assert st._ch["ch1"]["vv"]["t1"] > 0
    status, hdrs, payload = _call(st, "GET", "/view/ch1/t1", _guest_h())
    assert status == 200 and hdrs["content-type"] == "image/png"
    assert payload == b"\x89PNGDATA"
    # published tab with no image → 204
    _call(st, "PUT", "/tabs/ch1", _admin_h(), _jbody(["t1", "t2"]))
    status, _, payload = _call(st, "GET", "/view/ch1/t2", _guest_h())
    assert status == 204 and payload == b""


# 10. expiry + hb grace
def test_expiry_and_hb_grace(clock):
    st = RelayState(ADMIN)
    _new_channel(st, ttl=3600)
    _publish_session(st)
    # hb grace: advance past HB_GRACE but not expiry → 503
    clock["t"] += lr.HB_GRACE + 1
    status, _, _ = _call(st, "GET", "/poll/ch1/s1", _guest_h())
    assert status == 503
    # refresh hb, then advance past expiry → 410 (channel evicted)
    st2 = RelayState(ADMIN)
    _new_channel(st2, ttl=3600)
    _publish_session(st2)
    clock["t"] += 3601
    status, _, _ = _call(st2, "GET", "/poll/ch1/s1", _guest_h())
    assert status == 410


# 11. presence evict (in-place)
def test_presence_evict(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    status, _, _ = _call(st, "POST", "/presence/ch1", _guest_h(),
                         _jbody({"pid": "p1", "name": "Bob"}))
    assert status == 200
    _, _, p = _call(st, "GET", "/presence/ch1", _admin_h())
    assert any(x["pid"] == "p1" for x in _json(p))
    clock["t"] += lr.PRES_TTL + 1
    _, _, p = _call(st, "GET", "/presence/ch1", _admin_h())
    assert _json(p) == []
    assert "p1" not in st._ch["ch1"]["presence"]


# 12. expired channel eviction
def test_expired_channel_eviction(clock):
    st = RelayState(ADMIN)
    _new_channel(st, ttl=3600)
    assert "ch1" in st._ch
    clock["t"] += 3601
    # any handle() call triggers _evict_expired_channels
    _call(st, "GET", "/inbound/ch2", _admin_h())
    assert "ch1" not in st._ch


# 13. message TTL compaction
def test_message_ttl_compaction(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    _publish_session(st)
    now_m = lr.now_ms()
    stale_mid = f"{max(0, now_m - lr.MSG_TTL * 1000 - 10000):013d}-{'0' * 13}"
    st._ch["ch1"]["in_msgs"].append({"text": "stale", "name": "", "role": "user",
                                     "origin": "guest", "mid": stale_mid, "sid": "s1"})
    _call(st, "POST", "/msg/ch1/s1", _guest_h(), _jbody({"text": "fresh"}))
    _call(st, "GET", "/inbound/ch1", _admin_h())
    assert len(st._ch["ch1"]["in_msgs"]) == 1
    assert st._ch["ch1"]["in_msgs"][0]["text"] == "fresh"


# 14. type normalization
def test_type_normalization(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    _publish_session(st)
    _call(st, "POST", "/msg/ch1/s1", _guest_h(), _jbody({"text": "x", "name": 123}))
    _, _, ib = _call(st, "GET", "/inbound/ch1", _admin_h())
    m = _json(ib)["messages"][0]
    assert m["name"] == "123" and isinstance(m["name"], str)
    # whitespace-only name → Guest
    _call(st, "POST", "/msg/ch1/s1", _guest_h(), _jbody({"text": "y", "name": "   "}))
    _, _, ib = _call(st, "GET", "/inbound/ch1", _admin_h())
    names = {m["name"] for m in _json(ib)["messages"]}
    assert "Guest" in names


# 15. CORS / OPTIONS
def test_cors_options(clock):
    st = RelayState(ADMIN)
    status, hdrs, payload = _call(st, "OPTIONS", "/anything", {})
    assert status == 204 and payload == b""
    assert hdrs["access-control-allow-origin"] == "*"
    _new_channel(st)
    _, hdrs2, _ = _call(st, "GET", "/inbound/ch1", _admin_h())
    assert hdrs2["access-control-allow-origin"] == "*"


# 16. 404
def test_not_found(clock):
    st = RelayState(ADMIN)
    status, _, p = _call(st, "GET", "/no/such/route", {})
    assert status == 404 and _json(p) == {"error": "not found"}


# 17. non-dict / null JSON bodies (worker.js SSOT: parse error -> 400, valid
#     non-object -> coerced default, never 500). (reviewer code P2)
def test_nondict_json_bodies(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    # PUT /sessions null -> non-list -> stored [] (not 400 bad json)
    status, _, _ = _call(st, "PUT", "/sessions/ch1", _admin_h(), _jbody(None))
    assert status == 200
    assert st._ch["ch1"]["sessions"] == []
    # POST /admin/channel with a JSON array -> coerced {} -> 400 missing ch (not 500)
    status, _, p = _call(st, "POST", "/admin/channel", _admin_h(), _jbody([1, 2]))
    assert status == 400 and _json(p) == {"error": "missing ch"}
    # POST /msg with a JSON number (published sid) -> coerced {} -> 400 empty text (not 500)
    _publish_session(st)
    status, _, p = _call(st, "POST", "/msg/ch1/s1", _guest_h(), _jbody(123))
    assert status == 400 and _json(p) == {"error": "empty text"}
    # genuinely invalid JSON (and empty body) -> 400 bad json
    status, _, p = _call(st, "POST", "/admin/channel", _admin_h(), b"{not json")
    assert status == 400 and _json(p) == {"error": "bad json"}
    status, _, p = _call(st, "POST", "/admin/channel", _admin_h(), b"")
    assert status == 400 and _json(p) == {"error": "bad json"}


# 18. orphan (metadata-less) channel reaping + read routes don't create channels.
#     (reviewer code P2)
def test_orphan_channel_reaped_and_reads_dont_create(clock):
    st = RelayState(ADMIN)
    # read on a non-existent channel must NOT create it (worker.js never creates on read)
    status, _, p = _call(st, "GET", "/inbound/ghost", _admin_h())
    assert status == 200 and _json(p)["messages"] == []
    assert "ghost" not in st._ch
    status, _, p2 = _call(st, "GET", "/presence/ghost", _admin_h())
    assert status == 200 and _json(p2) == []
    assert "ghost" not in st._ch
    # a stray admin write resurrects a metadata-less channel (hb set, meta empty)
    _call(st, "PUT", "/heartbeat/orphan", _admin_h())
    assert st._ch["orphan"]["meta"] == {}
    # after HB_GRACE with no further hb, the next request reaps it via _evict
    clock["t"] += lr.HB_GRACE + 5
    _call(st, "GET", "/inbound/other", _admin_h())
    assert "orphan" not in st._ch


# 19. backlog storage + idempotent replace
def test_backlog_put_and_replace(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    # admin auth required
    status, _, _ = _call(st, "PUT", "/backlog/ch1/s1", {}, _jbody([{"text": "x"}]))
    assert status == 401
    # store 3 (blank text filtered), order-only mids sort before any live mid
    status, _, p = _call(st, "PUT", "/backlog/ch1/s1", _admin_h(), _jbody([
        {"text": "u1", "role": "user", "name": "H"},
        {"text": "   ", "role": "assistant"},        # filtered
        {"text": "a1", "role": "assistant", "name": "AI"},
        {"text": "u2", "role": "user"},
    ]))
    assert status == 200 and _json(p)["count"] == 3
    bl = st._ch["ch1"]["backlog"]["s1"]
    assert [m["text"] for m in bl] == ["u1", "a1", "u2"]
    assert [m["mid"] for m in bl] == [f"{0:013d}-{i:013d}" for i in range(3)]
    assert all(m["mid"] < f"{lr.now_ms():013d}-{'0'*13}" for m in bl)
    # idempotent full replace
    _call(st, "PUT", "/backlog/ch1/s1", _admin_h(), _jbody([{"text": "only", "role": "user"}]))
    assert [m["text"] for m in st._ch["ch1"]["backlog"]["s1"]] == ["only"]


# 19b. backlog PUT upgrades a channel created before "backlog" existed (hot-reload)
def test_backlog_put_on_legacy_channel(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    del st._ch["ch1"]["backlog"]        # channel from pre-"backlog" _ensure_channel
    status, _, _ = _call(st, "PUT", "/backlog/ch1/s1", _admin_h(), _jbody([{"text": "x", "role": "user"}]))
    assert status == 200
    assert st._ch["ch1"]["backlog"]["s1"][0]["text"] == "x"


# 20. history: turn pagination (user-delimited), before cursor, has_more
def test_history_turn_pagination(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    _publish_session(st)
    # 8 turns: u1,a1,...,u8,a8 (user at even indices 0,2,...,14)
    transcript = []
    for i in range(1, 9):
        transcript.append({"text": f"u{i}", "role": "user"})
        transcript.append({"text": f"a{i}", "role": "assistant"})
    _call(st, "PUT", "/backlog/ch1/s1", _admin_h(), _jbody(transcript))

    # page 1: newest 5 turns → u4..a8
    _, _, p = _call(st, "GET", "/history/ch1/s1?turns=5", _guest_h())
    obj = _json(p)
    assert [m["text"] for m in obj["messages"]] == \
        ["u4", "a4", "u5", "a5", "u6", "a6", "u7", "a7", "u8", "a8"]
    assert obj["has_more"] is True
    assert set(obj) == {"messages", "has_more", "server_now_ms"}

    # page 2: before u4 → remaining u1..a3, exhausted
    before = obj["messages"][0]["mid"]
    _, _, p2 = _call(st, "GET", f"/history/ch1/s1?turns=5&before={before}", _guest_h())
    obj2 = _json(p2)
    assert [m["text"] for m in obj2["messages"]] == ["u1", "a1", "u2", "a2", "u3", "a3"]
    assert obj2["has_more"] is False


# 21. history merges backlog (oldest) + live, sorted; unpublished sid → empty
def test_history_merges_backlog_and_live(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    _publish_session(st)
    _call(st, "PUT", "/backlog/ch1/s1", _admin_h(),
          _jbody([{"text": "old_u", "role": "user"}, {"text": "old_a", "role": "assistant"}]))
    clock["t"] += 1
    _call(st, "POST", "/msg/ch1/s1", _guest_h(), _jbody({"text": "live_u"}))
    clock["t"] += 1
    _, _, op = _call(st, "POST", "/out/ch1/s1", _admin_h(), _jbody({"text": "live_a", "role": "assistant"}))
    live_a_mid = _json(op)["mid"]

    # before the live assistant reply → backlog + live_u (older), turn-grouped
    _, _, p = _call(st, "GET", f"/history/ch1/s1?turns=5&before={live_a_mid}", _guest_h())
    texts = [m["text"] for m in _json(p)["messages"]]
    assert texts == ["old_u", "old_a", "live_u"]   # backlog (mid ts=0) sorts first

    # no before → whole history (<=5 turns) including the live reply
    _, _, pall = _call(st, "GET", "/history/ch1/s1?turns=5", _guest_h())
    assert [m["text"] for m in _json(pall)["messages"]] == ["old_u", "old_a", "live_u", "live_a"]

    # unpublished sid → graceful empty (mirrors /poll leniency)
    _, _, pe = _call(st, "GET", "/history/ch1/other?turns=5", _guest_h())
    assert _json(pe) == {"messages": [], "has_more": False, "server_now_ms": _json(pe)["server_now_ms"]}

    # guest auth still enforced
    status, _, _ = _call(st, "GET", "/history/ch1/s1", {"Authorization": "Bearer wrong"})
    assert status == 401


# optional: real socket smoke
def test_socket_smoke(tmp_path):
    import urllib.request
    html = tmp_path / "chatdock.html"
    html.write_text("<html>hi</html>", encoding="utf-8")
    srv = lr.start_server(ADMIN, html_path=html)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/", timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers.get("content-type", "").startswith("text/html")
            assert b"hi" in resp.read()
    finally:
        srv.shutdown()
        srv.shutdown()   # idempotent
