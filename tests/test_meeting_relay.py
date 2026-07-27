"""Tests for the meeting relay (Issue #42): token, lifecycle, echo gates,
cursor/lookback/dedup, server-time cursor, expiry/stop, publish scope, view hash,
and the share-window smoke test.

Hermetic: no real network. urllib is monkeypatched; the relay QThread `start` is
stubbed to a no-op so no background I/O runs.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import urllib.error

import pytest

from common.i18n import tr


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


class FakeResp:
    def __init__(self, data=b"", status=200):
        self._data = data
        self.status = status

    def read(self):
        return self._data

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeMsg:
    def __init__(self, role, content):
        self.role = role
        self.content = content


class FakeSession:
    def __init__(self, messages):
        self.messages = list(messages)


class FakeChat:
    def __init__(self, summaries=None, messages=None):
        self._summaries = summaries or []
        self._messages = messages or {}   # sid -> [FakeMsg, ...] (for backfill)
        self.injected = []
        self.created = []
        self._pending_remote = {}

    def session_summaries(self):
        return list(self._summaries)

    def create_remote_session(self, dataset):
        self.created.append(dataset)
        sid = "new%d" % len(self.created)
        self._summaries.append({"id": sid, "title": sid, "busy": False, "dataset": dataset})
        return sid

    def _session_by_id(self, sid):
        msgs = self._messages.get(sid)
        return FakeSession(msgs) if msgs is not None else None

    def inject_remote_message(self, text, sender, session_id=None):
        self.injected.append((text, sender, session_id))


class _FakeTabObj:
    """A window tab carrying a session_spec dataset (for active-DS filtering)."""
    def __init__(self, name, dataset):
        self.name = name
        self.session_spec = {"kind": "analysis", "name": name, "dataset": dataset}

    def grab(self):
        # A minimal real pixmap so _capture_tab can produce a PNG (view-render tests).
        from PySide6.QtGui import QPixmap
        pm = QPixmap(4, 4)
        pm.fill()
        return pm


class FakeWindow:
    def __init__(self, chat, dataset=None, tabs=None, tab_datasets=None, tab_objs=None):
        self._chat = chat
        self.current_dataset = dataset
        self._tabs = tabs or []
        # Each tab's owning dataset (fixed at construction; defaults to the
        # window's dataset). Switching current_dataset later does NOT change a
        # tab's dataset — that's exactly what drives the B5 publish swap.
        self._tab_datasets = dict(tab_datasets or {})
        self._default_ds = dataset
        # Explicit (name, dataset) pairs — the only way to model two same-named
        # tabs living in different datasets (the name-keyed dict above can't).
        self._tab_objs = list(tab_objs or [])

    def chat_widget(self):
        return self._chat

    def tab_names(self):
        if self._tab_objs:
            return [n for n, _ in self._tab_objs]
        return list(self._tabs)

    def tabs(self):
        if self._tab_objs:
            return [_FakeTabObj(n, ds) for n, ds in self._tab_objs]
        return [
            _FakeTabObj(n, self._tab_datasets.get(n, self._default_ds))
            for n in self._tabs
        ]

    def open_dataset_keys(self):
        # Model the real window: all open DS groups (null → "") in display order.
        # Here we synthesize from current_dataset + each tab's dataset.
        keys = ["" if self.current_dataset is None else self.current_dataset]
        for t in self.tabs():
            ds = t.session_spec.get("dataset")
            k = "" if ds is None else ds
            if k not in keys:
                keys.append(k)
        return keys

    def register_retranslate_hook(self, fn):
        pass


def _sess(sid):
    return {"id": sid, "title": sid.upper(), "busy": False, "dataset": "ds1"}


_UI_PREFS: dict = {}


@pytest.fixture(autouse=True)
def _isolate_ui_prefs(monkeypatch):
    """全 MeetingRelay 構築を hermetic に: __init__ の read_ui_pref / setter の
    update_ui_pref を in-memory ストアへ差し替え、実 ui_prefs.json も
    global_state_dir() のディレクトリ作成も踏ませない。_make_relay 経由か
    MeetingRelay 直接構築かを問わず適用される。"""
    import llm_bridge.paths as lp
    _UI_PREFS.clear()
    monkeypatch.setattr(lp, "read_ui_pref",
                        lambda key, default=None: _UI_PREFS.get(key, default))
    monkeypatch.setattr(lp, "update_ui_pref",
                        lambda key, value: _UI_PREFS.__setitem__(key, value))


def _make_relay(monkeypatch, win, *, ui_prefs=None):
    import meeting.relay as mr
    if ui_prefs:
        _UI_PREFS.update(ui_prefs)
    monkeypatch.setattr(mr._RelayWorker, "start", lambda self: None)
    r = mr.MeetingRelay(win)
    r._remote_base = "http://relay.test"   # legacy mode → synchronous token, no socket/tunnel
    r._admin_key = "ADMIN"
    return mr, r


# ---- token round-trip + meeting_start ----

def test_meeting_start_and_token(qapp, monkeypatch):
    chat = FakeChat([
        {"id": "a", "title": "A", "busy": False, "dataset": "ds1"},
        {"id": "b", "title": "B", "busy": False, "dataset": "ds2"},
    ])
    win = FakeWindow(chat, dataset="ds1", tabs=["t1"])
    mr, r = _make_relay(monkeypatch, win)
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = req.data
        captured["method"] = req.get_method()
        captured["auth"] = req.headers.get("Authorization")
        return FakeResp(json.dumps({"expires_at": 9999999999, "server_now_ms": 123}).encode())

    monkeypatch.setattr(mr.urllib.request, "urlopen", fake_urlopen)

    tokens = []
    r.tokenReady.connect(tokens.append)
    r.meeting_start(999999)   # clamp to 86400
    token = tokens[-1]

    body = json.loads(captured["body"])
    assert len(body["secret_hash"]) == 64
    assert body["ttl_sec"] == 86400
    assert captured["method"] == "POST"
    assert captured["auth"] == "Bearer ADMIN"
    assert r.expires_at() == 9999999999
    # publish scope = ALL open datasets' sessions (Issue #78): both "a" (ds1) and
    # "b" (ds2) are default-shared.
    assert r.published_session_ids() == {"a", "b"}

    pad = token + "=" * (-len(token) % 4)
    obj = json.loads(base64.urlsafe_b64decode(pad))
    assert obj["channel"] == r.channel()
    assert obj["base_url"] == "http://relay.test"
    assert obj["secret"]
    assert r.current_token() == token
    r.stop()


# ---- out: send gate ----

def test_out_gate(qapp, monkeypatch):
    chat = FakeChat()
    win = FakeWindow(chat)
    mr, r = _make_relay(monkeypatch, win)
    w = mr._RelayWorker("http://relay.test", "ADMIN", "ch")
    r._worker = w
    r._sharing = True
    r._published_session_ids = {"a"}

    r._on_message_added("a", "user", "hi", "local")
    assert len(w._outbox) == 1 and w._outbox[0]["kind"] == "out"
    assert w._outbox[0]["body"]["name"] == r.host_name
    w._outbox.clear()

    r._on_message_added("a", "assistant", "ans", "local")
    assert w._outbox[0]["body"]["name"] == tr("meeting.assistant_name_default")
    w._outbox.clear()

    # remote re-emit, non-published sid, empty text, not sharing → nothing sent
    r._on_message_added("a", "user", "hi", "remote")
    r._on_message_added("z", "user", "hi", "local")
    r._on_message_added("a", "user", "   ", "local")
    assert not w._outbox
    r._sharing = False
    r._on_message_added("a", "user", "hi", "local")
    assert not w._outbox


# ---- in: receive gate ----

def test_in_gate(qapp, monkeypatch):
    chat = FakeChat()
    win = FakeWindow(chat)
    mr, r = _make_relay(monkeypatch, win)
    r._published_session_ids = {"a"}

    r._on_remote_message("a", "Bob", "hello")
    assert chat.injected == [("hello", "Bob", "a")]
    # non-published sid is dropped (symmetric to out: gate)
    r._on_remote_message("z", "Bob", "nope")
    assert len(chat.injected) == 1


def test_live_message_gate_all_datasets(qapp, monkeypatch):
    """Issue #78: the LIVE out (added/streaming) and in (remote injection) gates
    are now scoped to _published_session_ids only — background-DS sessions are
    default-shared, so their in-flight replies flow and guest injections land
    regardless of which dataset is host-active."""
    chat = FakeChat([
        {"id": "a", "title": "A", "busy": False, "dataset": "dsA"},
        {"id": "b", "title": "B", "busy": False, "dataset": "dsB"},
    ])
    win = FakeWindow(chat, dataset="dsA")
    mr, r = _make_relay(monkeypatch, win)
    w = mr._RelayWorker("http://relay.test", "ADMIN", "ch")
    r._worker = w
    r._sharing = True
    r._published_session_ids = {"a", "b"}          # both published

    # dsA active: BOTH a and background dsB's b flow out (Issue #78).
    r._on_message_added("a", "assistant", "hi from a", "local")
    r._on_message_added("b", "assistant", "hi from b", "local")
    assert [i["sid"] for i in w._outbox] == ["a", "b"]
    w._outbox.clear()

    # switch to dsB: a (now background dsA) still flows — DS is no longer a gate.
    win.current_dataset = "dsB"
    r._on_message_added("a", "assistant", "still a", "local")
    assert [i["sid"] for i in w._outbox] == ["a"]
    w._outbox.clear()
    r._on_message_streaming("a", "partial a", "local", "sid1")
    assert [i["sid"] for i in w._outbox] == ["a"]   # streaming partial too
    w._outbox.clear()

    # an explicitly unpublished session is still withheld (the one remaining gate).
    r._published_session_ids = {"b"}
    r._on_message_added("a", "assistant", "unshared a", "local")
    assert not w._outbox

    # in: a guest message to any published session injects, regardless of active DS.
    r._published_session_ids = {"a", "b"}
    r._on_remote_message("a", "Bob", "to a")
    r._on_remote_message("b", "Bob", "to b")
    assert chat.injected == [("to a", "Bob", "a"), ("to b", "Bob", "b")]
    # unpublished sid still dropped
    r._published_session_ids = {"a"}
    r._on_remote_message("b", "Bob", "nope")
    assert len(chat.injected) == 2


def _mid(ts):
    return f"{ts:013d}-{'0' * 13}"


# ---- lookback + bucket + dedup ----

def test_worker_lookback_dedup(qapp):
    import meeting.relay as mr
    w = mr._RelayWorker("http://x", "k", "ch")
    got = []
    w.sig_inbound.connect(lambda s, n, t: got.append(t))
    seq = [
        {"messages": [{"sid": "S", "name": "n", "text": "M2", "mid": _mid(2000)}],
         "server_now_ms": 2000},
        {"messages": [{"sid": "S", "name": "n", "text": "M1", "mid": _mid(1000)},
                      {"sid": "S", "name": "n", "text": "M2", "mid": _mid(2000)}],
         "server_now_ms": 2000},
    ]
    calls = {"n": 0}

    def fake_req(method, path, data=None, is_png=False):
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return FakeResp(json.dumps(seq[i]).encode())

    w._req = fake_req
    w._do_inbound()   # reanchor: emits M2
    w._do_inbound()   # since-based: M1 new (late lower mid), M2 deduped
    assert got == ["M2", "M1"]


# ---- server-time cursor (clock skew) ----

def test_server_time_cursor(qapp):
    import meeting.relay as mr
    w = mr._RelayWorker("http://x", "k", "ch")
    big = 99999999999   # far from local time

    def fake_req(method, path, data=None, is_png=False):
        return FakeResp(json.dumps({"messages": [], "server_now_ms": big}).encode())

    w._req = fake_req
    w._do_inbound()
    assert w._latest_seen_ts == big
    assert w._reanchor is False


def test_inbound_failure_does_not_advance_cursor(qapp):
    import meeting.relay as mr
    w = mr._RelayWorker("http://x", "k", "ch")
    w._reanchor = False
    w._latest_seen_ts = 500

    def boom(method, path, data=None, is_png=False):
        raise OSError("network down")

    w._req = boom
    w._do_inbound()
    assert w._latest_seen_ts == 500   # unchanged on failure


# ---- 401/410 expiry + stop idempotency ----

def test_expired_and_stop_idempotent(qapp, monkeypatch):
    import meeting.relay as mr
    w = mr._RelayWorker("http://x", "k", "ch")
    states = []
    w.sig_state.connect(lambda s: states.append(s))

    def gone(method, path, data=None, is_png=False):
        raise urllib.error.HTTPError("http://x", 410, "gone", {}, io.BytesIO(b""))

    w._req = gone
    w._do_inbound()
    assert "expired" in states

    chat = FakeChat()
    win = FakeWindow(chat)
    monkeypatch.setattr(mr._RelayWorker, "start", lambda self: None)
    r = mr.MeetingRelay(win)
    r._worker = w
    r.stop()
    r.stop()   # idempotent
    assert r._worker is None


# ---- publish scope: all-default + dataset-switch invariance + deletion pruning ----

def test_publish_scope(qapp, monkeypatch):
    chat = FakeChat([
        {"id": "a", "title": "A", "busy": False, "dataset": "ds1"},
        {"id": "b", "title": "B", "busy": False, "dataset": "ds2"},
    ])
    win = FakeWindow(chat, dataset="ds1", tabs=[])
    mr, r = _make_relay(monkeypatch, win)
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)
    # publish scope = ALL open datasets (Issue #78): both sessions default-shared.
    assert r.published_session_ids() == {"a", "b"}
    r._last_sessions_json = None
    r._on_capture_tick()
    sess_puts = [i for i in r._worker._outbox if i["kind"] == "sessions"]
    assert sess_puts and {s["id"] for s in sess_puts[-1]["data"]} == {"a", "b"}
    # each published session carries its dataset (guest DS bucketing).
    assert {s["dataset"] for s in sess_puts[-1]["data"]} == {"ds1", "ds2"}

    # Switching the active dataset does NOT change the published set (both remain).
    win.current_dataset = "ds2"
    r._last_sessions_json = None
    r._worker._outbox.clear()
    r._on_capture_tick()
    sess_puts = [i for i in r._worker._outbox if i["kind"] == "sessions"]
    assert sess_puts and {s["id"] for s in sess_puts[-1]["data"]} == {"a", "b"}

    # a deleted session drops out of the selection via ∩ existing.
    win.current_dataset = "ds1"
    chat._summaries = [s for s in chat._summaries if s["id"] != "a"]
    r._last_sessions_json = None
    r._on_capture_tick()
    assert "a" not in r.published_session_ids()
    r.stop()


def test_capture_tick_exception_is_swallowed(qapp, monkeypatch):
    # A throw from a capture-tick collaborator must NOT propagate out of the
    # GUI-thread QTimer slot (it could tear down the app) and must NOT stop
    # sharing — the next tick recovers. (Issue #43 cause #6.)
    chat = FakeChat([{"id": "a", "title": "A", "busy": False, "dataset": "ds1"}])
    win = FakeWindow(chat, dataset="ds1", tabs=["t1"])
    mr, r = _make_relay(monkeypatch, win)
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)

    def boom():
        raise RuntimeError("grab failed")

    monkeypatch.setattr(chat, "session_summaries", boom)
    r._on_capture_tick()   # must not raise
    assert r._sharing is True
    r.stop()


# ---- backfill: stage pre-meeting transcript once per session per meeting ----

def test_backfill_on_meeting_start(qapp, monkeypatch):
    chat = FakeChat(
        [{"id": "a", "title": "A", "busy": False, "dataset": "ds1"}],
        messages={"a": [FakeMsg("user", "q1"), FakeMsg("assistant", "ans1"),
                        FakeMsg("tool", "junk"), FakeMsg("user", "   "),   # both filtered
                        FakeMsg("assistant", ""), FakeMsg("user", "q2")]},  # blank filtered
    )
    win = FakeWindow(chat, dataset="ds1", tabs=[])
    mr, r = _make_relay(monkeypatch, win)
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)   # commits state + first capture tick → backfill

    backlogs = [i for i in r._worker._outbox if i["kind"] == "backlog"]
    assert len(backlogs) == 1
    item = backlogs[0]
    assert item["sid"] == "a"
    assert [m["text"] for m in item["data"]] == ["q1", "ans1", "q2"]
    assert [m["role"] for m in item["data"]] == ["user", "assistant", "user"]
    assert item["data"][0]["name"] == r.host_name
    assert item["data"][1]["name"] == tr("meeting.assistant_name_default")
    assert r._backfilled_ids == {"a"}

    # subsequent ticks do NOT re-backfill the same session
    r._on_capture_tick()
    assert len([i for i in r._worker._outbox if i["kind"] == "backlog"]) == 1
    r.stop()


def test_backfill_recleared_on_restart(qapp, monkeypatch):
    chat = FakeChat([{"id": "a", "title": "A", "busy": False, "dataset": "ds1"}],
                    messages={"a": [FakeMsg("user", "q1")]})
    win = FakeWindow(chat, dataset="ds1", tabs=[])
    mr, r = _make_relay(monkeypatch, win)
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)
    assert r._backfilled_ids == {"a"}
    r.stop()
    # a fresh channel must re-stage backlog
    r.meeting_start(3600)
    assert r._backfilled_ids == {"a"}
    assert any(i["kind"] == "backlog" for i in r._worker._outbox)
    r.stop()


def test_worker_sends_backlog_put(qapp):
    import meeting.relay as mr
    w = mr._RelayWorker("http://x", "k", "ch")
    calls = []

    def fake_req(method, path, data=None, is_png=False):
        calls.append((method, path, data))
        return FakeResp(b"{}")

    w._req = fake_req
    w.enqueue({"kind": "backlog", "sid": "s1", "data": [{"text": "x", "role": "user"}]})
    w._drain_outbox()
    assert calls and calls[0][0] == "PUT" and calls[0][1] == "/backlog/ch/s1"
    assert calls[0][2] == [{"text": "x", "role": "user"}]


def test_backlog_history_end_to_end(qapp):
    """Full path over a real socket: worker PUT /backlog → relay → guest GET
    /history. Guards the worker<->relay integration the fake-_req tests skip."""
    import hashlib
    import urllib.request
    import meeting.local_relay as lr
    import meeting.relay as mr
    srv = lr.start_server("ADMIN")
    try:
        base = f"http://127.0.0.1:{srv.port}"
        secret = "sek"
        sh = hashlib.sha256(secret.encode()).hexdigest()
        srv.state.handle("POST", "/admin/channel", {"Authorization": "Bearer ADMIN"},
                         json.dumps({"ch": "ch", "ttl_sec": 3600, "secret_hash": sh}).encode())
        srv.state.handle("PUT", "/sessions/ch", {"Authorization": "Bearer ADMIN"},
                         json.dumps([{"id": "s1", "title": "T", "busy": False}]).encode())
        w = mr._RelayWorker(base, "ADMIN", "ch")
        w.enqueue({"kind": "backlog", "sid": "s1", "data": [
            {"text": "pre_u", "name": "H", "role": "user"},
            {"text": "pre_a", "name": "AI", "role": "assistant"}]})
        w._drain_outbox()
        req = urllib.request.Request(base + "/history/ch/s1?turns=5",
                                     headers={"Authorization": "Bearer " + secret})
        with urllib.request.urlopen(req, timeout=5) as r:
            obj = json.loads(r.read().decode())
        assert [m["text"] for m in obj["messages"]] == ["pre_u", "pre_a"]
        assert obj["has_more"] is False
    finally:
        srv.shutdown()


def test_newsession_worker_end_to_end(qapp):
    """Full path over a real socket: guest POST /newsession → relay → worker GET
    /newsessions → sig_new_session. Guards the worker<->relay integration the
    fake-_req tests skip (Issue #81).

    The response key "requests", the item fields ds/name/mid and the route path
    live as three independent literal sets (local_relay.py, relay.py, and the
    stubbed payloads in the fake-_req tests), so a one-sided rename would leave
    every other test green while the guest's "+" silently stops working."""
    import hashlib
    import urllib.request
    import meeting.local_relay as lr
    import meeting.relay as mr
    srv = lr.start_server("ADMIN")
    try:
        base = f"http://127.0.0.1:{srv.port}"
        secret = "sek"
        sh = hashlib.sha256(secret.encode()).hexdigest()
        srv.state.handle("POST", "/admin/channel", {"Authorization": "Bearer ADMIN"},
                         json.dumps({"ch": "ch", "ttl_sec": 3600, "secret_hash": sh}).encode())
        req = urllib.request.Request(
            base + "/newsession/ch?ds=dsA",
            data=json.dumps({"name": "Bob"}).encode(), method="POST",
            headers={"Authorization": "Bearer " + secret,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert json.loads(r.read().decode())["ok"] is True
        w = mr._RelayWorker(base, "ADMIN", "ch")
        got = []
        w.sig_new_session.connect(lambda ds, n: got.append((ds, n)))
        w._do_new_sessions()
        assert got == [("dsA", "Bob")]   # 応答キー・フィールド名・パスの三者一致
        w._do_new_sessions()
        assert got == [("dsA", "Bob")]   # 実サーバ相手でも mid dedup が効く
    finally:
        srv.shutdown()


def test_backfill_runs_after_hot_reload_without_field(qapp, monkeypatch):
    # Simulate a mid-meeting scope=patch hot-reload: the running instance predates
    # the _backfilled_ids field. The capture tick must recreate it and still backfill.
    chat = FakeChat([{"id": "a", "title": "A", "busy": False, "dataset": "ds1"}],
                    messages={"a": [FakeMsg("user", "q1")]})
    win = FakeWindow(chat, dataset="ds1", tabs=[])
    mr, r = _make_relay(monkeypatch, win)
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)
    del r._backfilled_ids                 # field absent (pre-reload instance)
    r._worker._outbox.clear()
    r._on_capture_tick()                  # must not raise; must backfill
    assert any(i["kind"] == "backlog" for i in r._worker._outbox)
    assert r._backfilled_ids == {"a"}
    r.stop()


# ---- tabs: new tabs auto-share, deselected tabs stay out ----

def test_tab_auto_share(qapp, monkeypatch):
    chat = FakeChat()
    win = FakeWindow(chat, tabs=["t1", "t2"])
    mr, r = _make_relay(monkeypatch, win)
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)
    assert r.published_tabs() == {("", "t1"), ("", "t2")}

    # a tab opened mid-meeting auto-joins the published set (default-share).
    win._tabs = ["t1", "t2", "t3"]
    assert r.absorb_new_tabs() == [("", "t3")]
    assert r.published_tabs() == {("", "t1"), ("", "t2"), ("", "t3")}
    r._on_capture_tick()
    tab_puts = [i for i in r._worker._outbox if i["kind"] == "tabs"]
    assert tab_puts and set(tab_puts[-1]["data"]["tabs_by_dataset"][""]) == {"t1", "t2", "t3"}
    assert tab_puts[-1]["data"]["active_dataset"] is None   # dataset-less window

    # an explicitly deselected tab is NOT re-added on the next absorb.
    r.set_published_tabs([("", "t1"), ("", "t3")])   # host unchecks t2
    assert r.absorb_new_tabs() == []                 # t2 is known, not re-absorbed
    assert r.published_tabs() == {("", "t1"), ("", "t3")}

    # a brand-new tab still auto-shares even after a prior deselect.
    win._tabs = ["t1", "t2", "t3", "t4"]
    assert r.absorb_new_tabs() == [("", "t4")]
    assert r.published_tabs() == {("", "t1"), ("", "t3"), ("", "t4")}
    r.stop()


def test_publish_scope_all_dataset_tabs(qapp, monkeypatch):
    """Issue #78: tabs in ALL open datasets are default-shared, keyed by (ds, name);
    a new tab in any dataset auto-joins; the payload buckets names per dataset."""
    chat = FakeChat()
    win = FakeWindow(chat, dataset="dsA", tabs=["tA", "tB"],
                     tab_datasets={"tA": "dsA", "tB": "dsB"})
    mr, r = _make_relay(monkeypatch, win)
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)
    # both datasets' tabs are published (Issue #78).
    assert r.published_tabs() == {("dsA", "tA"), ("dsB", "tB")}
    r._on_capture_tick()
    tab_puts = [i for i in r._worker._outbox if i["kind"] == "tabs"]
    tbd = tab_puts[-1]["data"]["tabs_by_dataset"]
    assert tbd == {"dsA": ["tA"], "dsB": ["tB"]}
    assert tab_puts[-1]["data"]["active_dataset"] == "dsA"
    assert set(tab_puts[-1]["data"]["datasets"]) == {"dsA", "dsB"}

    # a new tab in EITHER dataset auto-joins.
    win._tabs = ["tA", "tB", "tA2", "tC"]
    win._tab_datasets.update({"tA2": "dsA", "tC": "dsB"})
    assert set(r.absorb_new_tabs()) == {("dsA", "tA2"), ("dsB", "tC")}
    assert r.published_tabs() == {("dsA", "tA"), ("dsB", "tB"),
                                  ("dsA", "tA2"), ("dsB", "tC")}

    # switching the active dataset does not drop any DS's tabs; only active_dataset
    # in the payload changes.
    win.current_dataset = "dsB"
    r._worker._outbox.clear()
    r._on_capture_tick()
    tab_puts = [i for i in r._worker._outbox if i["kind"] == "tabs"]
    tbd = tab_puts[-1]["data"]["tabs_by_dataset"]
    assert set(tbd["dsA"]) == {"tA", "tA2"} and set(tbd["dsB"]) == {"tB", "tC"}
    assert tab_puts[-1]["data"]["active_dataset"] == "dsB"
    r.stop()


def test_all_ds_tab_pairs_none_safe(qapp, monkeypatch):
    """Issue #78: a dataset-less tab (session_spec dataset None) maps to the ""
    (null-group) key in _all_ds_tab_pairs without crashing."""
    chat = FakeChat()
    win = FakeWindow(chat, dataset=None, tabs=["v"], tab_datasets={"v": None})
    mr, r = _make_relay(monkeypatch, win)
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)
    assert r.published_tabs() == {("", "v")}
    r.stop()


# ---- DS layer: tabs payload carries the active dataset (guest DS bar) ----

def test_tabs_payload_carries_dataset(qapp, monkeypatch):
    chat = FakeChat()
    win = FakeWindow(chat, dataset="dsA", tabs=["t1"], tab_datasets={"t1": "dsA"})
    mr, r = _make_relay(monkeypatch, win)
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)
    tab_puts = [i for i in r._worker._outbox if i["kind"] == "tabs"]
    assert tab_puts and tab_puts[-1]["data"] == {
        "active_dataset": "dsA", "datasets": ["dsA"], "tabs_by_dataset": {"dsA": ["t1"]}}
    r.stop()

    # dataset-less window → active_dataset None, null-group "" key in the payload
    win2 = FakeWindow(chat, dataset=None, tabs=["v"], tab_datasets={"v": None})
    mr2, r2 = _make_relay(monkeypatch, win2)
    r2.meeting_start(3600)
    tab_puts = [i for i in r2._worker._outbox if i["kind"] == "tabs"]
    assert tab_puts and tab_puts[-1]["data"] == {
        "active_dataset": None, "datasets": [""], "tabs_by_dataset": {"": ["v"]}}
    r2.stop()


def test_dataset_switch_keeps_per_ds_view_caches(qapp, monkeypatch):
    """Issue #78: view caches are keyed by (ds, name), and the server no longer
    wipes its view store on a dataset switch — so a switch must NOT drop the whole
    cache. dsA's (dsA, t1) entry survives while dsB is active; same-named dsB tab
    keeps its own independent (dsB, t1) entry."""
    chat = FakeChat()
    win = FakeWindow(chat, dataset="dsA", tab_objs=[("t1", "dsA"), ("t1", "dsB")])
    mr, r = _make_relay(monkeypatch, win)
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)
    r._view_hashes = {("dsA", "t1"): b"h"}       # as if t1@dsA had been captured
    r._view_cachekeys = {("dsA", "t1"): 1}
    win.current_dataset = "dsB"
    r._on_capture_tick()
    # dsA's cache entry is NOT wiped (no server-side view wipe anymore)
    assert r._view_hashes.get(("dsA", "t1")) == b"h"
    r.stop()


def test_dataset_switch_republishes_identical_tab_list(qapp, monkeypatch):
    """Two datasets each publishing the same tab NAME: the switch must still
    re-PUT (active_dataset rides in the change-detect JSON) — pre-DS-layer the
    identical name list suppressed the publish and guests never saw the switch."""
    chat = FakeChat()
    win = FakeWindow(chat, dataset="dsA", tab_objs=[("t1", "dsA"), ("t1", "dsB")])
    mr, r = _make_relay(monkeypatch, win)
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)
    tab_puts = [i for i in r._worker._outbox if i["kind"] == "tabs"]
    assert tab_puts and tab_puts[-1]["data"]["active_dataset"] == "dsA"
    r._worker._outbox.clear()           # deliberately NOT resetting _last_tabs_json
    win.current_dataset = "dsB"
    r._on_capture_tick()
    # active_dataset changed → the change-detect JSON differs → re-PUT even though
    # tabs_by_dataset is identical across the switch.
    tab_puts = [i for i in r._worker._outbox if i["kind"] == "tabs"]
    assert tab_puts and tab_puts[-1]["data"]["active_dataset"] == "dsB"
    assert tab_puts[-1]["data"]["tabs_by_dataset"] == {"dsA": ["t1"], "dsB": ["t1"]}
    r.stop()


def test_stream_delivered_across_ds_switch(qapp, monkeypatch):
    """Issue #78: a published session's stream/final/later reply all flow even
    after a dataset switch (DS is no longer a gate). An explicitly unshared
    session (sid dropped from _published_session_ids) is still withheld."""
    chat = FakeChat([{"id": "a", "title": "A", "busy": False, "dataset": "dsA"}])
    win = FakeWindow(chat, dataset="dsA")
    mr, r = _make_relay(monkeypatch, win)
    w = mr._RelayWorker("http://relay.test", "ADMIN", "ch")
    r._worker = w
    r._sharing = True
    r._published_session_ids = {"a"}

    r._on_message_streaming("a", "Hel", "local", "st1")       # public partial
    assert [i["body"]["partial"] for i in w._outbox] == [True]
    w._outbox.clear()

    win.current_dataset = "dsB"                                # switch mid-stream
    r._stream_last_pub.clear()                                 # bypass 0.7s throttle
    r._on_message_streaming("a", "Hello wor", "local", "st1")  # still delivered
    assert [i["body"]["partial"] for i in w._outbox] == [True]
    w._outbox.clear()
    r._on_message_added("a", "assistant", "Hello world", "local")   # final delivered
    outs = [i for i in w._outbox if i["kind"] == "out"]
    assert len(outs) == 1
    assert outs[0]["body"]["stream_id"] == "st1" and outs[0]["body"]["partial"] is False
    assert outs[0]["body"]["text"] == "Hello world"
    w._outbox.clear()

    # a later, non-streamed reply of the (background-DS) session still flows
    r._on_message_added("a", "assistant", "later reply", "local")
    assert [i["sid"] for i in w._outbox] == ["a"]
    w._outbox.clear()

    # an explicitly unshared session's final is withheld — the one remaining gate.
    r._on_message_streaming("a", "x", "local", "st2")
    assert r._stream_ids == {"a": "st2"}
    w._outbox.clear()
    r._published_session_ids = set()
    r._on_message_added("a", "assistant", "unshared final", "local")
    assert not w._outbox


def test_tabs_sendfail_resets_latch(qapp, monkeypatch):
    """A dropped tabs PUT (network hiccup) must reset the change-detect latch so
    the next tick re-sends. Issue #78: the whole-view-cache wipe on a tabs failure
    is GONE (no server-side dataset-switch view wipe to recover from) — only the
    tabs/sessions latches reset."""
    chat = FakeChat()
    win = FakeWindow(chat, dataset="dsA", tabs=["t1"], tab_datasets={"t1": "dsA"})
    mr, r = _make_relay(monkeypatch, win)
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)
    assert r._last_tabs_json is not None
    assert any(i["kind"] == "tabs" for i in r._worker._outbox)
    # a background-DS view cache entry must survive a tabs failure (not wiped).
    r._view_hashes = {("dsB", "t1"): b"h"}
    r._view_cachekeys = {("dsB", "t1"): 1}

    def boom(method, path, data=None, is_png=False):
        raise OSError("network down")

    r._worker._req = boom
    r._worker._drain_outbox()          # drops every queued item, sig_sendfail per kind
    assert r._last_tabs_json is None   # latch reset (direct-connection delivery here)
    assert r._last_sessions_json is None
    assert r._view_hashes == {("dsB", "t1"): b"h"}   # NOT wiped anymore
    r._on_capture_tick()               # re-enqueues at the correct FIFO position
    assert any(i["kind"] == "tabs" for i in r._worker._outbox)

    # a dropped view PUT evicts just that (ds, tab) dedupe entry (not the whole cache)
    r._view_hashes = {("dsA", "t1"): b"a", ("dsB", "t1"): b"b"}
    r._view_cachekeys = {("dsA", "t1"): 1, ("dsB", "t1"): 2}
    r._worker.sig_sendfail.emit("view", "dsB", "t1")
    assert r._view_hashes == {("dsA", "t1"): b"a"}
    assert r._view_cachekeys == {("dsA", "t1"): 1}
    r.stop()


# ---- sessions: new sessions auto-share (Issue #48) ----

def test_session_auto_share(qapp, monkeypatch):
    chat = FakeChat([_sess("s1"), _sess("s2")])
    win = FakeWindow(chat, dataset="ds1", tabs=[])
    mr, r = _make_relay(monkeypatch, win, ui_prefs={"auto_share_new_sessions": True})
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)
    assert r.published_session_ids() == {"s1", "s2"}

    # a session created mid-meeting auto-joins the published set (default-share)
    chat._summaries.append(_sess("s3"))
    assert r.absorb_new_sessions() == ["s3"]
    assert "s3" in r.published_session_ids()
    r._last_sessions_json = None
    r._on_capture_tick()
    sess_puts = [i for i in r._worker._outbox if i["kind"] == "sessions"]
    assert sess_puts and {s["id"] for s in sess_puts[-1]["data"]} == {"s1", "s2", "s3"}

    # an explicitly deselected session is NOT re-absorbed on the next absorb
    r.set_published_sessions({"s1", "s3"})     # host unchecks s2
    assert r.absorb_new_sessions() == []
    assert r.published_session_ids() == {"s1", "s3"}

    # a brand-new session still auto-shares even after a prior deselect
    chat._summaries.append(_sess("s4"))
    assert r.absorb_new_sessions() == ["s4"]
    assert "s4" in r.published_session_ids()
    r.stop()


def test_deselect_hidden_session_persists_across_switch(qapp, monkeypatch):
    """Issue #51 / reviewer code P2 R3: a hidden dataset's session that the host
    explicitly deselected stays deselected after switching to that dataset.
    set_published_sessions records the opted-out id in _session_known so a later
    absorb_new_sessions does not treat it as undecided-new and re-share it."""
    chat = FakeChat([
        {"id": "a", "title": "A", "busy": False, "dataset": "dsA"},
        {"id": "b", "title": "B", "busy": False, "dataset": "dsB"},
    ])
    win = FakeWindow(chat, dataset="dsA")
    mr, r = _make_relay(monkeypatch, win, ui_prefs={"auto_share_new_sessions": True})
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)
    assert r.published_session_ids() == {"a", "b"}  # all DS default-shared (Issue #78)
    # host explicitly deselects b.
    r.set_published_sessions({"a"})                # b explicitly opted out
    # switch to dsB: b must NOT be auto-re-shared (deselection persists via _known).
    win.current_dataset = "dsB"
    assert r.absorb_new_sessions() == []
    assert "b" not in r.published_session_ids()
    r.stop()


def test_session_auto_share_off(qapp, monkeypatch):
    chat = FakeChat([_sess("s1")])
    win = FakeWindow(chat, dataset="ds1", tabs=[])
    mr, r = _make_relay(monkeypatch, win, ui_prefs={"auto_share_new_sessions": False})
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)
    before = r.published_session_ids()
    chat._summaries.append(_sess("s2"))
    assert r.absorb_new_sessions() == []
    assert r.published_session_ids() == before   # unchanged
    r.stop()


def test_session_auto_share_off_then_on_no_retroactive(qapp, monkeypatch):
    # Privacy invariant (P1): OFF→ON must NOT retroactively publish sessions
    # created while the toggle was OFF; only sessions created after ON auto-share.
    chat = FakeChat([_sess("s1")])
    win = FakeWindow(chat, dataset="ds1", tabs=[])
    mr, r = _make_relay(monkeypatch, win, ui_prefs={"auto_share_new_sessions": True})
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)
    assert r.published_session_ids() == {"s1"}

    r.set_auto_share_new_sessions(False)
    chat._summaries.append(_sess("s2"))        # created while OFF
    assert r.absorb_new_sessions() == []       # off → observed but not published
    assert "s2" not in r.published_session_ids()

    r.set_auto_share_new_sessions(True)        # flip back ON
    assert r.absorb_new_sessions() == []       # s2 already known → NOT retroactively published
    assert "s2" not in r.published_session_ids()

    chat._summaries.append(_sess("s3"))        # created after ON
    assert r.absorb_new_sessions() == ["s3"]
    assert "s3" in r.published_session_ids()
    r.stop()


def test_session_auto_share_off_to_on_unobserved(qapp, monkeypatch):
    # reviewer R2 P1: flip OFF→ON BEFORE any capture/refresh tick observes a session
    # created while OFF. set_auto_share_new_sessions must snapshot existing ids
    # into _session_known atomically before enabling, so the first absorb after
    # the flip does NOT publish that session.
    chat = FakeChat([_sess("s1")])
    win = FakeWindow(chat, dataset="ds1", tabs=[])
    mr, r = _make_relay(monkeypatch, win, ui_prefs={"auto_share_new_sessions": True})
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)
    r.set_auto_share_new_sessions(False)
    chat._summaries.append(_sess("s2"))        # created while OFF; NO absorb/tick in between
    r.set_auto_share_new_sessions(True)        # flip ON immediately (atomic snapshot)
    assert r.absorb_new_sessions() == []       # s2 already known → NOT published
    assert "s2" not in r.published_session_ids()
    chat._summaries.append(_sess("s3"))        # created after ON → still auto-shares
    assert r.absorb_new_sessions() == ["s3"]
    assert "s3" in r.published_session_ids()
    r.stop()


def test_new_session_note_gated_by_toggle(qapp, monkeypatch):
    # P1-C: the "not auto-shared" note must be hidden while the toggle is ON.
    chat = FakeChat([_sess("s1")])
    win = FakeWindow(chat, dataset="ds1", tabs=[])
    mr, r = _make_relay(monkeypatch, win, ui_prefs={"auto_share_new_sessions": True})
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)
    from gui.meeting_share import MeetingShareWindow
    sw = MeetingShareWindow(win, r)
    sw._timer.stop()

    # toggle OFF + a post-start, unpublished session → note visible
    r.set_auto_share_new_sessions(False)
    chat._summaries.append(_sess("s2"))
    r.absorb_new_sessions()
    sw._refresh_lists()
    assert not sw._new_session_note.isHidden()

    # toggle ON → note hidden (no contradictory "not auto-shared" message)
    r.set_auto_share_new_sessions(True)
    sw._refresh_lists()
    assert sw._new_session_note.isHidden()
    r.stop()


def test_pub_sessions_carry_dataset_and_datasets_union(qapp, monkeypatch):
    """Issue #78: every published session carries a `dataset` key (str or None),
    and a dataset=None published session unions "" into the `datasets` list so it
    is reachable from the guest UI (no unreachable-but-/msg-able published sid)."""
    chat = FakeChat([
        {"id": "a", "title": "A", "busy": False, "dataset": "ds1"},
        {"id": "n", "title": "N", "busy": False, "dataset": None},
    ])
    win = FakeWindow(chat, dataset="ds1", tabs=[])
    mr, r = _make_relay(monkeypatch, win)
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)
    r._on_capture_tick()
    sess_puts = [i for i in r._worker._outbox if i["kind"] == "sessions"]
    # invariant: every session dict has the "dataset" key
    assert all("dataset" in s for s in sess_puts[-1]["data"])
    tab_puts = [i for i in r._worker._outbox if i["kind"] == "tabs"]
    # the dataset=None session's "" key is unioned into datasets
    assert "" in tab_puts[-1]["data"]["datasets"]
    r.stop()


def test_participants_ds_gating_and_watched_render(qapp, monkeypatch):
    """Issue #78: _on_participants records watched-DS demand only from presence
    entries that actually carry "ds" (bootstrap/legacy omit it). A watched
    background DS is view-rendered on the next tick; an unwatched one gets tab
    names only (its view is skipped)."""
    chat = FakeChat()
    win = FakeWindow(chat, dataset="dsA", tab_objs=[("t", "dsA"), ("t", "dsB"), ("t", "dsC")])
    mr, r = _make_relay(monkeypatch, win)
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)

    # presence WITHOUT ds must not register a watched DS
    r._on_participants([{"pid": "p0", "name": "G", "sid": "", "tab": ""}])
    assert r._watched_ds_until == {}
    # presence WITH ds=dsB registers it as watched
    r._on_participants([{"pid": "p1", "name": "G", "sid": "", "tab": "t", "ds": "dsB"}])
    assert "dsB" in r._watched_ds_until

    # capture tick: active dsA + watched dsB are rendered; dsC (unwatched) is not.
    r._worker._outbox.clear()
    r._view_hashes = {}; r._view_cachekeys = {}   # force fresh capture (bypass dedup)
    r._on_capture_tick()
    view_ds = {i["ds"] for i in r._worker._outbox if i["kind"] == "view"}
    assert "dsA" in view_ds and "dsB" in view_ds and "dsC" not in view_ds
    r.stop()


def test_watched_render_capped(qapp, monkeypatch):
    """Issue #78: at most _MAX_WATCHED_RENDER background DSs are rendered per tick
    (plus the active DS), freshest-expiry first."""
    chat = FakeChat()
    import meeting.relay as _mr
    cap = _mr._MAX_WATCHED_RENDER
    tab_objs = [("t", "dsA")] + [("t", f"bg{i}") for i in range(cap + 3)]
    win = FakeWindow(chat, dataset="dsA", tab_objs=tab_objs)
    mr, r = _make_relay(monkeypatch, win)
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)
    import time as _t
    now = _t.monotonic()
    # all bg DSs watched, with increasing expiry (bg{cap+2} freshest)
    for i in range(cap + 3):
        r._watched_ds_until[f"bg{i}"] = now + 100 + i
    r._worker._outbox.clear()
    r._view_hashes = {}; r._view_cachekeys = {}   # force fresh capture (bypass dedup)
    r._on_capture_tick()
    view_ds = {i["ds"] for i in r._worker._outbox if i["kind"] == "view"}
    bg_rendered = {d for d in view_ds if d.startswith("bg")}
    assert "dsA" in view_ds                    # active always rendered
    assert len(bg_rendered) == cap             # capped
    # freshest-expiry DSs win the cap slots
    assert f"bg{cap + 2}" in bg_rendered and "bg0" not in bg_rendered
    r.stop()


def test_tab_optout_does_not_affect_same_name_other_ds(qapp, monkeypatch):
    """Issue #78: (ds, name) keying means opting a tab out in one dataset leaves
    the same-named tab in another dataset published."""
    chat = FakeChat()
    win = FakeWindow(chat, dataset="dsA", tab_objs=[("overview", "dsA"), ("overview", "dsB")])
    mr, r = _make_relay(monkeypatch, win)
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)
    assert r.published_tabs() == {("dsA", "overview"), ("dsB", "overview")}
    # host unchecks only dsA's overview
    r.set_published_tabs([("dsB", "overview")])
    r._on_capture_tick()
    tab_puts = [i for i in r._worker._outbox if i["kind"] == "tabs"]
    tbd = tab_puts[-1]["data"]["tabs_by_dataset"]
    assert "dsA" not in tbd and tbd.get("dsB") == ["overview"]
    r.stop()


# ---- QImage hash + PNG-bytes fallback ----

def test_qimage_hash(qapp):
    from PySide6.QtGui import QImage
    from meeting.relay import _qimage_digest
    a = QImage(8, 8, QImage.Format.Format_RGBA8888)
    a.fill(0xFF0000FF)
    b = QImage(8, 8, QImage.Format.Format_RGBA8888)
    b.fill(0xFF0000FF)
    c = QImage(8, 8, QImage.Format.Format_RGBA8888)
    c.fill(0xFF00FF00)
    assert _qimage_digest(a) == _qimage_digest(b)
    assert _qimage_digest(a) != _qimage_digest(c)


def test_capture_png_fallback(qapp, monkeypatch):
    import meeting.relay as mr
    from PySide6.QtGui import QPixmap
    chat = FakeChat()
    win = FakeWindow(chat)
    r = mr.MeetingRelay(win)
    pm = QPixmap(10, 10)
    pm.fill()

    class T:
        name = "t1"

        def grab(self):
            return pm

    monkeypatch.setattr(mr, "_qimage_digest", lambda img: None)  # simulate constBits failure
    png = r._capture_tab(T(), "t1")
    assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n"
    # identical next grab → unchanged (PNG-bytes hash matches) → None
    assert r._capture_tab(T(), "t1") is None


# ---- share window smoke ----

def test_share_window_smoke(qapp, monkeypatch):
    chat = FakeChat([{"id": "a", "title": "A", "busy": False, "dataset": "ds1"}])
    win = FakeWindow(chat, dataset="ds1", tabs=["t1"])
    mr, r = _make_relay(monkeypatch, win)
    from gui.meeting_share import MeetingShareWindow
    sw = MeetingShareWindow(win, r)
    assert sw.isHidden()
    items = [sw._ttl_combo.itemText(i) for i in range(sw._ttl_combo.count())]
    assert items == ["1h", "3h", "6h", "12h", "24h"]
    sw._name_edit.setText("Bob")
    assert r.host_name == "Bob"
    # select-all masters exist and retranslate covers their labels
    assert sw._sess_select_all is not None and sw._tab_select_all is not None
    sw.retranslate()
    # toggles + select-all (not sharing → no-op) must not raise
    sw._on_session_toggle()
    sw._on_tab_toggle()
    sw._on_select_all_sessions()
    sw._on_select_all_tabs()
    sw._timer.stop()


# ---- local mode (Issue #44): tunnel + stop ordering + stale signal ----

class _FakeServer:
    def __init__(self, port=54321, bind_host="127.0.0.1"):
        self.port = port
        self.bind_host = bind_host
        self.state = None
        self.shut = 0

    def shutdown(self):
        self.shut += 1


def _local_relay(monkeypatch, win, *, port=54321):
    mr, r = _make_relay(monkeypatch, win)
    r._remote_base = ""   # local mode
    monkeypatch.setattr(
        mr.local_relay, "start_server",
        lambda admin_key, **kw: _FakeServer(port=(kw.get("port") or port),
                                            bind_host=kw.get("bind_host", "127.0.0.1")))
    return mr, r


def test_meeting_start_local_tunnel(qapp, monkeypatch):
    chat = FakeChat([{"id": "a", "title": "A", "busy": False, "dataset": "ds1"}])
    win = FakeWindow(chat, dataset="ds1", tabs=["t1"])
    mr, r = _local_relay(monkeypatch, win, port=54321)

    seen_urls = []

    def fake_urlopen(req, timeout=None):
        seen_urls.append(req.full_url)
        return FakeResp(json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode())

    monkeypatch.setattr(mr.urllib.request, "urlopen", fake_urlopen)
    # Hermetic tunnel: run() executes synchronously, Tunnel.start returns a URL.
    monkeypatch.setattr(mr._TunnelStarter, "start", lambda self: self.run())
    monkeypatch.setattr(mr.Tunnel, "start",
                        lambda self, port, **kw: "https://x.trycloudflare.com")

    tokens = []
    r.tokenReady.connect(tokens.append)
    r.meeting_start(3600)

    assert seen_urls and seen_urls[0] == "http://127.0.0.1:54321/admin/channel"
    pad = tokens[-1] + "=" * (-len(tokens[-1]) % 4)
    obj = json.loads(base64.urlsafe_b64decode(pad))
    assert obj["base_url"] == "https://x.trycloudflare.com"
    assert r.base_url() == "https://x.trycloudflare.com"
    assert r.current_token() == tokens[-1]
    assert obj["channel"] == r.channel()
    r.stop()


def test_stop_during_starting_drops_stale_token(qapp, monkeypatch):
    chat = FakeChat()
    win = FakeWindow(chat, tabs=[])
    mr, r = _local_relay(monkeypatch, win)
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    monkeypatch.setattr(mr._TunnelStarter, "start", lambda self: None)  # no auto-fire

    tokens = []
    r.tokenReady.connect(tokens.append)
    r.meeting_start(3600)
    g = r._gen
    assert r.share_status() == "starting"

    r.stop()
    assert r.share_status() == "idle"

    r._on_tunnel_ready(g, "https://stale.trycloudflare.com")
    assert tokens == []   # stale generation → no token
    r._on_tunnel_ready(r._gen, "https://stale2.trycloudflare.com")
    assert tokens == []   # not sharing → no token


def test_tunnel_failed_clears_sharing_before_notify(qapp, monkeypatch):
    chat = FakeChat()
    win = FakeWindow(chat, tabs=[])
    mr, r = _local_relay(monkeypatch, win)
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    monkeypatch.setattr(mr._TunnelStarter, "start", lambda self: None)
    r.meeting_start(3600)

    seen = []
    r.channelStateChanged.connect(lambda s: seen.append((s, r.is_sharing())))
    r._on_tunnel_failed(r._gen, "boom")
    assert ("tunnel_failed", False) in seen
    assert r.is_sharing() is False
    assert r.share_status() == "tunnel_failed"


def test_meeting_start_reentry_is_noop(qapp, monkeypatch):
    chat = FakeChat()
    win = FakeWindow(chat, tabs=[])
    mr, r = _make_relay(monkeypatch, win)   # legacy mode
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        return FakeResp(json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode())

    monkeypatch.setattr(mr.urllib.request, "urlopen", fake_urlopen)
    r.meeting_start(3600)
    ch1 = r.channel()
    n_after_first = calls["n"]

    ret = r.meeting_start(3600)
    assert ret is None
    assert calls["n"] == n_after_first   # no new admin/channel POST
    assert r.channel() == ch1
    r.stop()


def test_host_side_ttl_expiry(qapp, monkeypatch):
    import time as _time
    chat = FakeChat()
    win = FakeWindow(chat, tabs=[])
    mr, r = _make_relay(monkeypatch, win)   # legacy mode
    methods = []

    def fake_urlopen(req, timeout=None):
        methods.append((req.get_method(), req.full_url))
        return FakeResp(json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode())

    monkeypatch.setattr(mr.urllib.request, "urlopen", fake_urlopen)
    r.meeting_start(3600)

    r._expires_at = int(_time.time()) - 1
    seen = []
    r.channelStateChanged.connect(seen.append)
    r._on_capture_tick()
    assert r.is_sharing() is False
    assert "expired" in seen
    assert any(m == "DELETE" and "/admin/channel/" in u for m, u in methods)


def test_share_window_adopts_running_token(qapp, monkeypatch):
    chat = FakeChat([{"id": "a", "title": "A", "busy": False, "dataset": "ds1"}])
    win = FakeWindow(chat, dataset="ds1", tabs=["t1"])
    mr, r = _make_relay(monkeypatch, win)   # legacy mode → synchronous token
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    tokens = []
    r.tokenReady.connect(tokens.append)
    r.meeting_start(3600)
    tok = tokens[-1]

    from gui.meeting_share import MeetingShareWindow
    sw = MeetingShareWindow(win, r)
    assert sw._token == tok
    assert sw._token_edit.text() == tok
    assert sw._state_label.text() == tr("meeting.state.sharing")
    sw._timer.stop()
    r.stop()


def test_share_url_and_copy_link(qapp, monkeypatch):
    # Issue #47: 共有 URL ヘルパ + 「リンクをコピー」ボタン
    chat = FakeChat([{"id": "a", "title": "A", "busy": False, "dataset": "ds1"}])
    win = FakeWindow(chat, dataset="ds1", tabs=["t1"])
    mr, r = _make_relay(monkeypatch, win)   # legacy → base_url() == "http://relay.test"
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    tokens = []
    r.tokenReady.connect(tokens.append)
    r.meeting_start(3600)
    tok = tokens[-1]

    from gui.meeting_share import MeetingShareWindow
    sw = MeetingShareWindow(win, r)
    sw._timer.stop()

    # happy path: guest base（末尾スラッシュ除去）+ "/#token=" + token
    assert sw._share_url() == "http://relay.test/#token=" + tok
    # コピーハンドラは _share_url() の薄いラッパ。例外を出さないこと（スモーク）
    sw._on_copy_link()
    # token 無し → 空 URL（クラッシュしない）
    sw._token = ""
    assert sw._share_url() == ""
    # base_url 未確定（異常系）でも相対 URL を返さない → 空（変更2-1 のガードを通す）
    sw._token = "x"
    monkeypatch.setattr(r, "base_url", lambda: "")
    assert sw._share_url() == ""
    r.stop()   # relay 後始末。姉妹テスト（test_share_window_adopts_running_token 末尾）と同様、冪等


def test_copy_buttons_flash_copied(qapp, monkeypatch):
    # Issue #58: コピー2ボタンに1秒間「コピー完了」フィードバック
    chat = FakeChat([{"id": "a", "title": "A", "busy": False, "dataset": "ds1"}])
    win = FakeWindow(chat, dataset="ds1", tabs=["t1"])
    mr, r = _make_relay(monkeypatch, win)
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    tokens = []
    r.tokenReady.connect(tokens.append)
    r.meeting_start(3600)
    tok = tokens[-1]

    from gui.meeting_share import MeetingShareWindow
    sw = MeetingShareWindow(win, r)
    sw._timer.stop()   # 姉妹テスト同様、周期リフレッシュを止める
    assert sw._token == tok

    # (1) リンクをコピー（happy path + 言語切替ガード）
    sw._on_copy_link()
    assert qapp.clipboard().text() == sw._share_url()
    assert sw._copy_link_btn.text() == tr("meeting.btn.copied")
    assert sw._copy_link_feedback.isActive()
    assert sw._copy_link_feedback.interval() == 1000
    # フィードバック中に言語切替(retranslate)が来ても「コピー完了」(現言語)を維持する
    sw.retranslate()
    assert sw._copy_link_btn.text() == tr("meeting.btn.copied")
    # 発火を決定的に再現: emit で復元 → stop で active を落とす
    sw._copy_link_feedback.timeout.emit()
    sw._copy_link_feedback.stop()
    assert sw._copy_link_btn.text() == tr("meeting.btn.copy_link")
    assert not sw._copy_link_feedback.isActive()
    # 復元後の retranslate は通常ラベルを設定する
    sw.retranslate()
    assert sw._copy_link_btn.text() == tr("meeting.btn.copy_link")

    # (2) コピー（トークン、happy path）
    sw._on_copy()
    assert qapp.clipboard().text() == sw._token
    assert sw._copy_btn.text() == tr("meeting.btn.copied")
    assert sw._copy_feedback.isActive()
    assert sw._copy_feedback.interval() == 1000
    sw._copy_feedback.timeout.emit()
    sw._copy_feedback.stop()
    assert sw._copy_btn.text() == tr("meeting.btn.copy")
    assert not sw._copy_feedback.isActive()

    # (3) 空トークン → フィードバックなし（この時点で両タイマーとも inactive）
    sw._token = ""
    sw._on_copy()
    sw._on_copy_link()
    assert sw._copy_btn.text() == tr("meeting.btn.copy")
    assert sw._copy_link_btn.text() == tr("meeting.btn.copy_link")
    assert not sw._copy_feedback.isActive()
    assert not sw._copy_link_feedback.isActive()

    # (4) 後始末
    sw._copy_feedback.stop()
    sw._copy_link_feedback.stop()
    r.stop()   # relay 後始末（冪等。姉妹テスト末尾と同様）


# ---- Issue #81: guest-requested new chat sessions ----

def test_worker_new_sessions_emit_and_dedup(qapp):
    import meeting.relay as mr
    w = mr._RelayWorker("http://x", "k", "ch")
    got = []
    w.sig_new_session.connect(lambda ds, n: got.append((ds, n)))
    payload = {"requests": [{"ds": "dsA", "name": "Bob", "mid": _mid(2000)}],
               "server_now_ms": 2000}

    def fake_req(method, path, data=None, is_png=False):
        return FakeResp(json.dumps(payload).encode())

    w._req = fake_req
    w._do_new_sessions()
    w._do_new_sessions()
    assert got == [("dsA", "Bob")]
    assert w._ns_reanchor is False


def test_new_sessions_failure_does_not_advance_cursor(qapp):
    import meeting.relay as mr
    w = mr._RelayWorker("http://x", "k", "ch")
    w._ns_reanchor = False
    w._ns_latest_seen_ts = 500

    def boom(method, path, data=None, is_png=False):
        raise OSError("network down")

    w._req = boom
    w._do_new_sessions()
    assert w._ns_latest_seen_ts == 500


def test_new_sessions_expired_emits_state(qapp):
    import meeting.relay as mr
    w = mr._RelayWorker("http://x", "k", "ch")
    states = []
    w.sig_state.connect(states.append)

    def gone(method, path, data=None, is_png=False):
        raise urllib.error.HTTPError("http://x", 410, "gone", {}, io.BytesIO(b""))

    w._req = gone
    w._do_new_sessions()
    assert "expired" in states


def test_new_session_request_publishes_regardless_of_toggle(qapp, monkeypatch):
    chat = FakeChat([_sess("a")])
    win = FakeWindow(chat, dataset="ds1")
    _mr, r = _make_relay(monkeypatch, win,
                         ui_prefs={"auto_share_new_sessions": False})
    r._sharing = True
    r._published_session_ids = {"a"}
    r._session_known = {"a"}
    r._on_new_session_request("ds1", "Bob")
    assert chat.created == ["ds1"]
    sid = "new1"
    assert sid in r.published_session_ids()
    assert sid in r._session_known


def test_new_session_request_rejects_null_group(qapp, monkeypatch, caplog):
    chat = FakeChat([_sess("a")])
    win = FakeWindow(chat, dataset="ds1")
    _mr, r = _make_relay(monkeypatch, win)
    r._sharing = True
    r._published_session_ids = {"a"}
    before = r.published_session_ids()
    with caplog.at_level(logging.WARNING, logger="meeting.relay"):
        r._on_new_session_request("", "Bob")
    hits = [rec for rec in caplog.records
            if "the null group is not a creatable target" in rec.getMessage()]
    assert len(hits) == 1
    assert chat.created == []
    assert r.published_session_ids() == before


def test_new_session_request_unknown_ds_falls_back(qapp, monkeypatch):
    chat = FakeChat([_sess("a")])
    win = FakeWindow(chat, dataset="ds1")
    _mr, r = _make_relay(monkeypatch, win)
    r._sharing = True
    r._published_session_ids = {"a"}
    r._on_new_session_request("bogus", "Bob")
    assert chat.created == ["ds1"]


def test_new_session_request_accepts_closed_dataset_with_sessions(qapp, monkeypatch):
    chat = FakeChat([
        _sess("a"),
        {"id": "c", "title": "C", "busy": False, "dataset": "dsClosed"},
    ])
    win = FakeWindow(chat, dataset="ds1")
    _mr, r = _make_relay(monkeypatch, win)
    r._sharing = True
    r._published_session_ids = {"a", "c"}
    r._on_new_session_request("dsClosed", "Bob")
    assert chat.created == ["dsClosed"]


def test_new_session_request_accepts_published_tab_only_dataset(qapp, monkeypatch):
    chat = FakeChat([_sess("a")])
    win = FakeWindow(chat, dataset="ds1")
    _mr, r = _make_relay(monkeypatch, win)
    r._sharing = True
    r._published_session_ids = {"a"}
    r._published_tabs = {("dsTabOnly", "t1")}
    r._on_new_session_request("dsTabOnly", "Bob")
    assert chat.created == ["dsTabOnly"]


def test_new_session_request_rejects_opted_out_dataset(qapp, monkeypatch):
    chat = FakeChat([
        _sess("a"),
        {"id": "o", "title": "O", "busy": False, "dataset": "dsOut"},
    ])
    win = FakeWindow(chat, dataset="ds1")
    _mr, r = _make_relay(monkeypatch, win)
    r._sharing = True
    r._published_session_ids = {"a"}       # "o" opted out → no dsOut chip
    r._on_new_session_request("dsOut", "Bob")
    assert chat.created == ["ds1"]


def test_new_session_request_ignored_when_not_sharing(qapp, monkeypatch):
    chat = FakeChat([_sess("a")])
    win = FakeWindow(chat, dataset="ds1")
    _mr, r = _make_relay(monkeypatch, win)
    r._sharing = False
    r._published_session_ids = {"a"}
    before = r.published_session_ids()
    r._on_new_session_request("ds1", "Bob")
    assert chat.created == []
    assert r.published_session_ids() == before


def test_new_session_request_no_active_dataset_drops(qapp, monkeypatch, caplog):
    chat = FakeChat()
    win = FakeWindow(chat, dataset=None)
    _mr, r = _make_relay(monkeypatch, win)
    r._sharing = True
    with caplog.at_level(logging.WARNING, logger="meeting.relay"):
        r._on_new_session_request("bogus", "Bob")
    hits = [rec for rec in caplog.records
            if "the host has no current dataset to fall back to" in rec.getMessage()]
    assert len(hits) == 1
    assert chat.created == []


def test_new_session_survives_capture_tick(qapp, monkeypatch):
    import meeting.relay as mr
    chat = FakeChat([_sess("a")])
    win = FakeWindow(chat, dataset="ds1")
    _mr, r = _make_relay(monkeypatch, win,
                         ui_prefs={"auto_share_new_sessions": False})
    r._sharing = True
    r._published_session_ids = {"a"}
    r._session_known = {"a"}
    r._on_new_session_request("ds1", "Bob")
    sid = "new1"
    r._worker = mr._RelayWorker("http://x", "ADMIN", "ch")
    r._on_capture_tick()
    assert sid in r.published_session_ids()


def test_shareable_ds_keys_order(qapp, monkeypatch):
    chat = FakeChat()
    win = FakeWindow(chat, dataset="ds1")
    _mr, r = _make_relay(monkeypatch, win)
    r._published_tabs = {("dsTabOnly", "t1")}
    sessions = [
        {"id": "a", "dataset": "ds1"},        # already open → no duplicate
        {"id": "b", "dataset": "dsSess"},     # session-derived extra
        {"id": "c", "dataset": None},         # null group
    ]
    assert r._shareable_ds_keys(sessions) == ["ds1", "dsSess", "", "dsTabOnly"]


# ---- Issue #85: LAN 直結リンク ----

def _lan_urlopen(mr, monkeypatch):
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 9999999999, "server_now_ms": 1}).encode()))
    monkeypatch.setattr(mr._TunnelStarter, "start", lambda self: None)


def test_lan_tunnel_failure_is_non_fatal(qapp, monkeypatch):
    chat = FakeChat()
    win = FakeWindow(chat, tabs=[])
    mr, r = _local_relay(monkeypatch, win)
    _lan_urlopen(mr, monkeypatch)
    r.meeting_start(3600, lan=True, host_ip="192.168.1.50")
    r._on_tunnel_failed(r._gen, "boom")
    assert r.is_sharing() is True
    assert r.share_status() == "external_unavailable"
    assert r.channel() is not None
    link = r.lan_link()
    assert link and "http://192.168.1.50:" in link and "/#token=" in link
    assert r.current_token() == ""          # 外部専用: LAN トークンは漏れない
    assert r._tunnel is None
    r.stop()


def test_lan_env_host_fallback(qapp, monkeypatch):
    monkeypatch.setenv("RELAY_LAN_HOST", "10.0.0.7")
    chat = FakeChat()
    win = FakeWindow(chat, tabs=[])
    mr, r = _local_relay(monkeypatch, win)
    _lan_urlopen(mr, monkeypatch)
    r.meeting_start(3600, lan=True)          # host_ip 省略 → env フォールバック
    link = r.lan_link()
    assert "http://10.0.0.7:" in link and "/#token=" in link
    r.stop()


def test_lan_rebind_new_before_old_shutdown(qapp, monkeypatch):
    chat = FakeChat()
    win = FakeWindow(chat, tabs=[])
    mr, r = _local_relay(monkeypatch, win)
    _lan_urlopen(mr, monkeypatch)
    r.meeting_start(3600)                    # loopback
    r.stop()                                 # loopback は畳まれない
    assert r._local_server is not None
    old = r._local_server
    r.meeting_start(3600, lan=True, host_ip="192.168.1.50")
    assert old.shut == 1                     # 新生成後に旧を畳む
    assert r._local_server.bind_host == "0.0.0.0"
    r.stop()


def test_stop_folds_public_server(qapp, monkeypatch):
    chat = FakeChat()
    win = FakeWindow(chat, tabs=[])
    mr, r = _local_relay(monkeypatch, win)
    _lan_urlopen(mr, monkeypatch)
    r.meeting_start(3600, lan=True, host_ip="192.168.1.50")
    srv = r._local_server
    assert srv.bind_host == "0.0.0.0"
    r.stop()
    assert srv.shut == 1 and r._local_server is None
    # loopback 共有では stop() 後も残る（app-scoped）
    r.meeting_start(3600)
    assert r._local_server is not None
    r.stop()
    assert r._local_server is not None


def test_expired_folds_public_server(qapp, monkeypatch):
    chat = FakeChat()
    win = FakeWindow(chat, tabs=[])
    mr, r = _local_relay(monkeypatch, win)
    _lan_urlopen(mr, monkeypatch)
    r.meeting_start(3600, lan=True, host_ip="192.168.1.50")
    srv = r._local_server
    r._on_state("expired")
    assert srv.shut == 1 and r._local_server is None
    # loopback 共有では expired でも残る
    r.meeting_start(3600)
    assert r._local_server is not None
    r._on_state("expired")
    assert r._local_server is not None
    r.stop()


def test_post_failure_folds_public_server(qapp, monkeypatch):
    chat = FakeChat()
    win = FakeWindow(chat, tabs=[])
    mr, r = _make_relay(monkeypatch, win)
    r._remote_base = ""
    created = []

    def fake_start(admin_key, **kw):
        s = _FakeServer(port=54321, bind_host=kw.get("bind_host", "127.0.0.1"))
        created.append(s)
        return s

    monkeypatch.setattr(mr.local_relay, "start_server", fake_start)
    monkeypatch.setattr(mr._TunnelStarter, "start", lambda self: None)

    def boom(req, timeout=None):
        raise OSError("post fail")

    monkeypatch.setattr(mr.urllib.request, "urlopen", boom)
    with pytest.raises(OSError):
        r.meeting_start(3600, lan=True, host_ip="192.168.1.50")
    assert r._local_server is None
    assert created and created[-1].shut == 1
    assert r.is_sharing() is False

    # loopback: POST 失敗でも app-scoped は残る（fold は no-op）
    created.clear()
    with pytest.raises(OSError):
        r.meeting_start(3600)
    assert r._local_server is not None


def test_lan_base_url_strict_gate(qapp, monkeypatch):
    chat = FakeChat()
    win = FakeWindow(chat, tabs=[])
    mr, r = _make_relay(monkeypatch, win)
    # LAN 再バインド失敗後の壊れた状態を直接組み立てる: 旧 loopback が残存
    r._sharing = False
    r._lan = True
    r._lan_host = "192.168.1.50"
    r._channel = "c"
    r._secret = "s"
    r._local_server = _FakeServer(bind_host="127.0.0.1")
    assert r.lan_base_url() == ""
    assert r.lan_link() == ""
    # 共有中でも loopback bind なら空（bind_host ゲート）
    r._sharing = True
    assert r.lan_base_url() == ""
    # 正常な LAN 共有中（public bind）では非空
    r._local_server = _FakeServer(bind_host="0.0.0.0", port=54321)
    assert r.lan_base_url() == "http://192.168.1.50:54321"


def test_last_tunnel_failed_lifecycle(qapp, monkeypatch):
    # external_unavailable の後、手動停止/TTL 失効は idle に落ちる
    chat = FakeChat()
    win = FakeWindow(chat, tabs=[])
    mr, r = _local_relay(monkeypatch, win)
    _lan_urlopen(mr, monkeypatch)
    r.meeting_start(3600, lan=True, host_ip="192.168.1.50")
    r._on_tunnel_failed(r._gen, "boom")
    assert r.share_status() == "external_unavailable"
    r.meeting_stop()
    assert r.share_status() == "idle"        # 手動停止は失敗表示にしない

    r.meeting_start(3600, lan=True, host_ip="192.168.1.50")
    r._on_tunnel_failed(r._gen, "boom")
    assert r.share_status() == "external_unavailable"
    r._on_state("expired")
    assert r.share_status() == "idle"        # TTL 失効も idle
    r.stop()


def test_flag_strict_bool(qapp):
    from llm_bridge import _flag
    assert _flag("false") is False
    assert _flag("true") is True
    assert _flag(0) is False
    assert _flag(1) is True
    with pytest.raises(ValueError):
        _flag("nonsense")


def test_primary_ip_prefers_lan(qapp, monkeypatch):
    import socket
    from gui.meeting_share import MeetingShareWindow
    chat = FakeChat()
    win = FakeWindow(chat, tabs=[])
    mr, r = _make_relay(monkeypatch, win)
    sw = MeetingShareWindow(win, r)
    sw._timer.stop()

    def gai(ips):
        return lambda host, *a, **kw: [
            (socket.AF_INET, None, None, "", (ip, 0)) for ip in ips]

    monkeypatch.setattr(socket, "getaddrinfo",
                        gai(["25.0.0.1", "192.168.1.50", "127.0.0.1"]))
    assert sw._primary_ip() == "192.168.1.50"
    monkeypatch.setattr(socket, "getaddrinfo", gai(["127.0.0.1", "169.254.1.1"]))
    assert sw._primary_ip() == ""
    r.stop()
