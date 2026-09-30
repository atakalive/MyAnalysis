"""Tests for llm_bridge.chat_search_history (Qt-free, Issue #108)."""

from __future__ import annotations

import json

import pytest

from llm_bridge import chat_search_history as h
from llm_bridge.chat_search import SearchRequest


def _req(**kw):
    base = dict(query="q", scope="dataset", dataset="dsA", include_archived=False, ai=False)
    base.update(kw)
    return SearchRequest(**base)


def _entry(**kw):
    e = h.new_entry(_req(), datasets=["dsA"], n_hits=3, session_id="sid1")
    e.update(kw)
    return e


def test_append_load_roundtrip(tmp_path):
    e1, e2 = _entry(query="a"), _entry(query="b")
    assert h.append_history(tmp_path, e1)
    assert h.append_history(tmp_path, e2)
    entries, status = h.load_history(tmp_path)
    assert status == "ok" and [e["query"] for e in entries] == ["a", "b"]
    assert (tmp_path / "chat_search_history.json.bak").exists()


def test_max_entries(tmp_path):
    for i in range(5):
        h.append_history(tmp_path, _entry(query=str(i)), max_entries=3)
    entries, _ = h.load_history(tmp_path)
    assert [e["query"] for e in entries] == ["2", "3", "4"]


def test_default_limit_500(tmp_path):
    from common.paths import durable_write_json
    durable_write_json(h.history_path(tmp_path),
                       {"version": 1, "entries": [_entry(query=str(i)) for i in range(500)]})
    h.append_history(tmp_path, _entry(query="new"))
    entries, _ = h.load_history(tmp_path)
    assert len(entries) == 500 and entries[0]["query"] == "1" and entries[-1]["query"] == "new"


def test_delete(tmp_path):
    e = _entry()
    h.append_history(tmp_path, e)
    assert h.delete_history_entry(tmp_path, e["id"])
    assert h.load_history(tmp_path)[0] == []
    assert not h.delete_history_entry(tmp_path, "nope")


def test_absent(tmp_path):
    assert h.load_history(tmp_path) == ([], "absent")


def test_unreadable_is_preserved(tmp_path):
    p = h.history_path(tmp_path)
    p.write_bytes(b"{broken")
    assert h.load_history(tmp_path) == ([], "unreadable")
    assert not h.append_history(tmp_path, _entry())
    assert p.read_bytes() == b"{broken"
    assert not h.delete_history_entry(tmp_path, "x")


def test_zero_bytes_rewritten(tmp_path):
    p = h.history_path(tmp_path)
    p.write_bytes(b"")
    (tmp_path / "chat_search_history.json.bak").write_bytes(b"")
    assert h.append_history(tmp_path, _entry())
    assert len(h.load_history(tmp_path)[0]) == 1


def test_version_2_is_unreadable(tmp_path):
    p = h.history_path(tmp_path)
    raw = json.dumps({"version": 2, "entries": []}).encode()
    p.write_bytes(raw)
    assert h.load_history(tmp_path)[1] == "unreadable"
    assert not h.append_history(tmp_path, _entry())
    assert p.read_bytes() == raw


def test_invalid_entries_dropped(tmp_path):
    good = _entry(query="good")
    bads = []
    e = _entry(); e.pop("datasets"); bads.append(e)                       # noqa: E702
    bads.append(_entry(datasets=[]))
    bads.append(_entry(datasets="dsA"))
    e = _entry(); e.pop("hint"); bads.append(e)                           # noqa: E702
    bads.append(_entry(include_archived="yes"))
    bads.append(_entry(n_hits=True))
    bads.append(_entry(n_hits=-1))
    bads.append(_entry(ts=float("nan")))
    bads.append(_entry(id=""))
    data = {"version": 1, "entries": bads[:4] + [good] + bads[4:]}
    h.history_path(tmp_path).write_text(json.dumps(data), encoding="utf-8")
    entries, status = h.load_history(tmp_path)
    assert status == "ok" and [x["query"] for x in entries] == ["good"]
    assert h.append_history(tmp_path, _entry(query="next"))
    assert [x["query"] for x in h.load_history(tmp_path)[0]] == ["good", "next"]


@pytest.mark.parametrize("ts", [1e20, -1e20, 10**400, 1e13])
def test_unconvertible_ts_dropped(tmp_path, ts):
    """有限でも日時にできない ts は load で落とす（一覧の日時表示で落ちないように）。

    10**400 は math.isfinite 自体が OverflowError になる値。load_history は never raise。"""
    assert h.valid_entry(_entry(ts=ts)) is False
    data = {"version": 1, "entries": [_entry(ts=ts, query="bad"), _entry(query="good")]}
    h.history_path(tmp_path).write_text(json.dumps(data), encoding="utf-8")
    entries, status = h.load_history(tmp_path)
    assert status == "ok" and [x["query"] for x in entries] == ["good"]


def test_new_entry_valid():
    e = h.new_entry(_req(ai=True, hint="w", scope="all", dataset=None),
                    datasets=["dsA", "dsB"], n_hits=None, session_id="s")
    assert h.valid_entry(e) and e["mode"] == "ai" and e["hint"] == "w"
    assert h.valid_entry(_entry())
    with pytest.raises(ValueError):
        h.new_entry(_req(scope="unbound", dataset=None), datasets=[], n_hits=0, session_id="s")
