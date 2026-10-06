"""Tests for llm_bridge.chat_store (Qt-free): chat session serialization,
per-file IO round-trip, dataset load, delete, and merge rules."""

from __future__ import annotations

import copy
import json

import pytest

from llm_backend.base import Message
from llm_bridge.chat_store import (
    ChatSession,
    SCHEMA_VERSION,
    _DEFAULT_TITLE,
    content_fingerprint,
    delete_session_file,
    fork_session,
    load_dataset_sessions,
    merge_sessions,
    message_from_dict,
    message_to_dict,
    new_session,
    read_session_file,
    read_session_file_status,
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
        # Left None like `draft`: the resume token is no longer persisted (it names
        # a PC-local native session), so a round-tripped object only compares equal
        # when it starts out None. Non-persistence has its own tests below.
        backend_session_id=None,
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


def test_session_order_roundtrip(tmp_path):
    sess = _sample_session()
    sess.order = 3.0
    assert session_to_dict(sess)["order"] == 3.0
    assert session_from_dict(session_to_dict(sess)).order == 3.0


def test_session_from_dict_missing_order_defaults_zero():
    d = session_to_dict(_sample_session())
    del d["order"]  # legacy file written before the order field existed
    assert session_from_dict(d).order == 0.0


def test_load_dataset_sessions_sorted_by_explicit_order(tmp_path):
    # Explicit order wins over updated: lower order = earlier, even though the
    # higher-order session was updated more recently.
    a = _sample_session()
    a.id, a.order, a.updated = "a", 0.0, 100.0
    b = _sample_session()
    b.id, b.order, b.updated = "b", 1.0, 999.0
    write_session_file(tmp_path, a)
    write_session_file(tmp_path, b)
    got = load_dataset_sessions(tmp_path)
    assert [s.id for s in got] == ["a", "b"]


# ---- delete_session_file ----


def test_delete_session_file(tmp_path):
    sess = _sample_session()
    write_session_file(tmp_path, sess)
    assert delete_session_file(tmp_path, sess.id) is True
    assert not (tmp_path / "chat_sessions" / f"{sess.id}.json").exists()


def test_delete_session_file_absent_returns_true(tmp_path):
    assert delete_session_file(tmp_path, "nonexistent") is True


# ---- durability: chat history survives a lost primary (mount) ----


def test_write_session_file_creates_bak(tmp_path):
    sess = _sample_session()
    write_session_file(tmp_path, sess)
    bak = tmp_path / "chat_sessions" / f"{sess.id}.json.bak"
    assert bak.is_file()
    assert read_session_file(bak) == sess


def test_read_session_file_recovers_from_bak(tmp_path):
    sess = _sample_session()
    write_session_file(tmp_path, sess)
    primary = tmp_path / "chat_sessions" / f"{sess.id}.json"
    primary.write_bytes(b"")   # primary evicted → 0-byte
    assert read_session_file(primary) == sess


def test_load_recovers_deleted_primary_from_bak(tmp_path):
    sess = _sample_session()
    sess.id = "s1"
    write_session_file(tmp_path, sess)
    (tmp_path / "chat_sessions" / "s1.json").unlink()   # primary 消失、.bak 残存
    got = load_dataset_sessions(tmp_path)
    assert [s.id for s in got] == ["s1"]


def test_load_bak_not_double_counted(tmp_path):
    a = _sample_session(); a.id = "a"
    b = _sample_session(); b.id = "b"
    write_session_file(tmp_path, a)
    write_session_file(tmp_path, b)
    got = load_dataset_sessions(tmp_path)
    assert sorted(s.id for s in got) == ["a", "b"]   # .bak が余分な重複を生まない


def test_delete_removes_bak_and_no_resurrect(tmp_path):
    sess = _sample_session()
    sess.id = "d1"
    write_session_file(tmp_path, sess)
    assert delete_session_file(tmp_path, "d1") is True
    assert not (tmp_path / "chat_sessions" / "d1.json").exists()
    assert not (tmp_path / "chat_sessions" / "d1.json.bak").exists()
    assert load_dataset_sessions(tmp_path) == []   # .bak から復活しない


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


def test_content_fingerprint_covers_content_but_not_order_or_dataset():
    """保存時の変化判定の指紋（Issue #106 A-3）。order と dataset は数えず、
    本文や updated は数える。正規化されて同じになる表記ゆれは同じ指紋になる。"""
    a = _sample_session()
    moved = copy.deepcopy(a)
    moved.order = a.order + 7.0
    moved.dataset = "other"
    assert content_fingerprint(moved) == content_fingerprint(a)
    body = copy.deepcopy(a)
    body.messages[-1] = Message(role="assistant", content="different")
    assert content_fingerprint(body) != content_fingerprint(a)
    stamp = copy.deepcopy(a)
    stamp.updated = (a.updated or 0.0) + 1.0
    assert content_fingerprint(stamp) != content_fingerprint(a)
    blank, none = copy.deepcopy(a), copy.deepcopy(a)
    blank.engine_model = "  "      # session_from_dict が None に正規化する
    none.engine_model = None
    assert content_fingerprint(blank) == content_fingerprint(none)
    # ディスクへ書いて読み戻した版と同じ指紋になる
    assert content_fingerprint(session_from_dict(session_to_dict(a))) == content_fingerprint(a)


def test_merge_prefer_incoming_replaces_older():
    a = _s("a", 5.0)
    b = _s("b", 1.0)
    a_old = _s("a", 1.0)
    out = merge_sessions([a, b], [a_old], prefer_incoming=True)
    assert out[0] is a_old          # 古い updated でも差し替わる
    assert [s.id for s in out] == ["a", "b"]   # 位置は既存のまま
    assert merge_sessions([a, b], [a_old])[0] is a   # 既定値では差し替わらない


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


# ---- draft: per-tab 未送信下書き（in-memory のみ／非永続） ----


def test_session_draft_defaults_empty():
    assert _sample_session().draft == ""


def test_session_draft_not_persisted():
    """draft は composer の一時状態なのでディスクに出さない: to_dict に載らず、
    reload しても復活しない（永続化するのは messages/order/tool_display まで）。"""
    sess = _sample_session()
    sess.draft = "unsent text"
    d = session_to_dict(sess)
    assert "draft" not in d
    assert session_from_dict(d).draft == ""


def test_session_draft_does_not_break_disk_roundtrip(tmp_path):
    """非永続なので、下書きの有無に関わらずディスク往復は等値のまま。"""
    sess = _sample_session()
    sess.draft = "unsent text"
    write_session_file(tmp_path, sess)
    loaded = read_session_file(tmp_path / "chat_sessions" / f"{sess.id}.json")
    assert loaded.draft == ""
    assert loaded == _sample_session()


# ---- backend_session_id: ネイティブ resume token（in-memory のみ／非永続） ----


def test_backend_session_id_not_persisted():
    """token は PC ローカルのネイティブセッションを指すので同期側に書かない。"""
    sess = _sample_session()
    sess.backend_session_id = "sess-xyz"
    d = session_to_dict(sess)
    assert "backend_session_id" not in d
    assert session_from_dict(d).backend_session_id is None


def test_backend_session_id_in_old_file_is_ignored_not_fatal():
    """旧ファイル（token 入り）は読めること。値は無視され None になるだけ。

    別 PC で作られた token をそのまま --resume に渡すのが元のバグなので、
    「読めるが使わない」が正しい。version は 1 のままなので既存チャットは消えない。
    """
    d = session_to_dict(_sample_session())
    d["backend_session_id"] = "token-from-another-pc"
    loaded = session_from_dict(d)
    assert loaded.backend_session_id is None
    assert loaded == _sample_session()          # 他のフィールドは無傷


def test_backend_session_id_does_not_break_disk_roundtrip(tmp_path):
    sess = _sample_session()
    sess.backend_session_id = "sess-xyz"
    write_session_file(tmp_path, sess)
    loaded = read_session_file(tmp_path / "chat_sessions" / f"{sess.id}.json")
    assert loaded.backend_session_id is None
    assert loaded == _sample_session()


# ---- tool_display (Issue #35) ----


def test_session_tool_display_roundtrip():
    sess = _sample_session()
    sess.tool_display = "compact"
    d = session_to_dict(sess)
    assert d["tool_display"] == "compact"
    assert session_from_dict(d) == sess


def test_session_tool_display_default_none():
    sess = _sample_session()
    assert sess.tool_display is None
    d = session_to_dict(sess)
    assert d["tool_display"] is None
    assert session_from_dict(d).tool_display is None


def test_session_from_dict_missing_tool_display_is_none():
    d = session_to_dict(_sample_session())
    del d["tool_display"]
    assert session_from_dict(d).tool_display is None


def test_session_from_dict_bad_tool_display_normalized_to_none():
    d = session_to_dict(_sample_session())
    d["tool_display"] = "bogus"
    assert session_from_dict(d).tool_display is None


# ---- fork_session (Issue #63) ----


def test_fork_session_copies_up_to_cut():
    src = _sample_session()
    src.tool_display = "compact"
    src.order = 3.0
    new = fork_session(src, 2, title="my chat (fork)")
    assert new.messages == src.messages[:2]
    assert new.id != src.id
    assert new.backend_session_id is None
    assert new.dataset == src.dataset
    assert new.backend_name == src.backend_name
    assert new.tool_display == src.tool_display
    assert new.order == src.order
    assert new.title == "my chat (fork)"


def test_fork_session_title_sentinel_verbatim():
    src = _sample_session()
    new = fork_session(src, 1, title=_DEFAULT_TITLE)
    assert new.title == _DEFAULT_TITLE


def test_fork_session_is_nondestructive_and_deepcopies():
    src = ChatSession(
        id="s1",
        title="t",
        messages=[
            Message(role="system", content="sys"),
            Message(role="assistant", content="a", tool_calls=[{"id": "x"}]),
        ],
        dataset="ds",
        backend_name="claude-code",
        backend_session_id="sess",
        created=1.0,
        updated=2.0,
    )
    new = fork_session(src, 2, title="t (fork)")
    # mutate src post-fork → fork side unaffected
    src.messages[0].content = "MUTATED"
    src.messages[1].tool_calls.append({"id": "y"})
    src.messages.append(Message(role="user", content="later"))
    assert new.messages[0].content == "sys"
    assert new.messages[1].tool_calls == [{"id": "x"}]
    assert len(new.messages) == 2
    # src itself keeps its own mutations but fork stayed independent
    assert src.backend_session_id == "sess"


# ---- archived (Issue #93) ----


def test_archived_roundtrip():
    sess = _sample_session()
    sess.archived = True
    d = session_to_dict(sess)
    assert d["archived"] is True
    assert session_from_dict(d).archived is True


def test_archived_default_false_roundtrip():
    sess = _sample_session()
    d = session_to_dict(sess)
    assert d["archived"] is False
    assert session_from_dict(d).archived is False


def test_archived_missing_key_defaults_false():
    d = session_to_dict(_sample_session())
    del d["archived"]
    assert session_from_dict(d).archived is False


def _s_arch(id, updated, archived):
    s = _s(id, updated)
    s.archived = archived
    return s


def test_merge_newer_archived_replaces():
    a = _s_arch("a", 1.0, False)
    a2 = _s_arch("a", 5.0, True)
    out = merge_sessions([a], [a2])
    assert out[0] is a2
    assert out[0].archived is True


def test_merge_older_archived_does_not_replace():
    a = _s_arch("a", 5.0, False)
    a_old = _s_arch("a", 1.0, True)
    out = merge_sessions([a], [a_old])
    assert out[0] is a
    assert out[0].archived is False


# ---- engine 上書き（セッションごとのエンジン切替） ----


def test_engine_override_roundtrip():
    sess = _sample_session()
    sess.engine, sess.engine_model = "pi", "qwen3-coder"
    sess.engine_provider = "llama.cpp"
    d = session_to_dict(sess)
    assert session_from_dict(d) == sess


def test_engine_override_defaults_none():
    s = _sample_session()
    assert (s.engine, s.engine_model, s.engine_provider) == (None, None, None)
    assert s.engine_effort is None


def test_v1_dict_without_engine_keys_roundtrips():
    """新キーを持たない既存ファイルが読めること。version を上げると
    session_from_dict が ValueError → read_session_file が None → タブから全消滅する。"""
    d = session_to_dict(_sample_session())
    for k in ("engine", "engine_model", "engine_provider", "engine_effort"):
        del d[k]
    assert d["version"] == SCHEMA_VERSION == 1
    assert session_from_dict(d) == _sample_session()


def test_engine_id_is_not_whitelisted_against_the_catalog():
    """未知のエンジン ID を消さない: 古いビルドを経由しただけで意図を壊さないため。
    妥当性は使用時に解決して既定へ縮退させる（chat_store は llm_backend 非依存）。"""
    d = session_to_dict(_sample_session())
    d["engine"] = "engine-from-a-newer-build"
    assert session_from_dict(d).engine == "engine-from-a-newer-build"


def test_engine_override_non_str_degrades_to_none():
    d = session_to_dict(_sample_session())
    d["engine"], d["engine_model"], d["engine_provider"] = 7, ["x"], "   "
    d["engine_effort"] = 3
    loaded = session_from_dict(d)
    assert (loaded.engine, loaded.engine_model, loaded.engine_provider) \
        == (None, None, None)
    assert loaded.engine_effort is None
    d["engine_effort"] = "   "
    assert session_from_dict(d).engine_effort is None


def test_engine_effort_roundtrips_and_forks():
    src = _sample_session()
    src.engine, src.engine_effort = "pi", "low"
    assert session_from_dict(session_to_dict(src)).engine_effort == "low"
    assert fork_session(src, cut=2, title="forked").engine_effort == "low"


def test_fork_carries_engine_override():
    """分岐先が既定に戻ってしまうと fork の意味が薄れる。"""
    src = _sample_session()
    src.engine, src.engine_model = "codex", "gpt-5.6-sol"
    new = fork_session(src, cut=2, title="forked")
    assert (new.engine, new.engine_model) == ("codex", "gpt-5.6-sol")
    assert new.backend_session_id is None       # resume token だけは引き継がない


# ---- persona 上書き（セッションごとの AI ペルソナ） ----


def test_persona_roundtrip():
    sess = _sample_session()
    sess.persona = "粗野な知識人"
    d = session_to_dict(sess)
    assert d["persona"] == "粗野な知識人"
    assert session_from_dict(d) == sess


def test_persona_empty_string_survives_roundtrip():
    """`""` は「明示的にペルソナなし」のセンチネル。_opt_str に通すと blank→None
    に潰れて「全体設定に従う」へ化け、タブ単位の上書き解除が消える。"""
    sess = _sample_session()
    sess.persona = ""
    d = session_to_dict(sess)
    assert d["persona"] == ""
    assert session_from_dict(d).persona == ""


def test_persona_defaults_none():
    sess = _sample_session()
    assert sess.persona is None
    d = session_to_dict(sess)
    assert d["persona"] is None
    assert session_from_dict(d).persona is None


def test_v1_dict_without_persona_key_roundtrips():
    """persona キーを持たない既存ファイルが読めること（None=全体設定に従う）。"""
    d = session_to_dict(_sample_session())
    del d["persona"]
    assert d["version"] == SCHEMA_VERSION == 1
    assert session_from_dict(d) == _sample_session()


def test_persona_non_str_degrades_to_none():
    d = session_to_dict(_sample_session())
    d["persona"] = 7
    assert session_from_dict(d).persona is None


def test_fork_carries_persona():
    """分岐先も同じ口調で続けるのが期待値。"""
    src = _sample_session()
    src.persona = "粗野な知識人"
    new = fork_session(src, cut=2, title="forked")
    assert new.persona == "粗野な知識人"


def test_read_session_file_status_ok_absent_unreadable(tmp_path):
    sess = _sample_session()
    assert read_session_file_status(tmp_path, sess.id) == ("absent", None)
    write_session_file(tmp_path, sess)
    status, got = read_session_file_status(tmp_path, sess.id)
    assert status == "ok" and got.id == sess.id
    primary = tmp_path / "chat_sessions" / f"{sess.id}.json"
    primary.unlink()                 # .bak だけ残った
    status, got = read_session_file_status(tmp_path, sess.id)
    assert status == "ok" and got.id == sess.id
    primary.write_text("", encoding="utf-8")
    (tmp_path / "chat_sessions" / f"{sess.id}.json.bak").write_text("", encoding="utf-8")
    assert read_session_file_status(tmp_path, sess.id) == ("unreadable", None)


def test_write_session_file_raises_on_oserror(tmp_path, monkeypatch):
    from common import paths as common_paths

    def boom(*a, **k):
        raise OSError("mount write failed")

    monkeypatch.setattr(common_paths, "atomic_write_text", boom)
    with pytest.raises(OSError):
        write_session_file(tmp_path, _sample_session())


# ---- kind / search_spec（Issue #108） ----


def test_kind_and_search_spec_roundtrip():
    from llm_bridge.chat_store import normalize_search_spec  # noqa: F401
    sess = _sample_session()
    sess.kind = "search"
    sess.search_spec = {"scope": "dataset", "dataset": "ds_a", "include_archived": True, "ai": True}
    d = session_to_dict(sess)
    assert d["kind"] == "search"
    got = session_from_dict(d)
    assert got.kind == "search"
    assert got.search_spec == sess.search_spec
    assert got == sess


def test_old_dict_without_kind_is_chat():
    d = session_to_dict(_sample_session())
    d.pop("kind")
    d.pop("search_spec")
    got = session_from_dict(d)
    assert got.kind == "chat" and got.search_spec is None


def test_unknown_kind_is_chat():
    d = session_to_dict(_sample_session())
    d["kind"] = "weird"
    d["search_spec"] = {"scope": "all", "include_archived": False, "ai": False}
    got = session_from_dict(d)
    assert got.kind == "chat" and got.search_spec is None


def test_chat_kind_drops_search_spec():
    d = session_to_dict(_sample_session())
    d["kind"] = "chat"
    d["search_spec"] = {"scope": "all", "include_archived": False, "ai": False}
    assert session_from_dict(d).search_spec is None
    sess = _sample_session()
    sess.search_spec = {"scope": "all", "include_archived": False, "ai": False}
    assert session_to_dict(sess)["search_spec"] is None


def test_normalize_search_spec():
    from llm_bridge.chat_store import normalize_search_spec
    assert normalize_search_spec(None) is None
    assert normalize_search_spec({"scope": "bogus", "include_archived": False, "ai": False}) is None
    assert normalize_search_spec({"scope": "dataset", "include_archived": False, "ai": False}) is None
    assert normalize_search_spec({"scope": "dataset", "dataset": "", "include_archived": False,
                                  "ai": False}) is None
    assert normalize_search_spec({"scope": "all", "include_archived": "yes", "ai": False}) is None
    assert normalize_search_spec({"scope": "all", "include_archived": False, "ai": 1}) is None
    assert normalize_search_spec({"scope": "all", "dataset": "x", "include_archived": False,
                                  "ai": True}) == {"scope": "all", "dataset": None,
                                                   "include_archived": False, "ai": True}
    assert normalize_search_spec({"scope": "unbound", "include_archived": True, "ai": False,
                                  "extra": 1}) == {"scope": "unbound", "dataset": None,
                                                   "include_archived": True, "ai": False}


def test_new_session_kind_search():
    spec = {"scope": "all", "dataset": None, "include_archived": False, "ai": True}
    s = new_session("b", "sys", kind="search", search_spec=spec)
    assert s.kind == "search" and s.search_spec == spec
    assert new_session("b", "sys").kind == "chat"


def test_fork_of_search_session_is_chat():
    spec = {"scope": "all", "dataset": None, "include_archived": False, "ai": True}
    src = new_session("b", "sys", kind="search", search_spec=spec)
    src.messages.append(Message(role="user", content="q"))
    f = fork_session(src, 2, title="t")
    assert f.kind == "chat" and f.search_spec is None


def test_content_fingerprint_distinguishes_kind():
    a = _sample_session()
    b = _sample_session()
    b.kind = "search"
    b.search_spec = {"scope": "all", "dataset": None, "include_archived": False, "ai": False}
    assert content_fingerprint(a) != content_fingerprint(b)
