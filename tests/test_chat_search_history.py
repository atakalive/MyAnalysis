"""Tests for llm_bridge.chat_search_history (Qt-free, Issue #108)."""

from __future__ import annotations

import json
import time

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


def test_new_entry_ts():
    e = h.new_entry(_req(), datasets=["dsA"], n_hits=0, session_id="s", ts=1_790_000_000.5)
    assert e["ts"] == 1_790_000_000.5 and h.valid_entry(e)
    before = time.time()
    e2 = h.new_entry(_req(), datasets=["dsA"], n_hits=0, session_id="s")
    assert before <= e2["ts"] <= time.time()


# ---- 埋め込み検索・無料検索の entry（Issue #128） ----


def _emb_req(mode, scope="dataset", **kw):
    base = dict(dataset="dsA" if scope == "dataset" else None, mode=mode)
    base.update(kw)
    return _req(scope=scope, **base)


def test_new_entry_embedding_forms():
    e = h.new_entry(_emb_req("semantic"), datasets=["dsA"], n_hits=4, session_id="s")
    assert (e["mode"], e["semantic"], e["exact"], e["scope"]) == ("text", True, False, "dataset")
    assert "registered" not in e
    e = h.new_entry(_emb_req("hybrid", scope="all"), datasets=["dsA", "dsB"], n_hits=4,
                    session_id="s")
    assert (e["mode"], e["semantic"], e["exact"], e["scope"]) == ("text", True, True, "all")
    e = h.new_entry(_emb_req("semantic", scope="registered"), datasets=[], n_hits=None,
                    session_id="s")
    assert (e["scope"], e["registered"], e["datasets"]) == ("all", True, [])
    for e in (h.new_entry(_emb_req("semantic"), datasets=["dsA"], n_hits=1, session_id="s"),
              h.new_entry(_emb_req("hybrid", scope="registered"), datasets=[], n_hits=1,
                          session_id="s")):
        assert h.valid_entry(e)


def test_new_entry_text_and_ai_keys_unchanged():
    keys = {"id", "ts", "mode", "query", "hint", "scope", "datasets", "include_archived",
            "n_hits", "session_id"}
    assert set(h.new_entry(_req(), datasets=["dsA"], n_hits=1, session_id="s")) == keys
    assert set(h.new_entry(_req(ai=True, hint="x"), datasets=["dsA"], n_hits=None,
                           session_id="s")) == keys


def test_entry_mode_and_registered():
    def mk(mode, scope="dataset", **kw):
        return h.new_entry(_emb_req(mode, scope=scope, **kw) if mode in ("semantic", "hybrid")
                           else _req(ai=(mode == "ai")),
                           datasets=[] if scope == "registered" else ["dsA"], n_hits=1,
                           session_id="s")

    assert h.entry_mode(mk("text")) == "text" and h.entry_mode(mk("ai")) == "ai"
    assert h.entry_mode(mk("semantic")) == "semantic"
    assert h.entry_mode(mk("hybrid")) == "hybrid"
    assert h.entry_registered(mk("semantic", scope="registered"))
    assert not h.entry_registered(mk("semantic", scope="all"))
    assert not h.entry_registered(mk("text"))


@pytest.mark.parametrize("extra, mode, registered", [
    ({"semantic": "yes"}, "text", False),
    ({"semantic": 1, "exact": True}, "text", False),
    ({"semantic": True, "exact": "no"}, "semantic", False),
    ({"mode": "ai", "semantic": True}, "ai", False),
    ({"registered": True}, "text", False),
    ({"semantic": True, "registered": True, "scope": "dataset"}, "semantic", False),
    ({"semantic": True, "registered": 1, "scope": "all"}, "semantic", False),
    ({"future_key": {"x": 1}}, "text", False),
])
def test_odd_optional_keys_keep_entry_valid(tmp_path, extra, mode, registered):
    """任意キーの型違い・矛盾で entry を無効にしない（append_history の書き戻しで消えないように）。"""
    e = _entry(**extra)
    assert h.valid_entry(e)
    assert h.entry_mode(e) == mode and h.entry_registered(e) is registered
    assert h.append_history(tmp_path, e)
    assert h.append_history(tmp_path, _entry(query="later"))
    entries, _ = h.load_history(tmp_path)
    assert entries[0] == e


# 旧ビルド（00d1d5f）の valid_entry / load_history / append_history の写し。旧ビルドが
# 新しい entry を含む履歴に追記しても、新しい entry が未知のキーごと残ることを固める。

def _v1_valid_entry(e) -> bool:
    import math
    from datetime import datetime
    if not isinstance(e, dict):
        return False
    if not isinstance(e.get("id"), str) or not e["id"]:
        return False
    ts = e.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return False
    try:
        if not math.isfinite(ts):
            return False
        datetime.fromtimestamp(ts)
    except (OverflowError, OSError, ValueError):
        return False
    if e.get("mode") not in ("text", "ai"):
        return False
    if not isinstance(e.get("query"), str) or not isinstance(e.get("hint"), str):
        return False
    scope = e.get("scope")
    if scope not in ("dataset", "all"):
        return False
    ds = e.get("datasets")
    if not isinstance(ds, list) or not all(isinstance(x, str) for x in ds):
        return False
    if scope == "dataset" and not ds:
        return False
    if not isinstance(e.get("include_archived"), bool):
        return False
    n = e.get("n_hits")
    if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 0):
        return False
    if not isinstance(e.get("session_id"), str) or not e["session_id"]:
        return False
    return True


def _v1_load_history(work_dir):
    try:
        from common.paths import durable_read_json
        status, data = durable_read_json(h.history_path(work_dir))
    except Exception:
        return ([], "unreadable")
    if status == "absent":
        return ([], "absent")
    if status not in ("ok", "recovered"):
        return ([], "unreadable")
    if not isinstance(data, dict) or data.get("version") != 1 \
            or not isinstance(data.get("entries"), list):
        return ([], "unreadable")
    return ([e for e in data["entries"] if _v1_valid_entry(e)], status)


def _v1_append_history(work_dir, entry, *, max_entries=500) -> bool:
    try:
        from common.paths import durable_write_json
        path = h.history_path(work_dir)
        entries, status = _v1_load_history(work_dir)
        if status == "unreadable":
            if h._has_content(path):
                return False
            entries = []
        entries = (entries + [entry])[-max_entries:]
        durable_write_json(path, {"version": 1, "entries": entries})
        return True
    except (OSError, ValueError, TypeError):
        return False


def _v1_new_entry(req, *, datasets, n_hits, session_id, ts=None):
    import uuid
    return {"id": uuid.uuid4().hex, "ts": time.time() if ts is None else ts,
            "mode": "ai" if req.ai else "text", "query": req.query, "hint": req.hint,
            "scope": req.scope, "datasets": list(datasets),
            "include_archived": bool(req.include_archived), "n_hits": n_hits,
            "session_id": session_id}


def test_old_build_keeps_new_entries(tmp_path):
    new = [h.new_entry(_emb_req("semantic", scope="registered"), datasets=[], n_hits=3,
                       session_id="s1"),
           h.new_entry(_emb_req("hybrid"), datasets=["dsA"], n_hits=5, session_id="s2")]
    for e in new:
        assert h.append_history(tmp_path, e)
    old_entry = _v1_new_entry(_req(query="old"), datasets=["dsA"], n_hits=1, session_id="s3")
    assert _v1_append_history(tmp_path, old_entry)          # 旧ビルドが追記
    seen_by_old, _ = _v1_load_history(tmp_path)
    assert [e["session_id"] for e in seen_by_old] == ["s1", "s2", "s3"]
    entries, _ = h.load_history(tmp_path)                    # 新しいビルドで読み戻す
    assert entries[:2] == new
    assert [h.entry_mode(e) for e in entries] == ["semantic", "hybrid", "text"]
    assert h.entry_registered(entries[0])
    # 旧ビルドでは一致検索の行として読める
    assert all(_v1_valid_entry(e) and e["mode"] == "text" for e in new)
