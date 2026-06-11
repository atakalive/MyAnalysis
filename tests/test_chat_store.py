"""Tests for llm_bridge.chat_store (Qt-free): chat session serialization,
per-file IO round-trip, dataset load, delete, and merge rules."""

from __future__ import annotations

import json

import pytest

from llm_backend.base import Message
from llm_bridge.chat_store import (
    ChatSession,
    SCHEMA_VERSION,
    delete_session_file,
    load_dataset_sessions,
    merge_sessions,
    message_from_dict,
    message_to_dict,
    new_session,
    read_session_file,
    session_from_dict,
    session_to_dict,
    write_session_file,
)


# ---- Message ⇄ dict ----


def test_message_roundtrip_system_user():
    for m in (
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
    ):
        assert message_from_dict(message_to_dict(m)) == m


def test_message_roundtrip_assistant_tool_calls():
    m = Message(
        role="assistant",
        content="calling",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "foo", "arguments": '{"x": 1}'},
            }
        ],
    )
    assert message_from_dict(message_to_dict(m)) == m


def test_message_roundtrip_tool():
    m = Message(role="tool", content="result", tool_call_id="c1")
    assert message_from_dict(message_to_dict(m)) == m


def test_message_from_dict_bad_role_raises():
    with pytest.raises(ValueError):
        message_from_dict({"role": "nonsense", "content": "x"})


# ---- ChatSession ⇄ dict ----


def _sample_session() -> ChatSession:
    return ChatSession(
        id="abc123",
        title="my chat",
        messages=[
            Message(role="system", content="sys"),
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello"),
        ],
        dataset="ds_a",
        backend_name="claude-code",
        backend_session_id="sess-xyz",
        created=100.0,
        updated=200.0,
    )


def test_session_roundtrip():
    sess = _sample_session()
    d = session_to_dict(sess)
    assert d["version"] == SCHEMA_VERSION
    assert session_from_dict(d) == sess


def test_session_from_dict_missing_version_raises():
    d = session_to_dict(_sample_session())
    del d["version"]
    with pytest.raises(ValueError):
        session_from_dict(d)


def test_session_from_dict_wrong_version_raises():
    d = session_to_dict(_sample_session())
    d["version"] = 999
    with pytest.raises(ValueError):
        session_from_dict(d)


def test_session_from_dict_bad_role_raises():
    d = session_to_dict(_sample_session())
    d["messages"][0]["role"] = "nonsense"
    with pytest.raises(ValueError):
        session_from_dict(d)


# ---- read_session_file → None on corruption ----


def test_read_session_file_bad_json_none(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("{ not json", encoding="utf-8")
    assert read_session_file(p) is None


def test_read_session_file_bad_role_none(tmp_path):
    d = session_to_dict(_sample_session())
    d["messages"][0]["role"] = "nonsense"
    p = tmp_path / "x.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    assert read_session_file(p) is None


def test_read_session_file_wrong_version_none(tmp_path):
    d = session_to_dict(_sample_session())
    d["version"] = 999
    p = tmp_path / "x.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    assert read_session_file(p) is None


def test_read_session_file_missing_keys_none(tmp_path):
    for raw in ('{"version": 1}', '{"version": 1, "messages": "x"}'):
        p = tmp_path / "x.json"
        p.write_text(raw, encoding="utf-8")
        assert read_session_file(p) is None


def test_read_session_file_missing_file_none(tmp_path):
    assert read_session_file(tmp_path / "nope.json") is None


# ---- write/read disk round-trip ----


def test_write_read_disk_roundtrip(tmp_path):
    sess = _sample_session()
    write_session_file(tmp_path, sess)
    target = tmp_path / "chat_sessions" / f"{sess.id}.json"
    assert target.is_file()
    assert read_session_file(target) == sess


def test_write_session_file_no_tmp_left(tmp_path):
    write_session_file(tmp_path, _sample_session())
    assert list((tmp_path / "chat_sessions").glob("*.tmp")) == []


# ---- load_dataset_sessions ----


def test_load_dataset_sessions_sorted_and_skips_corrupt(tmp_path):
    s1 = _sample_session()
    s1.id, s1.updated = "s1", 100.0
    s2 = _sample_session()
    s2.id, s2.updated = "s2", 300.0
    write_session_file(tmp_path, s1)
    write_session_file(tmp_path, s2)
    (tmp_path / "chat_sessions" / "bad.json").write_text("{bad", encoding="utf-8")
    got = load_dataset_sessions(tmp_path)
    assert [s.id for s in got] == ["s2", "s1"]


def test_load_dataset_sessions_missing_dir_no_mkdir(tmp_path):
    assert load_dataset_sessions(tmp_path) == []
    assert not (tmp_path / "chat_sessions").exists()


# ---- delete_session_file ----


def test_delete_session_file(tmp_path):
    sess = _sample_session()
    write_session_file(tmp_path, sess)
    assert delete_session_file(tmp_path, sess.id) is True
    assert not (tmp_path / "chat_sessions" / f"{sess.id}.json").exists()


def test_delete_session_file_absent_returns_true(tmp_path):
    assert delete_session_file(tmp_path, "nonexistent") is True


# ---- merge_sessions ----


def _s(id, updated):
    return ChatSession(
        id=id,
        title=id,
        messages=[Message(role="system", content="s")],
        dataset=None,
        backend_name="mock",
        backend_session_id=None,
        created=0.0,
        updated=updated,
    )


def test_merge_adds_new_id():
    a = _s("a", 1.0)
    b = _s("b", 1.0)
    out = merge_sessions([a], [b])
    assert {s.id for s in out} == {"a", "b"}


def test_merge_replaces_when_incoming_newer():
    a = _s("a", 1.0)
    a2 = _s("a", 5.0)
    out = merge_sessions([a], [a2])
    assert len(out) == 1
    assert out[0] is a2


def test_merge_tie_keeps_existing():
    a = _s("a", 3.0)
    a_tie = _s("a", 3.0)
    out = merge_sessions([a], [a_tie])
    assert out[0] is a


def test_merge_older_keeps_existing():
    a = _s("a", 5.0)
    a_old = _s("a", 1.0)
    out = merge_sessions([a], [a_old])
    assert out[0] is a


def test_merge_does_not_mutate_inputs():
    existing = [_s("a", 1.0)]
    incoming = [_s("b", 1.0)]
    merge_sessions(existing, incoming)
    assert [s.id for s in existing] == ["a"]
    assert [s.id for s in incoming] == ["b"]


# ---- new_session ----


def test_new_session():
    sess = new_session("claude-code", "SYSTEM", dataset="ds_a")
    assert sess.backend_session_id is None
    assert sess.dataset == "ds_a"
    assert len(sess.messages) == 1
    assert sess.messages[0].role == "system"
    assert sess.messages[0].content == "SYSTEM"
