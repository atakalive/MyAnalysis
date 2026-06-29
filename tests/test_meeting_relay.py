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
        self._pending_remote = {}

    def session_summaries(self):
        return list(self._summaries)

    def _session_by_id(self, sid):
        msgs = self._messages.get(sid)
        return FakeSession(msgs) if msgs is not None else None

    def inject_remote_message(self, text, sender, session_id=None):
        self.injected.append((text, sender, session_id))


class FakeWindow:
    def __init__(self, chat, dataset=None, tabs=None):
        self._chat = chat
        self.current_dataset = dataset
        self._tabs = tabs or []

    def chat_widget(self):
        return self._chat

    def tab_names(self):
        return list(self._tabs)

    def tabs(self):
        return []

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
    # default publish scope = ALL sessions across every dataset
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
    # default = ALL sessions across every dataset
    assert r.published_session_ids() == {"a", "b"}

    # deselect one, then switch dataset: the snapshot must NOT be recomputed
    # (otherwise ds2's "b" would re-enter). Proves dataset-switch invariance.
    r.set_published_sessions({"a"})
    win.current_dataset = "ds2"
    r._last_sessions_json = None
    r._on_capture_tick()
    assert r.published_session_ids() == {"a"}
    sess_puts = [i for i in r._worker._outbox if i["kind"] == "sessions"]
    assert sess_puts and {s["id"] for s in sess_puts[-1]["data"]} == {"a"}

    # a deleted session drops out via ∩ existing.
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
    assert r.published_tabs() == {"t1", "t2"}

    # a tab opened mid-meeting auto-joins the published set (default-share).
    win._tabs = ["t1", "t2", "t3"]
    assert r.absorb_new_tabs() == ["t3"]
    assert r.published_tabs() == {"t1", "t2", "t3"}
    r._on_capture_tick()
    tab_puts = [i for i in r._worker._outbox if i["kind"] == "tabs"]
    assert tab_puts and set(tab_puts[-1]["data"]) == {"t1", "t2", "t3"}

    # an explicitly deselected tab is NOT re-added on the next absorb.
    r.set_published_tabs(["t1", "t3"])     # host unchecks t2
    assert r.absorb_new_tabs() == []       # t2 is known, not re-absorbed
    assert r.published_tabs() == {"t1", "t3"}

    # a brand-new tab still auto-shares even after a prior deselect.
    win._tabs = ["t1", "t2", "t3", "t4"]
    assert r.absorb_new_tabs() == ["t4"]
    assert r.published_tabs() == {"t1", "t3", "t4"}
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
    def __init__(self, port=54321):
        self.port = port
        self.state = None
        self.shut = 0

    def shutdown(self):
        self.shut += 1


def _local_relay(monkeypatch, win, *, port=54321):
    mr, r = _make_relay(monkeypatch, win)
    r._remote_base = ""   # local mode
    monkeypatch.setattr(mr.local_relay, "start_server",
                        lambda admin_key, **kw: _FakeServer(port=port))
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
