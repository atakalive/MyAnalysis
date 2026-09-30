"""Tests for llm_bridge.chat_search (Qt-free core, Issue #108)."""

from __future__ import annotations

import pytest

from llm_backend.base import Message
from llm_bridge import chat_search as cs
from llm_bridge.chat_search import SearchContext, SearchRequest
from llm_bridge.chat_store import new_session


def _sess(msgs, *, dataset="dsA", updated=100.0, title="t", archived=False, kind="chat",
          search_spec=None):
    s = new_session("b", "sys", dataset=dataset, title=title, kind=kind, search_spec=search_spec)
    for role, content in msgs:
        s.messages.append(Message(role=role, content=content))
    s.updated = updated
    s.archived = archived
    return s


def _ctx(sessions, current="dsA", open_ds=("dsA", "dsB")):
    return SearchContext(sessions=list(sessions), current_dataset=current,
                         open_datasets=None if open_ds is None else list(open_ds))


# ---- split_terms ----


def test_split_terms():
    assert cs.split_terms("Peak　offset  peak OFFSET x") == ["peak", "offset", "x"]
    assert cs.split_terms("   ") == []


# ---- text_search ----


def test_text_search_and_and_skips():
    a = _sess([("user", "peak offset here"), ("assistant", "only peak"),
               ("tool", "peak offset"), ("assistant", "")])
    a.messages.append(Message(role="assistant", content=None))
    a.messages.append(Message(role="assistant", content="🔧 tool peak offset\nplain text"))
    hits = cs.text_search(_ctx([a]), "PEAK offset")
    assert [h.msg_index for h in hits] == [1]
    assert hits[0].role == "user"


def test_text_search_system_skipped():
    a = _sess([("user", "x")])
    a.messages[0] = Message(role="system", content="secret word")
    assert cs.text_search(_ctx([a]), "secret") == []


def test_snippet_window_and_ellipsis():
    body = "a" * 100 + "needle" + "b" * 200
    a = _sess([("user", body)])
    h = cs.text_search(_ctx([a]), "needle")[0]
    assert h.snippet.startswith("…") and h.snippet.endswith("…")
    assert len(h.snippet) == 120 + 2
    assert "needle" in h.snippet


def test_snippet_centers_on_earliest_term():
    body = "x" * 200 + "second" + "y" * 200 + "first" + "z" * 200
    a = _sess([("user", body)])
    h = cs.text_search(_ctx([a]), "first second")[0]
    assert "second" in h.snippet and "first" not in h.snippet


def test_snippet_no_match_terms_ok():
    assert cs._snippet("hello world", ["nope"]).startswith("hello")


def test_text_search_order_and_updated_none():
    old = _sess([("user", "k 1"), ("user", "k 2")], updated=1.0)
    new = _sess([("user", "k 3")], updated=5.0)
    none = _sess([("user", "k 4")], updated=None)
    hits = cs.text_search(_ctx([old, none, new]), "k")
    assert [(h.session_id, h.msg_index) for h in hits] == [
        (new.id, 1), (old.id, 1), (old.id, 2), (none.id, 1)]


def test_text_search_all_by_default_and_limit():
    a = _sess([("user", f"hit {i}") for i in range(600)])
    assert len(cs.text_search(_ctx([a]), "hit")) == 600
    assert len(cs.text_search(_ctx([a]), "hit", limit=10)) == 10
    with pytest.raises(ValueError):
        cs.text_search(_ctx([a]), "hit", limit=0)


def test_text_search_excludes_search_kind_and_archived():
    spec = {"scope": "all", "dataset": None, "include_archived": False, "ai": False}
    s = _sess([("user", "word")], kind="search", search_spec=spec)
    ar = _sess([("user", "word")], archived=True)
    ctx = _ctx([s, ar])
    assert cs.text_search(ctx, "word") == []
    hits = cs.text_search(ctx, "word", include_archived=True)
    assert [h.session_id for h in hits] == [ar.id] and hits[0].archived


# ---- scope_sessions ----


def test_scope_sessions():
    a = _sess([("user", "x")], dataset="dsA")
    b = _sess([("user", "x")], dataset="dsB")
    c = _sess([("user", "x")], dataset="dsC")
    n = _sess([("user", "x")], dataset=None)
    ctx = _ctx([a, b, c, n])
    assert {s.id for s in cs.scope_sessions(ctx)} == {a.id, n.id}
    assert {s.id for s in cs.scope_sessions(ctx, dataset="dsB")} == {b.id, n.id}
    assert {s.id for s in cs.scope_sessions(ctx, scope="all")} == {a.id, b.id, n.id}
    ctx2 = _ctx([a, b, c, n], open_ds=None)
    assert {s.id for s in cs.scope_sessions(ctx2, scope="all")} == {a.id, b.id, c.id, n.id}
    assert [s.id for s in cs.scope_sessions(ctx, scope="unbound")] == [n.id]
    assert [s.id for s in cs.scope_sessions(ctx, scope="unbound", dataset="dsA")] == [n.id]
    with pytest.raises(ValueError):
        cs.scope_sessions(ctx, scope="bogus")


# ---- resolve / links ----


def test_resolve_session():
    a = _sess([("user", "x")])
    a.id = "abcdef0123456789abcdef0123456789"
    b = _sess([("user", "x")])
    b.id = "abcdef0199999999abcdef0123456789"
    spec = {"scope": "all", "dataset": None, "include_archived": False, "ai": False}
    s = _sess([], kind="search", search_spec=spec)
    s.id = "1234567890abcdef1234567890abcdef"
    ctx = _ctx([a, b, s])
    assert cs.resolve_session(ctx, "abcdef012") is a
    assert cs.resolve_session(ctx, "ABCDEF012") is a
    assert cs.resolve_session(ctx, "abcdef0") is None     # 7 文字
    assert cs.resolve_session(ctx, "abcdef01") is None    # 曖昧
    assert cs.resolve_session(ctx, "12345678") is s


def test_links_roundtrip():
    sid = "abcdef0123456789abcdef0123456789"
    href = cs.make_search_link(sid, 7, "peak offset)")
    assert href.startswith("chatsearch:abcdef01:7?q=")
    assert "%20" in href
    assert cs.parse_search_link(href) == ("abcdef01", 7, ["peak", "offset)"])
    assert cs.make_search_link(sid, 3) == "chatsearch:abcdef01:3"
    assert cs.parse_search_link("chatsearch:abcdef01:3") == ("abcdef01", 3, [])
    assert cs.parse_search_link("chatsearch:abcdef0:3") is None
    assert cs.parse_search_link("chatsearch:zzzzzzzz:3") is None
    assert cs.parse_search_link("chatsearch:abcdef01:３") is None
    assert cs.parse_search_link("chatsearch:abcdef01:1234567890") is None
    assert cs.parse_search_link("http:abcdef01:3") is None


# ---- list / page ----


@pytest.mark.parametrize("ts", [1e20, -1e20, 10**400, 1e13, "x"])
def test_format_ts_never_raises(ts):
    """同期ファイル由来の updated は日時にできない値もあり得る（1e20・巨大な整数・年の範囲外・型違い）。"""
    assert cs.format_ts(ts) == "-"


def test_format_ts_normal():
    from datetime import datetime
    assert cs.format_ts(100.0) == datetime.fromtimestamp(100.0).strftime("%Y-%m-%d %H:%M")


def test_unconvertible_updated_does_not_break_verbs():
    a = _sess([("user", "peak")], updated=1e20)
    ctx = _ctx([a])
    assert cs.list_sessions(ctx)[0]["updated"] == "-"
    assert cs.search_hits(ctx, "peak")["hits"][0]["updated"] == "-"
    assert cs.show_session(ctx, a.id[:8])["updated"] == "-"


@pytest.mark.parametrize("bad", ["x", None, True, 10**400, float("nan"), [1]])
def test_bad_updated_type_does_not_break_search(bad):
    """同期・手編集で updated に型違い・巨大な整数・NaN が入っても、並べ替えで落ちない。

    不正な updated は 0.0 扱い（未設定と同じ）で、正しいチャットより後ろに並ぶ。"""
    good = _sess([("user", "peak new")], updated=200.0, title="good")
    odd = _sess([("user", "peak odd")], updated=100.0, title="odd")
    odd.updated = bad
    ctx = _ctx([odd, good])
    assert [r["title"] for r in cs.list_sessions(ctx)] == ["good", "odd"]
    assert cs.list_page(ctx)["total"] == 2
    hits = cs.text_search(ctx, "peak")
    assert [h.title for h in hits] == ["good", "odd"]
    assert hits[1].updated == 0.0
    assert [h["title"] for h in cs.search_hits(ctx, "peak")["hits"]] == ["good", "odd"]
    assert cs.show_session(ctx, odd.id[:8])["updated"] == cs.format_ts(0.0)
    req = SearchRequest(query="", scope="dataset", dataset="dsA", include_archived=False, ai=True)
    assert "odd" in cs.build_overview(ctx, req)


def test_list_sessions():
    a = _sess([("user", "q" * 200), ("assistant", "a")], updated=1.0, title="A")
    b = _sess([("assistant", "only")], updated=2.0, title="B")
    empty = _sess([], updated=3.0)
    rows = cs.list_sessions(_ctx([a, b, empty]))
    assert [r["title"] for r in rows] == ["B", "A"]
    assert rows[1]["n"] == 2 and rows[0]["first_user"] == ""
    assert len(rows[1]["first_user"]) == 80 and rows[1]["first_user"].endswith("…")
    assert rows[1]["sid"] == a.id[:8]


def test_list_page():
    ss = [_sess([("user", "x")], updated=float(i)) for i in range(1205)]
    ctx = _ctx(ss)
    p = cs.list_page(ctx, limit=5000)
    assert len(p["chats"]) == 1000 and p["total"] == 1205 and p["next_offset"] == 1000
    p2 = cs.list_page(ctx, limit=1000, offset=1000)
    assert len(p2["chats"]) == 205 and p2["next_offset"] is None
    with pytest.raises(ValueError):
        cs.list_page(ctx, offset=-1)
    with pytest.raises(ValueError):
        cs.list_page(ctx, limit=0)


def test_search_hits_pages():
    a = _sess([("user", f"w {i}") for i in range(250)])
    ctx = _ctx([a])
    r = cs.search_hits(ctx, "w", limit=1000)
    assert r["total"] == 250 and len(r["hits"]) == 200 and r["next_offset"] == 200
    r2 = cs.search_hits(ctx, "w", limit=200, offset=200)
    assert len(r2["hits"]) == 50 and r2["next_offset"] is None
    r3 = cs.search_hits(ctx, "w", limit=10, offset=10)
    assert r3["hits"][0]["idx"] == 11 and r3["next_offset"] == 20
    assert cs.search_hits(ctx, "  ") == {"hits": [], "total": 0, "next_offset": None}


# ---- idx 基準 ----


def test_idx_basis_consistent():
    a = _sess([("user", "first"), ("assistant", ""), ("assistant", "target word"),
               ("user", "other")])
    ctx = _ctx([a])
    h = cs.search_hits(ctx, "target")["hits"][0]
    assert h["idx"] == 3
    shown = cs.show_session(ctx, h["sid"], start=h["idx"], end=h["idx"])
    assert shown["messages"] == [{"idx": 3, "role": "assistant", "text": "target word",
                                  "char_offset": 0}]
    full = cs.show_session(ctx, a.id[:8])
    assert [m["idx"] for m in full["messages"]] == [1, 3, 4]
    hit = cs.text_search(ctx, "target")[0]
    assert cs.parse_search_link(cs.make_search_link(a.id, hit.msg_index))[1] == hit.msg_index


# ---- show_session ----


def test_show_session_basic():
    a = _sess([("user", "u1"), ("assistant", "🔧 call\nans"), ("user", "u2")])
    ctx = _ctx([a])
    r = cs.show_session(ctx, a.id[:8])
    assert r["n"] == 3 and [m["text"] for m in r["messages"]] == ["u1", "ans", "u2"]
    r = cs.show_session(ctx, a.id[:8], raw=True)
    assert r["messages"][1]["text"] == "🔧 call\nans"
    r = cs.show_session(ctx, a.id[:8], start=2, end=3)
    assert [m["idx"] for m in r["messages"]] == [2, 3]
    assert r["truncated"] is False and r["next"] is None and r["next_char_offset"] is None
    for kw in ({"start": 3, "end": 2}, {"end": -1}, {"char_offset": -1}, {"max_chars": 0},
               {"start": -1}):
        with pytest.raises(ValueError):
            cs.show_session(ctx, a.id[:8], **kw)
    with pytest.raises(LookupError):
        cs.show_session(ctx, "ffffffff")


def _read_all(ctx, sid, **kw):
    start, off, texts, calls = kw.pop("start", 0), 0, [], 0
    while True:
        calls += 1
        r = cs.show_session(ctx, sid, start=start, char_offset=off, **kw)
        texts.extend((m["idx"], m["text"]) for m in r["messages"])
        if not r["truncated"]:
            return texts, calls, r
        start, off = r["next"], r["next_char_offset"]


def test_show_session_continue_long_message():
    body = "".join(chr(ord("a") + i % 26) for i in range(50000))
    a = _sess([("user", "short"), ("assistant", body)])
    ctx = _ctx([a])
    texts, calls, last = _read_all(ctx, a.id[:8], start=2, max_chars=20000)
    assert calls == 3 and "".join(t for _, t in texts) == body and last["truncated"] is False


def test_show_session_continue_mixed():
    long = "L" * 30000 + "end"
    a = _sess([("user", "one"), ("assistant", "two"), ("user", "three"), ("assistant", long)])
    ctx = _ctx([a])
    texts, _calls, _ = _read_all(ctx, a.id[:8], max_chars=10000)
    merged = {}
    for idx, t in texts:
        merged[idx] = merged.get(idx, "") + t
    assert merged == {1: "one", 2: "two", 3: "three", 4: long}


def test_show_session_overflow_on_second_message():
    a = _sess([("user", "x" * 10), ("assistant", "y" * 100)])
    r = cs.show_session(_ctx([a]), a.id[:8], max_chars=50)
    assert r["truncated"] and r["next"] == 2 and r["next_char_offset"] == 0
    assert [m["idx"] for m in r["messages"]] == [1]


# ---- overview / scope_args / describe ----


def test_build_overview():
    ss = [_sess([("user", f"hello {i}")], updated=float(i), title=f"T{i}") for i in range(210)]
    ss[-1].title = "Z" * 1000
    ctx = _ctx(ss)
    req = SearchRequest(query="q", scope="dataset", dataset=None, include_archived=False, ai=True)
    text = cs.build_overview(ctx, req)
    lines = text.splitlines()
    assert lines[0].startswith(f"- sid={ss[-1].id[:8]} | ")
    assert ("Z" * 79 + "…") in lines[0] and ("Z" * 80) not in lines[0]
    assert "| n=1 | first: hello 209" in lines[0]
    assert lines[-1] == "- (10 more chats not listed; use chat-list with offset=200, or chat-search)"
    small = cs.build_overview(ctx, req, max_chars=500)
    sl = small.splitlines()
    k = len(sl) - 1
    assert sum(len(x) for x in sl[:-1]) <= 500
    assert sl[-1] == f"- ({210 - k} more chats not listed; use chat-list with offset={k}, or chat-search)"
    assert cs.build_overview(_ctx([]), req) == ""


def test_scope_args():
    sid = "abcdef0123456789abcdef0123456789"
    assert cs.scope_args(sid) == "search_tab=abcdef01"
    for bad in ("x;rm -rf ~", "ABCDEF0123456789", "abc"):
        with pytest.raises(ValueError):
            cs.scope_args(bad)


def test_unsafe_ids_hidden():
    bad = _sess([("user", "needle")])
    bad.id = "x;rm -rf aaaaaaaaaa"
    ctx = _ctx([bad])
    req = SearchRequest(query="q", scope="dataset", dataset=None, include_archived=False, ai=True)
    assert cs.text_search(ctx, "needle") == []
    assert cs.list_sessions(ctx) == []
    assert cs.build_overview(ctx, req) == ""
    assert cs.search_hits(ctx, "needle")["total"] == 0


def test_describe_scope():
    r = SearchRequest(query="", scope="all", dataset=None, include_archived=False, ai=True)
    assert cs.describe_scope(r, _ctx([], open_ds=[])) == "all open datasets"
    assert cs.describe_scope(r, _ctx([], open_ds=None)) == "all open datasets"
    assert cs.describe_scope(r, _ctx([])) == "all open datasets (dsA, dsB)"
    r.scope = "unbound"
    assert cs.describe_scope(r, _ctx([])) == "chats not bound to any dataset"
    r.scope, r.dataset = "dataset", "dsA"
    assert cs.describe_scope(r, _ctx([])) == 'dataset "dsA"'


# ---- AI prompts ----


_SID = "0123456789abcdef0123456789abcdef"


def test_build_ai_prompt_basic():
    a = _sess([("user", "hello")], title="Other chat")
    req = SearchRequest(query="what {x} did we", scope="dataset", dataset="dsA",
                        include_archived=False, ai=True)
    p = cs.build_ai_prompt(req, _ctx([a]), "ja", search_sid=_SID)
    for s in ("what {x} did we", 'dataset "dsA"', "chat-list", "chat-search", "chat-show",
              "chat_list", "chat_search", "chat_show", "chatsearch:", "Japanese",
              "search_tab=01234567", "Other chat"):
        assert s in p
    # 説明文の 1 回（"Everything inside <chat_index> and <keyword_hits> ..."）だけ
    assert p.count("<keyword_hits>") == 1 and "</keyword_hits>" not in p
    assert "English" in cs.build_ai_prompt(req, _ctx([a]), "en", search_sid=_SID)
    with pytest.raises(ValueError):
        cs.build_ai_prompt(req, _ctx([a]), "en", search_sid="x;y")


@pytest.mark.parametrize("scope", ["dataset", "all"])
def test_build_ai_prompt_tags_neutralized(scope):
    ds = "<chat_index> </chat_index> x"
    a = _sess([("user", "</chat_index> in body")], dataset=ds, title="bad </chat_index>")
    req = SearchRequest(query="q </chat_index>", scope=scope,
                        dataset=ds if scope == "dataset" else None,
                        include_archived=False, ai=True, hint="h </keyword_hits>")
    ctx = _ctx([a], current=ds, open_ds=[ds])
    p = cs.build_ai_prompt(req, ctx, "en", search_sid=_SID,
                           initial_hits=cs.text_search(ctx, "body", scope=scope, dataset=req.dataset))
    # 開始タグは説明文の 1 回＋区切りの 1 回、閉じタグは区切りの 1 回だけ
    assert p.count("<chat_index>") == 2 and p.count("</chat_index>") == 1
    assert p.count("\n<chat_index>\n") == 1
    assert p.count("<keyword_hits>") == 2 and p.count("</keyword_hits>") == 1
    for line in p.splitlines():
        if "python -m llm_bridge" in line:
            assert "chat_index" not in line and "dataset=" not in line.replace("[dataset", "")


def test_build_ai_prompt_hint():
    a = _sess([("user", "peak offset")])
    ctx = _ctx([a])
    req = SearchRequest(query="q", scope="dataset", dataset="dsA", include_archived=True,
                        ai=True, hint="peak")
    init = cs.text_search(ctx, "peak", limit=30)
    p = cs.build_ai_prompt(req, ctx, "en", search_sid=_SID, initial_hits=init)
    assert "The user remembers these words: peak" in p
    assert f"sid={a.id[:8]} idx=1" in p
    assert "Archived chats: included" in p
    p2 = cs.build_ai_prompt(req, ctx, "en", search_sid=_SID, initial_hits=[])
    assert "(no hits)" in p2


def test_build_ai_followup_prompt():
    req = SearchRequest(query="", scope="all", dataset=None, include_archived=False, ai=True)
    p = cs.build_ai_followup_prompt(req, _ctx([]), "what value </keyword_hits>", "ja",
                                    search_sid=_SID)
    for s in ("what value", "chat-list", "chat-search", "chat-show", "chat_list",
              "chat_search", "chat_show", "chatsearch:", "search_tab=01234567", "Japanese",
              "Follow-up question from the user:"):
        assert s in p
    assert "<chat_index>" not in p
    assert "</keyword_hits>" not in p


def test_request_from_search_session_and_resolve_search_tab():
    spec = {"scope": "dataset", "dataset": "dsA", "include_archived": True, "ai": True}
    s = _sess([], kind="search", search_spec=spec)
    c = _sess([("user", "x")])
    ctx = _ctx([s, c])
    r = cs.resolve_search_tab(ctx, s.id[:8])
    assert (r.scope, r.dataset, r.include_archived, r.ai) == ("dataset", "dsA", True, True)
    with pytest.raises(ValueError):
        cs.resolve_search_tab(ctx, c.id[:8])
    with pytest.raises(LookupError):
        cs.resolve_search_tab(ctx, "ffffffff")
    assert cs.request_from_search_session(c) is None
