"""PC ローカルの resume token ストア (data/llm_state/backend_sessions.json)。

ネイティブセッション (~/.claude, ~/.pi/agent/sessions, ~/.codex) は PC ローカルなので、
それを指す token を同期側の chat_sessions/<id>.json に書くと別 PC で存在しない ID を
--resume に渡してしまう。ここはその退避先。conftest の autouse fixture が
backend_sessions_path を tmp へ向けるので、実ファイルは触らない。
"""
import json

import llm_bridge.paths as lb_paths
from llm_bridge.paths import (
    BACKEND_SESSION_TTL,
    drop_backend_session,
    read_backend_session,
    write_backend_session,
)


def backend_sessions_path():
    """Resolve through the module so conftest's monkeypatch applies.

    `from ... import backend_sessions_path` would bind the ORIGINAL function and
    write the developer's real data/llm_state/ file while the code under test read
    the patched tmp one — silently testing nothing.
    """
    return lb_paths.backend_sessions_path()


def test_roundtrip():
    write_backend_session("s1", "claude-cli", "claude-code", "tok-1")
    rec = read_backend_session("s1")
    assert rec["engine"] == "claude-cli"
    assert rec["backend"] == "claude-code"
    assert rec["token"] == "tok-1"


def test_missing_returns_none():
    assert read_backend_session("nope") is None
    assert read_backend_session("") is None


def test_entries_are_independent():
    write_backend_session("s1", "pi", "pi-coding-agent", "tok-1")
    write_backend_session("s2", "codex", "codex", "tok-2")
    drop_backend_session("s1")
    assert read_backend_session("s1") is None
    assert read_backend_session("s2")["token"] == "tok-2"      # 兄弟は無傷


def test_write_with_empty_token_drops_entry():
    write_backend_session("s1", "pi", "pi-coding-agent", "tok-1")
    write_backend_session("s1", "pi", "pi-coding-agent", None)
    assert read_backend_session("s1") is None


def test_corrupt_file_never_raises_and_reads_empty():
    backend_sessions_path().write_text("{ not json", encoding="utf-8")
    assert read_backend_session("s1") is None          # 例外を投げない


def test_non_dict_file_reads_empty():
    backend_sessions_path().write_text("[1, 2]", encoding="utf-8")
    assert read_backend_session("s1") is None


def test_malformed_entries_are_skipped_not_fatal():
    """手編集/旧形式で壊れたエントリは「resume に使わない」だけで済ませる。"""
    backend_sessions_path().write_text(json.dumps({
        "ok":       {"engine": "pi", "backend": "pi-coding-agent", "token": "t"},
        "no_token": {"engine": "pi", "backend": "pi-coding-agent"},
        "empty":    {"engine": "pi", "backend": "pi-coding-agent", "token": ""},
        "int_tok":  {"engine": "pi", "backend": "pi-coding-agent", "token": 7},
        "not_dict": "whatever",
    }), encoding="utf-8")
    assert read_backend_session("ok")["token"] == "t"
    for bad in ("no_token", "empty", "int_tok", "not_dict"):
        assert read_backend_session(bad) is None


def test_non_str_engine_backend_degrade_to_none():
    backend_sessions_path().write_text(json.dumps({
        "s1": {"engine": 3, "backend": ["x"], "token": "t"},
    }), encoding="utf-8")
    rec = read_backend_session("s1")
    assert rec["token"] == "t"
    assert rec["engine"] is None and rec["backend"] is None


def test_expired_entries_pruned_on_write():
    """TTL 超過は書込のたびに落ちる。セッション ID 突合では孤児を消せない
    (閉じているデータセットのチャットは GUI に載らない) ので年齢で切る。"""
    stale = {"engine": "pi", "backend": "pi-coding-agent", "token": "old",
             "updated": 1.0}                        # epoch 1970 → 確実に期限切れ
    backend_sessions_path().write_text(json.dumps({"old": stale}), encoding="utf-8")
    write_backend_session("new", "pi", "pi-coding-agent", "tok-new")
    assert read_backend_session("old") is None
    assert read_backend_session("new")["token"] == "tok-new"


def test_fresh_entry_survives_a_later_write():
    write_backend_session("s1", "pi", "pi-coding-agent", "tok-1")
    write_backend_session("s2", "pi", "pi-coding-agent", "tok-2")
    assert read_backend_session("s1")["token"] == "tok-1"


def test_ttl_is_generous_enough_to_be_harmless():
    """短すぎる TTL は「同じ PC なのに毎回全履歴再送」になるので下限を固定する。"""
    assert BACKEND_SESSION_TTL >= 30 * 24 * 3600
