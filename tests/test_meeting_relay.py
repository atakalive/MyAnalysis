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


class FakeChat:
    def __init__(self, summaries=None):
        self._summaries = summaries or []
        self.injected = []
        self._pending_remote = {}

    def session_summaries(self):
        return list(self._summaries)

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


def _make_relay(monkeypatch, win):
    import meeting.relay as mr
    monkeypatch.setattr(mr._RelayWorker, "start", lambda self: None)
    r = mr.MeetingRelay(win)
    r._base_url = "http://relay.test"
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
        return FakeResp(json.dumps({"expires_at": 999, "server_now_ms": 123}).encode())

    monkeypatch.setattr(mr.urllib.request, "urlopen", fake_urlopen)

    token = r.meeting_start(999999)   # clamp to 86400

    body = json.loads(captured["body"])
    assert len(body["secret_hash"]) == 64
    assert body["ttl_sec"] == 86400
    assert captured["method"] == "POST"
    assert captured["auth"] == "Bearer ADMIN"
    assert r.expires_at() == 999
    # default publish scope = ALL sessions across every dataset
    assert r.published_session_ids() == {"a", "b"}

    pad = token + "=" * (-len(token) % 4)
    obj = json.loads(base64.urlsafe_b64decode(pad))
    assert obj["channel"] == r.channel()
    assert obj["base_url"] == "http://relay.test"
    assert obj["secret"]
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
                            json.dumps({"expires_at": 1, "server_now_ms": 1}).encode()))
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
                            json.dumps({"expires_at": 1, "server_now_ms": 1}).encode()))
    r.meeting_start(3600)

    def boom():
        raise RuntimeError("grab failed")

    monkeypatch.setattr(chat, "session_summaries", boom)
    r._on_capture_tick()   # must not raise
    assert r._sharing is True
    r.stop()


# ---- tabs: new tabs auto-share, deselected tabs stay out ----

def test_tab_auto_share(qapp, monkeypatch):
    chat = FakeChat()
    win = FakeWindow(chat, tabs=["t1", "t2"])
    mr, r = _make_relay(monkeypatch, win)
    monkeypatch.setattr(mr.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            json.dumps({"expires_at": 1, "server_now_ms": 1}).encode()))
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
