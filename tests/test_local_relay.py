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
    assert st._ch["ch1"]["tabs_by_ds"][""] == ["t1"]   # bare list → null-group slice


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
    assert status == 403 and _json(p) == {"error": "session not in scope"}
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
    assert set(obj) == {"messages", "sessions", "tabs", "active_dataset",
                        "datasets", "view_versions", "alive", "server_now_ms"}
    assert obj["alive"] is True
    assert obj["active_dataset"] is None   # legacy bare-list publish → null dataset

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
    assert st._ch["ch1"]["vv_by_ds"][""]["t1"] > 0
    status, hdrs, payload = _call(st, "GET", "/view/ch1/t1", _guest_h())
    assert status == 200 and hdrs["content-type"] == "image/png"
    assert payload == b"\x89PNGDATA"
    # published tab with no image → 204
    _call(st, "PUT", "/tabs/ch1", _admin_h(), _jbody(["t1", "t2"]))
    status, _, payload = _call(st, "GET", "/view/ch1/t2", _guest_h())
    assert status == 204 and payload == b""


# 9b. DS layer: tabs PUT new object shape {active_dataset, tabs}
def test_tabs_put_dataset_shape(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    status, _, p = _call(st, "PUT", "/tabs/ch1", _admin_h(),
                         _jbody({"active_dataset": "ds1", "tabs": ["t1", "t2"]}))
    assert status == 200 and _json(p) == {"ok": True}
    assert st._ch["ch1"]["tabs_by_ds"]["ds1"] == ["t1", "t2"]
    assert st._ch["ch1"]["dataset"] == "ds1"
    # a session in ds1 so ds-less poll (active-DS filter) returns it
    _call(st, "PUT", "/sessions/ch1", _admin_h(),
          _jbody([{"id": "s1", "title": "T", "busy": False, "dataset": "ds1"}]))
    _, _, pp = _call(st, "GET", "/poll/ch1/s1", _guest_h())
    obj = _json(pp)
    assert obj["active_dataset"] == "ds1" and obj["tabs"] == ["t1", "t2"]


# 9c. legacy bare-list tabs PUT stays accepted (pre-DS host) → null dataset
def test_tabs_put_legacy_list_null_dataset(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    _publish_tab(st)                        # bare ["t1"]
    assert st._ch["ch1"]["tabs_by_ds"][""] == ["t1"]
    assert st._ch["ch1"]["dataset"] is None
    _publish_session(st)
    _, _, pp = _call(st, "GET", "/poll/ch1/s1", _guest_h())
    assert _json(pp)["active_dataset"] is None


# 9d. per-DS view isolation: same-named tabs in two datasets keep separate PNGs
#     and a guest reading with a given ds never sees the other dataset's bytes.
def test_same_named_tabs_per_ds_isolation(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    # publish "t1" in BOTH datasets via the new multi-DS shape
    _call(st, "PUT", "/tabs/ch1", _admin_h(), _jbody({
        "active_dataset": "ds1", "datasets": ["ds1", "ds2"],
        "tabs_by_dataset": {"ds1": ["t1"], "ds2": ["t1"]}}))
    _call(st, "PUT", "/view/ch1/t1?ds=ds1", _admin_h(), b"\x89PNG_DS1")
    _call(st, "PUT", "/view/ch1/t1?ds=ds2", _admin_h(), b"\x89PNG_DS2")
    assert st._ch["ch1"]["vv_by_ds"]["ds1"]["t1"] > 0
    assert st._ch["ch1"]["vv_by_ds"]["ds2"]["t1"] > 0
    # each ds reads its own PNG
    status, _, p1 = _call(st, "GET", "/view/ch1/t1?ds=ds1", _guest_h())
    assert status == 200 and p1 == b"\x89PNG_DS1"
    status, _, p2 = _call(st, "GET", "/view/ch1/t1?ds=ds2", _guest_h())
    assert status == 200 and p2 == b"\x89PNG_DS2"
    # ds absent → active (ds1) slice
    status, _, p0 = _call(st, "GET", "/view/ch1/t1", _guest_h())
    assert status == 200 and p0 == b"\x89PNG_DS1"
    # poll per DS returns that DS's view_versions + tabs; datasets list is global
    _publish_session(st)
    _, _, pp = _call(st, "GET", "/poll/ch1/_?ds=ds2", _guest_h())
    obj = _json(pp)
    assert obj["active_dataset"] == "ds1" and obj["tabs"] == ["t1"]
    assert set(obj["datasets"]) == {"ds1", "ds2"}
    assert "t1" in obj["view_versions"]


# 9d-2. empty ds= (null group) reads a slice distinct from the active dataset —
#       the keep_blank_values regression guard.
def test_null_group_ds_addressable(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    _call(st, "PUT", "/tabs/ch1", _admin_h(), _jbody({
        "active_dataset": "ds1", "datasets": ["ds1", ""],
        "tabs_by_dataset": {"ds1": ["t1"], "": ["t1"]}}))
    _call(st, "PUT", "/view/ch1/t1?ds=ds1", _admin_h(), b"\x89PNG_ACTIVE")
    _call(st, "PUT", "/view/ch1/t1?ds=", _admin_h(), b"\x89PNG_NULL")
    # empty ds= must reach the null group, NOT the active slice
    status, _, pn = _call(st, "GET", "/view/ch1/t1?ds=", _guest_h())
    assert status == 200 and pn == b"\x89PNG_NULL"
    status, _, pa = _call(st, "GET", "/view/ch1/t1?ds=ds1", _guest_h())
    assert status == 200 and pa == b"\x89PNG_ACTIVE"


# 9d-3. a dataset dropped from `datasets` has its slices pruned (3-dict union)
def test_dropped_dataset_pruned(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    _call(st, "PUT", "/tabs/ch1", _admin_h(), _jbody({
        "active_dataset": "ds1", "datasets": ["ds1", "ds2"],
        "tabs_by_dataset": {"ds1": ["t1"], "ds2": ["t1"]}}))
    _call(st, "PUT", "/view/ch1/t1?ds=ds2", _admin_h(), b"\x89PNG_DS2")
    # host closes ds2 (drops it from datasets); even the view/vv slice is pruned
    _call(st, "PUT", "/tabs/ch1", _admin_h(), _jbody({
        "active_dataset": "ds1", "datasets": ["ds1"],
        "tabs_by_dataset": {"ds1": ["t1"]}}))
    assert "ds2" not in st._ch["ch1"]["tabs_by_ds"]
    assert "ds2" not in st._ch["ch1"]["views_by_ds"]
    assert "ds2" not in st._ch["ch1"]["vv_by_ds"]
    # ds1 (still open) survives
    assert st._ch["ch1"]["tabs_by_ds"]["ds1"] == ["t1"]


# 9e. tab-list-only changes within one dataset keep the view store
def test_same_dataset_tabs_change_keeps_views(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    _call(st, "PUT", "/tabs/ch1", _admin_h(), _jbody({"active_dataset": "ds1", "tabs": ["t1"]}))
    _call(st, "PUT", "/view/ch1/t1", _admin_h(), b"\x89PNG")
    _call(st, "PUT", "/tabs/ch1", _admin_h(), _jbody({"active_dataset": "ds1", "tabs": ["t1", "t2"]}))
    assert st._ch["ch1"]["views_by_ds"]["ds1"].get("t1") == b"\x89PNG"
    assert st._ch["ch1"]["vv_by_ds"]["ds1"]["t1"] > 0


# 9f. field-type coercion on the new shape (admin-only surface, never 500)
def test_tabs_put_bad_field_types(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    status, _, _ = _call(st, "PUT", "/tabs/ch1", _admin_h(),
                         _jbody({"active_dataset": 5, "tabs": "x"}))
    assert status == 200
    assert st._ch["ch1"]["dataset"] is None and st._ch["ch1"]["tabs_by_ds"][""] == []
    _call(st, "PUT", "/tabs/ch1", _admin_h(), _jbody({"tabs": ["t1", 5]}))
    assert st._ch["ch1"]["tabs_by_ds"][""] == ["t1", "5"]  # entries str-coerced (guest renders raw)
    status, _, p = _call(st, "PUT", "/tabs/ch1", _admin_h(), b"{not json")
    assert status == 400 and _json(p) == {"error": "bad json"}


# 9g. channel dict from a pre-DS _ensure_channel (mid-meeting hot-reload) must not 500
def test_poll_on_legacy_channel(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    _publish_session(st)
    del st._ch["ch1"]["dataset"]
    status, _, p = _call(st, "GET", "/poll/ch1/s1", _guest_h())
    assert status == 200 and _json(p)["active_dataset"] is None
    # a tabs PUT on such a channel upgrades it in place
    status, _, _ = _call(st, "PUT", "/tabs/ch1", _admin_h(), _jbody(["t1"]))
    assert status == 200 and st._ch["ch1"]["dataset"] is None


# 9h. /history for an unpublished sid keeps has_more=True (no histExhausted latch
#     across a dataset switch; see the route comment)
def test_history_unpublished_sid_has_more(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    _, _, pe = _call(st, "GET", "/history/ch1/nosuch?turns=5", _guest_h())
    obj = _json(pe)
    assert obj["messages"] == [] and obj["has_more"] is True


# 9i. ds-scoped sessions in poll: ds absent → active-DS sessions only (legacy
#     presenter-mirror); ds present → flat all-DS session list (client filters).
def test_poll_sessions_ds_scope(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    _call(st, "PUT", "/tabs/ch1", _admin_h(), _jbody({
        "active_dataset": "dsA", "datasets": ["dsA", "dsB"],
        "tabs_by_dataset": {"dsA": [], "dsB": []}}))
    _call(st, "PUT", "/sessions/ch1", _admin_h(), _jbody([
        {"id": "a", "title": "A", "busy": False, "dataset": "dsA"},
        {"id": "b", "title": "B", "busy": False, "dataset": "dsB"},
    ]))
    # ds absent → only active dsA's sessions
    _, _, p0 = _call(st, "GET", "/poll/ch1/_", _guest_h())
    assert {s["id"] for s in _json(p0)["sessions"]} == {"a"}
    # ds present → all sessions (flat), client-side filtered by curDs
    _, _, pb = _call(st, "GET", "/poll/ch1/_?ds=dsB", _guest_h())
    assert {s["id"] for s in _json(pb)["sessions"]} == {"a", "b"}


# 9j. session scope gate across poll messages / /msg / /history (Issue #78):
#     ds absent → active-DS sid only; ds present → sid.dataset == ds only.
def test_session_scope_gate(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    _call(st, "PUT", "/tabs/ch1", _admin_h(), _jbody({
        "active_dataset": "dsA", "datasets": ["dsA", "dsB"],
        "tabs_by_dataset": {"dsA": [], "dsB": []}}))
    _call(st, "PUT", "/sessions/ch1", _admin_h(), _jbody([
        {"id": "a", "title": "A", "busy": False, "dataset": "dsA"},
        {"id": "b", "title": "B", "busy": False, "dataset": "dsB"},
    ]))
    # background dsB message present
    _call(st, "POST", "/out/ch1/b", _admin_h(), _jbody({"text": "b_reply", "role": "assistant"}))

    # ds absent (legacy guest): background dsB sid `b` is out of scope everywhere
    _, _, pm = _call(st, "GET", "/poll/ch1/b", _guest_h())
    assert _json(pm)["messages"] == []                       # poll messages gated
    status, _, _ = _call(st, "POST", "/msg/ch1/b", _guest_h(), _jbody({"text": "hi"}))
    assert status == 403                                     # /msg gated
    _, _, ph = _call(st, "GET", "/history/ch1/b", _guest_h())
    assert _json(ph)["has_more"] is True and _json(ph)["messages"] == []

    # ds=dsB (new guest): sid `b` is in scope; the dsA sid `a` is NOT
    _, _, pm2 = _call(st, "GET", "/poll/ch1/b?ds=dsB", _guest_h())
    assert [m["text"] for m in _json(pm2)["messages"]] == ["b_reply"]
    status, _, _ = _call(st, "POST", "/msg/ch1/b?ds=dsB", _guest_h(), _jbody({"text": "hi"}))
    assert status == 200
    status, _, _ = _call(st, "POST", "/msg/ch1/a?ds=dsB", _guest_h(), _jbody({"text": "x"}))
    assert status == 403                                     # a is dsA, not dsB


# 9k. hot-reload migration: a pre-#78 channel (flat tabs/views/vv, no per-DS keys)
#     is upgraded in place and its stored view survives (guest still sees it).
def test_hot_reload_migration_keeps_view(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    _publish_session(st, sid="s1")
    # simulate a channel dict produced by pre-#78 code: flat stores, no per-DS keys
    cs = st._ch["ch1"]
    cs["dataset"] = "dsX"
    cs["tabs"] = ["t1"]
    cs["views"] = {"t1": b"\x89PNG_OLD"}
    cs["vv"] = {"t1": 1234}
    for k in ("datasets", "tabs_by_ds", "views_by_ds", "vv_by_ds"):
        cs.pop(k, None)
    # a read path (poll) must upgrade-in-place and preserve the flat view
    _, _, pp = _call(st, "GET", "/poll/ch1/s1", _guest_h())
    obj = _json(pp)
    assert obj["active_dataset"] == "dsX" and obj["datasets"] == ["dsX"]
    assert obj["tabs"] == ["t1"] and "t1" in obj["view_versions"]
    status, _, payload = _call(st, "GET", "/view/ch1/t1", _guest_h())
    assert status == 200 and payload == b"\x89PNG_OLD"
    # flat store is blanked post-migration (never re-migrated)
    assert cs["tabs"] == [] and cs["views"] == {} and cs["vv"] == {}


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


# 17. non-dict / null JSON bodies (the reference Worker SSOT: parse error -> 400, valid
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
    # read on a non-existent channel must NOT create it (the reference Worker never creates on read)
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

    # unpublished sid → graceful empty; has_more stays True so a guest whose
    # session left the published set (dataset switch) doesn't latch histExhausted
    _, _, pe = _call(st, "GET", "/history/ch1/other?turns=5", _guest_h())
    assert _json(pe) == {"messages": [], "has_more": True, "server_now_ms": _json(pe)["server_now_ms"]}

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


# ---- Issue #85: bind_host / fixed-port contract ----

def test_start_server_bind_host():
    srv = lr.start_server(ADMIN, bind_host="0.0.0.0")
    try:
        assert srv.bind_host == "0.0.0.0"
    finally:
        srv.shutdown()


def test_start_server_fixed_port_no_silent_fallback():
    """Linux/WSL semantics: a fixed port already in use raises OSError (no silent
    ephemeral fallback); after shutdown the same port re-binds (no port leak)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    srv = lr.start_server(ADMIN, port=port)
    try:
        assert srv.port == port
        with pytest.raises(OSError):
            lr.start_server(ADMIN, port=port)   # collision → OSError, no fallback
    finally:
        srv.shutdown()

    srv2 = lr.start_server(ADMIN, port=port)    # freed → re-binds same port
    try:
        assert srv2.port == port
    finally:
        srv2.shutdown()


# ---- Issue #81: guest-requested new chat sessions ----

def test_newsession_guest_auth(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    status, _, _ = _call(st, "POST", "/newsession/ch1", {}, _jbody({"name": "Bob"}))
    assert status == 401
    status, _, _ = _call(st, "POST", "/newsession/ghost", _guest_h(), _jbody({"name": "Bob"}))
    assert status == 410


def test_newsession_enqueue_and_drain(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    status, _, p = _call(st, "POST", "/newsession/ch1?ds=dsA", _guest_h(),
                         _jbody({"name": "Bob"}))
    assert status == 200
    obj = _json(p)
    assert obj["ok"] is True and obj["mid"]

    status, _, p2 = _call(st, "GET", "/newsessions/ch1", _admin_h())
    assert status == 200
    reqs = _json(p2)["requests"]
    assert len(reqs) == 1
    assert reqs[0]["ds"] == "dsA"
    assert reqs[0]["name"] == "Bob"
    assert reqs[0]["mid"] == obj["mid"]


def test_newsessions_admin_only(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    status, _, _ = _call(st, "GET", "/newsessions/ch1", _guest_h())
    assert status == 401


def test_newsession_ds_canonicalisation(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    _call(st, "PUT", "/tabs/ch1", _admin_h(),
          _jbody({"active_dataset": "dsH", "tabs": ["t1"]}))

    def _post(path):
        status, _, p = _call(st, "POST", path, _guest_h(), _jbody({"name": "Bob"}))
        return status, _json(p)["mid"]

    s_a, mid_a = _post("/newsession/ch1?ds=")          # explicit empty → null group
    s_b, mid_b = _post("/newsession/ch1")              # absent → host active
    s_c, mid_c = _post("/newsession/ch1?ds=dsA")       # unpublished name → verbatim
    assert (s_a, s_b, s_c) == (200, 200, 200)

    _, _, p = _call(st, "GET", "/newsessions/ch1", _admin_h())
    by_mid = {r["mid"]: r["ds"] for r in _json(p)["requests"]}
    assert by_mid[mid_a] == ""
    assert by_mid[mid_b] == "dsH"
    assert by_mid[mid_c] == "dsA"


def test_newsession_since_cursor(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    clock["t"] += 1
    _call(st, "POST", "/newsession/ch1?ds=dsA", _guest_h(), _jbody({"name": "n1"}))
    clock["t"] += 1
    _call(st, "POST", "/newsession/ch1?ds=dsA", _guest_h(), _jbody({"name": "n2"}))

    _, _, p = _call(st, "GET", "/newsessions/ch1", _admin_h())
    reqs = _json(p)["requests"]
    mid1 = next(r["mid"] for r in reqs if r["name"] == "n1")
    mid2 = next(r["mid"] for r in reqs if r["name"] == "n2")

    _, _, r1 = _call(st, "GET", f"/newsessions/ch1?since={mid1}", _admin_h())
    assert {r["name"] for r in _json(r1)["requests"]} == {"n1", "n2"}
    _, _, r2 = _call(st, "GET", f"/newsessions/ch1?since={mid2}", _admin_h())
    assert {r["name"] for r in _json(r2)["requests"]} == {"n2"}


def test_newsession_queue_cap(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    total = lr.MAX_NEW_SESSIONS + 5
    for i in range(total):
        _call(st, "POST", "/newsession/ch1?ds=dsA", _guest_h(),
              _jbody({"name": "g%d" % i}))
    _, _, p = _call(st, "GET", "/newsessions/ch1", _admin_h())
    reqs = _json(p)["requests"]
    assert len(reqs) == lr.MAX_NEW_SESSIONS
    names = {r["name"] for r in reqs}
    assert "g0" not in names                       # oldest dropped
    assert "g%d" % (total - 1) in names            # newest kept


def test_newsession_missing_key_channel(clock):
    st = RelayState(ADMIN)
    _new_channel(st)
    st._ch["ch1"].pop("new_sessions", None)        # pre-#81 channel (hot-reload)
    status, _, p = _call(st, "POST", "/newsession/ch1?ds=dsA", _guest_h(),
                         _jbody({"name": "Bob"}))
    assert status == 200
    st._ch["ch1"].pop("new_sessions", None)
    status, _, p2 = _call(st, "GET", "/newsessions/ch1", _admin_h())
    assert status == 200
    assert _json(p2)["requests"] == []
