"""Tests for tool-use pipeline: SSE parsing, dispatch, and bridge helpers."""

from __future__ import annotations

import json

import pytest

from llm_backend.base import Message, TextDelta, ToolCallRequest


# ---------------------------------------------------------------------------
# SSE tool_calls parsing (OpenAICompatBackend.stream internals)
# ---------------------------------------------------------------------------


def _simulate_stream(sse_lines: list[str]) -> list[TextDelta | ToolCallRequest]:
    """Reproduce the SSE → event conversion from OpenAICompatBackend.stream.

    Extracts the pure-logic core (no HTTP, no urllib) so we can test
    tool_call delta merging and JSON argument assembly in isolation.
    """
    tool_buffers: dict[int, dict] = {}
    events: list[TextDelta | ToolCallRequest] = []
    for raw_line in sse_lines:
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta", {})
        content = delta.get("content")
        if content:
            events.append(TextDelta(text=content))
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            if idx not in tool_buffers:
                tool_buffers[idx] = {
                    "id": tc.get("id", ""),
                    "name": tc.get("function", {}).get("name", ""),
                    "arguments_str": "",
                }
            buf = tool_buffers[idx]
            if tc.get("id"):
                buf["id"] = tc["id"]
            fn = tc.get("function", {})
            if fn.get("name"):
                buf["name"] = fn["name"]
            buf["arguments_str"] += fn.get("arguments", "")
    for buf in tool_buffers.values():
        raw_args = buf["arguments_str"]
        if not raw_args.strip():
            args = {}
        else:
            try:
                parsed = json.loads(raw_args)
            except (json.JSONDecodeError, ValueError):
                args = {"__parse_error__": f"malformed JSON arguments: {raw_args!r}"}
            else:
                if isinstance(parsed, dict):
                    args = parsed
                else:
                    args = {
                        "__parse_error__": (
                            f"expected JSON object, got "
                            f"{type(parsed).__name__}: {raw_args!r}"
                        )
                    }
        events.append(ToolCallRequest(id=buf["id"], name=buf["name"], arguments=args))
    return events


class TestSSEToolCallParsing:
    def test_split_arguments_merged(self):
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"set_split","arguments":""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"na"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"me\\":\\"x\\"}"}}]}}]}',
            "data: [DONE]",
        ]
        events = _simulate_stream(lines)
        assert len(events) == 1
        tc = events[0]
        assert isinstance(tc, ToolCallRequest)
        assert tc.id == "call_1"
        assert tc.name == "set_split"
        assert tc.arguments == {"name": "x"}

    def test_text_and_tool_calls_mixed(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"hello"}}]}',
            'data: {"choices":[{"delta":{"content":" world"}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c","function":{"name":"get_state","arguments":"{\\"name\\":\\"t\\"}"}}]}}]}',
            "data: [DONE]",
        ]
        events = _simulate_stream(lines)
        texts = [e for e in events if isinstance(e, TextDelta)]
        tools = [e for e in events if isinstance(e, ToolCallRequest)]
        assert len(texts) == 2
        assert texts[0].text == "hello"
        assert texts[1].text == " world"
        assert len(tools) == 1
        assert tools[0].arguments == {"name": "t"}

    def test_empty_arguments_yields_empty_dict(self):
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c","function":{"name":"list_analyses","arguments":""}}]}}]}',
            "data: [DONE]",
        ]
        events = _simulate_stream(lines)
        assert events[0].arguments == {}

    def test_malformed_json_yields_parse_error(self):
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c","function":{"name":"foo","arguments":"{\\"bad"}}]}}]}',
            "data: [DONE]",
        ]
        events = _simulate_stream(lines)
        assert "__parse_error__" in events[0].arguments
        assert "malformed" in events[0].arguments["__parse_error__"]

    def test_non_dict_json_yields_parse_error(self):
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c","function":{"name":"foo","arguments":"[1,2,3]"}}]}}]}',
            "data: [DONE]",
        ]
        events = _simulate_stream(lines)
        assert "__parse_error__" in events[0].arguments
        assert "expected JSON object" in events[0].arguments["__parse_error__"]

    def test_multiple_tool_calls(self):
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"a","function":{"name":"list_analyses","arguments":""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"b","function":{"name":"get_state","arguments":"{\\"name\\":\\"x\\"}"}}]}}]}',
            "data: [DONE]",
        ]
        events = _simulate_stream(lines)
        assert len(events) == 2
        assert events[0].name == "list_analyses"
        assert events[1].name == "get_state"


# ---------------------------------------------------------------------------
# Message.to_payload
# ---------------------------------------------------------------------------


class TestMessagePayload:
    def test_tool_role(self):
        m = Message(role="tool", tool_call_id="x", content="result")
        p = m.to_payload()
        assert p == {"role": "tool", "tool_call_id": "x", "content": "result"}

    def test_assistant_with_tool_calls(self):
        tc = [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "f", "arguments": "{}"},
            }
        ]
        m = Message(role="assistant", content=None, tool_calls=tc)
        p = m.to_payload()
        assert p["role"] == "assistant"
        assert p["tool_calls"] == tc
        assert p["content"] == ""

    def test_user_message(self):
        m = Message(role="user", content="hi")
        assert m.to_payload() == {"role": "user", "content": "hi"}

    def test_invalid_role_raises(self):
        with pytest.raises(ValueError, match="invalid role"):
            Message(role="alien", content="x")


# ---------------------------------------------------------------------------
# gui.tools._dispatch — direct FS read paths
# ---------------------------------------------------------------------------


DS = "ds_test"


class _FakeTab:
    def __init__(self, name, session_spec=None, dataset=None):
        self.name = name
        if session_spec is not None:
            self.session_spec = session_spec
        if dataset is not None:
            self.dataset = dataset


class _FakeWindow:
    """Minimal Qt-free window for gui.tools._dispatch.

    open_dataset_names() drives the multi-dataset list_analyses map (Issue #51);
    absent it, dispatch falls back to [current_dataset]."""
    def __init__(self, current_dataset=None, tabs=(), open_names=None):
        self._current_dataset = current_dataset
        self._tabs = list(tabs)
        self._open_names = open_names

    @property
    def current_dataset(self):
        return self._current_dataset

    def tabs(self):
        return self._tabs

    def open_dataset_names(self):
        if self._open_names is None:
            return [self._current_dataset] if self._current_dataset else []
        return list(self._open_names)


def _make_analyses(tmp_path, dataset, names):
    root = tmp_path / dataset / "analyses"
    root.mkdir(parents=True, exist_ok=True)
    for n in names:
        (root / n).mkdir()
        (root / n / "analysis.py").write_text("# ok")


class TestDispatchDirectReads:
    def test_list_analyses(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path / name)
        _make_analyses(tmp_path, DS, ["alpha"])
        (tmp_path / DS / "analyses" / "beta").mkdir()  # no analysis.py → excluded

        from gui.tools import _dispatch

        win = _FakeWindow(current_dataset=DS, open_names=[DS])
        # New shape: a keyed map {dataset: [names]} across open datasets.
        result = json.loads(_dispatch(win, "list_analyses", {}))
        assert result == {DS: ["alpha"]}

    def test_list_analyses_across_open_datasets(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path / name)
        _make_analyses(tmp_path, "dsA", ["summary", "shared"])
        _make_analyses(tmp_path, "dsB", ["shared"])  # same-named analysis in 2 DS

        from gui.tools import _dispatch

        win = _FakeWindow(current_dataset="dsA", open_names=["dsA", "dsB"])
        result = json.loads(_dispatch(win, "list_analyses", {}))
        assert result == {"dsA": ["shared", "summary"], "dsB": ["shared"]}
        # dataset= restricts to one dataset (same map shape).
        one = json.loads(_dispatch(win, "list_analyses", {"dataset": "dsB"}))
        assert one == {"dsB": ["shared"]}

    def test_get_active_tab_missing(self, tmp_path, monkeypatch):
        p = tmp_path / "active.json"
        monkeypatch.setattr("gui.tools.active_state_path", lambda: p)

        from gui.tools import _dispatch

        result = json.loads(_dispatch(None, "get_active_tab", {}))
        assert result == {"active_tab": None, "chat_dataset": None}

    def test_get_active_tab_exists(self, tmp_path, monkeypatch):
        p = tmp_path / "active.json"
        p.write_text('{"active_tab": "_demo"}')
        monkeypatch.setattr("gui.tools.active_state_path", lambda: p)

        from gui.tools import _dispatch

        result = json.loads(_dispatch(None, "get_active_tab", {}))
        assert result == {"active_tab": "_demo", "chat_dataset": None}

    def test_get_state_read_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path / name)
        state_dir = tmp_path / DS / "_work" / "analyses" / "mytest" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "current.json").write_text('{"unit": "A"}')

        from gui.tools import _dispatch

        tab = _FakeTab("mytest", session_spec={"kind": "analysis", "dataset": DS})
        win = _FakeWindow(current_dataset=DS, tabs=[tab])
        result = json.loads(_dispatch(win, "get_state", {"name": "mytest"}))
        assert result == {"unit": "A"}

    def test_get_state_missing_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path / name)

        from gui.tools import _dispatch

        # no open tab named "nonexist" → {} (read path never resolves work_dir).
        win = _FakeWindow(current_dataset=DS, tabs=[])
        result = json.loads(_dispatch(win, "get_state", {"name": "nonexist"}))
        assert result == {}
        assert not (tmp_path / DS / "_work").exists()

    def test_get_state_traversal_blocked(self, tmp_path, monkeypatch):
        from gui.tools import _dispatch

        win = _FakeWindow(current_dataset=DS, tabs=[])
        result = json.loads(_dispatch(win, "get_state", {"name": "../etc"}))
        assert "error" in result

    def test_get_state_uses_tab_dataset_not_current(self, tmp_path, monkeypatch):
        """active タブ a が dsA・current_dataset=dsB のとき dsA/a の state を返す。"""
        monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path / name)
        sd = tmp_path / "dsA" / "_work" / "analyses" / "a" / "state"
        sd.mkdir(parents=True)
        (sd / "current.json").write_text('{"from": "dsA"}')

        from gui.tools import _dispatch

        tab = _FakeTab("a", session_spec={"kind": "analysis", "dataset": "dsA"})
        win = _FakeWindow(current_dataset="dsB", tabs=[tab])
        result = json.loads(_dispatch(win, "get_state", {"name": "a"}))
        assert result == {"from": "dsA"}

    def test_get_state_figure_viewer_returns_empty(self, tmp_path, monkeypatch):
        """同名 figure viewer が開いているとき get_state は {}（kind!=analysis）。"""
        from gui.tools import _dispatch

        tab = _FakeTab("v", session_spec={"kind": "figure", "dataset": DS})
        win = _FakeWindow(current_dataset=DS, tabs=[tab])
        result = json.loads(_dispatch(win, "get_state", {"name": "v"}))
        assert result == {}

    def test_parse_error_forwarded(self):
        from gui.tools import _dispatch

        result = json.loads(
            _dispatch(None, "get_state", {"__parse_error__": "bad json"})
        )
        assert result == {"error": "bad json"}

    def test_unknown_tool(self):
        from gui.tools import _dispatch

        result = json.loads(_dispatch(None, "no_such_tool", {}))
        assert "error" in result


# ---------------------------------------------------------------------------
# _via_bridge cancel
# ---------------------------------------------------------------------------


class TestViaBridgeCancel:
    def test_cancelled_returns_immediately(self, monkeypatch):
        monkeypatch.setattr("gui.tools.commands.submit", lambda *a, **kw: "fake-id")
        monkeypatch.setattr("gui.tools.commands.wait_for", lambda *a, **kw: None)

        from gui.tools import _via_bridge

        result = json.loads(
            _via_bridge(
                "window", None, "list-tabs", {}, timeout=5.0, cancelled=lambda: True
            )
        )
        assert result["status"] == "cancelled"

    def test_timeout_returns_timeout(self, monkeypatch):
        monkeypatch.setattr("gui.tools.commands.submit", lambda *a, **kw: "fake-id")
        monkeypatch.setattr("gui.tools.commands.wait_for", lambda *a, **kw: None)

        from gui.tools import _via_bridge

        result = json.loads(
            _via_bridge(
                "window", None, "list-tabs", {}, timeout=0.1, cancelled=lambda: False
            )
        )
        assert result["status"] == "timeout"

    def test_success_returns_result(self, monkeypatch):
        monkeypatch.setattr("gui.tools.commands.submit", lambda *a, **kw: "fake-id")
        monkeypatch.setattr(
            "gui.tools.commands.wait_for",
            lambda *a, **kw: {"status": "ok", "error": None, "result": ["_demo"]},
        )

        from gui.tools import _via_bridge

        result = json.loads(
            _via_bridge(
                "window", None, "list-tabs", {}, timeout=5.0, cancelled=lambda: False
            )
        )
        assert result["status"] == "ok"
        assert result["result"] == ["_demo"]


# ---------------------------------------------------------------------------
# Nested panel split tools (Issue #97)
# ---------------------------------------------------------------------------


class TestPaneTools:
    def _record(self, monkeypatch):
        calls = []

        def fake(tier, target, verb, args, timeout=10.0, cancelled=None):
            calls.append((tier, target, verb, dict(args)))
            return json.dumps({"status": "ok"})

        monkeypatch.setattr("gui.tools._via_bridge", fake)
        return calls

    def test_set_split_forwards_slot(self, monkeypatch):
        from gui.tools import _dispatch

        calls = self._record(monkeypatch)
        _dispatch(None, "set_split", {"name": "q", "left": 1, "right": 2, "slot": "top"})
        assert calls == [("tab", "q", "set-split", {"left": 1, "right": 2, "slot": "top"})]

    def test_set_split_without_slot_unchanged(self, monkeypatch):
        from gui.tools import _dispatch

        calls = self._record(monkeypatch)
        _dispatch(None, "set_split", {"name": "q", "left": 1, "right": 2})
        assert calls == [("tab", "q", "set-split", {"left": 1, "right": 2})]

    def test_close_pane_dispatch(self, monkeypatch):
        from gui.tools import _dispatch

        calls = self._record(monkeypatch)
        _dispatch(None, "close_pane", {"name": "q", "slot": "bottom/right", "dataset": "d"})
        assert calls == [
            ("tab", "q", "close-pane", {"slot": "bottom/right", "dataset": "d"})
        ]

    def test_list_panes_dispatch(self, monkeypatch):
        from gui.tools import _dispatch

        calls = self._record(monkeypatch)
        _dispatch(None, "list_panes", {"name": "q"})
        assert calls == [("tab", "q", "list-panes", {})]

    def test_image_verb_forwards_slot(self, monkeypatch):
        from gui.tools import _dispatch

        calls = self._record(monkeypatch)
        _dispatch(None, "set_lut", {"name": "q", "lut": "Fire", "slot": "bottom/right"})
        assert calls == [
            ("tab", "q", "set-lut", {"lut": "Fire", "slot": "bottom/right"})
        ]

    def test_schemas(self):
        from gui.tools import TOOLS

        by = {t["function"]["name"]: t["function"]["parameters"] for t in TOOLS}
        assert "slot" in by["set_split"]["properties"]
        assert by["set_split"]["required"] == ["name", "left", "right"]
        assert by["close_pane"]["required"] == ["name", "slot"]
        assert by["list_panes"]["required"] == ["name"]
        assert "enum" not in by["show"]["properties"]["slot"]
        assert "enum" not in by["show_image"]["properties"]["slot"]
        for t in ("set_lut", "set_range", "set_mode", "set_channel", "set_visible",
                  "set_z", "set_t", "auto_contrast"):
            assert "slot" in by[t]["properties"], t

    def test_chat_search_tools_dispatch(self, monkeypatch):
        from gui.tools import _dispatch

        calls = self._record(monkeypatch)
        _dispatch(None, "chat_list", {"scope": "all", "archived": True, "offset": 5,
                                      "search_tab": "abcd1234", "bogus": 1})
        _dispatch(None, "chat_search", {"query": "peak", "offset": 50, "limit": 10,
                                        "sid": "x"})
        _dispatch(None, "chat_show", {"sid": "abcd1234", "start": 3, "end": 4,
                                      "char_offset": 100, "query": "x"})
        assert calls == [
            ("window", None, "chat-list",
             {"scope": "all", "archived": True, "search_tab": "abcd1234", "offset": 5}),
            ("window", None, "chat-search", {"query": "peak", "limit": 10, "offset": 50}),
            ("window", None, "chat-show",
             {"sid": "abcd1234", "start": 3, "end": 4, "char_offset": 100}),
        ]


# ---------------------------------------------------------------------------
# 関数ツール chat_recall（Issue #127）
# ---------------------------------------------------------------------------


def _hit(sid="0123456789abcdef0123456789abcdef", idx=3, dataset="dsA", snippet="snip"):
    from llm_bridge.chat_index import RecallHit
    return RecallHit(sid=sid, sid8=sid[:8], idx=idx, dataset=dataset, title="T",
                     archived=False, role="assistant", score=0.03, cosine=0.8,
                     lexical=0.0, snippet=snippet, updated=0.0)


class _FakeIndex:
    def __init__(self, hits=(), warnings=()):
        self.hits = list(hits)
        self.warnings = list(warnings)
        self.calls: list = []

    def search(self, query, *, datasets=None, include_archived=False, k=20):
        self.calls.append(("search", query, datasets, include_archived, k))
        return list(self.hits), list(self.warnings)

    def stale_summary(self, datasets=None):
        self.calls.append(("stale", datasets))
        return {"dsA": 2}

    def __getattr__(self, name):          # update / rebuild / clear などは呼ばれてはならない
        raise AssertionError(f"chat_recall must not call ChatIndex.{name}")


class TestChatRecallTool:
    @pytest.fixture()
    def env(self, monkeypatch):
        import config
        from llm_bridge import chat_index
        config.DATASETS.update({"dsA": {}, "dsB": {}})
        idx = _FakeIndex(hits=[_hit()])
        state = {"ready": True, "opened": 0}

        def _open(embedder=None):
            state["opened"] += 1
            return idx

        monkeypatch.setattr(chat_index, "is_ready", lambda: state["ready"])
        monkeypatch.setattr(chat_index.ChatIndex, "open", staticmethod(_open))
        return idx, state

    @staticmethod
    def _call(args, chat_dataset=None):
        from gui.tools import make_dispatch
        return json.loads(make_dispatch(None)("chat_recall", args, chat_dataset=chat_dataset))

    def test_tools_for_turn(self):
        from gui.tools import CHAT_RECALL_TOOL, TOOLS, tools_for_turn
        before = list(TOOLS)
        assert tools_for_turn(False) is TOOLS
        on = tools_for_turn(True)
        assert on[-1] is CHAT_RECALL_TOOL and on[:-1] == TOOLS
        assert TOOLS == before                       # TOOLS 自体は変えない
        fn = CHAT_RECALL_TOOL["function"]
        assert fn["name"] == "chat_recall"
        assert fn["parameters"]["required"] == ["query"]
        assert set(fn["parameters"]["properties"]) == {
            "query", "dataset", "all", "k", "archived", "expand"}

    def test_not_ready_does_not_open(self, env):
        _idx, state = env
        state["ready"] = False
        out = self._call({"query": "peak"})
        assert "off or not installed" in out["error"]
        assert state["opened"] == 0

    @pytest.mark.parametrize(("args", "chat_ds", "want"), [
        ({"query": "q"}, "dsA", ["dsA"]),                 # このチャットの DS
        ({"query": "q"}, None, None),                     # DS の無いチャット → 全登録
        ({"query": "q", "dataset": "dsB"}, "dsA", ["dsB"]),
        ({"query": "q", "all": True}, "dsA", None),
    ])
    def test_scope(self, env, args, chat_ds, want):
        idx, _ = env
        out = self._call(args, chat_dataset=chat_ds)
        assert out["scope"] == {"datasets": want, "include_archived": False}
        assert idx.calls[0] == ("search", "q", want, False, 20)
        assert idx.calls[1] == ("stale", want)

    @pytest.mark.parametrize("args", [
        {"query": "q", "dataset": "dsA", "all": True},
        {"query": "q", "dataset": "nope"},
        {"query": "q", "dataset": ""},
        {"query": "  "},
        {"query": 3},
        {"query": "q", "k": "x"},
        {"query": "q", "k": True},
        {"query": "q", "archived": "yes"},
        {"query": "q", "all": 1},
        {"query": "q", "expand": "no"},
    ])
    def test_bad_arguments(self, env, args):
        idx, _ = env
        out = self._call(args)
        assert isinstance(out.get("error"), str)
        assert idx.calls == []

    def test_unknown_chat_dataset_is_an_error(self, env):
        idx, _ = env
        out = self._call({"query": "q"}, chat_dataset="gone")
        assert "unknown dataset" in out["error"] and idx.calls == []

    @pytest.mark.parametrize(("k", "want"), [(0, 1), (999, 200), (5, 5)])
    def test_k_is_clamped(self, env, k, want):
        idx, _ = env
        self._call({"query": "q", "k": k, "archived": True})
        assert idx.calls[0] == ("search", "q", None, True, want)

    def test_result_shape(self, env):
        out = self._call({"query": " peak offset "}, chat_dataset="dsA")
        assert out["query"] == "peak offset"
        assert out["updated_index"] is False
        assert out["stale"] == {"dsA": 2}
        assert out["warnings"] == []
        assert out["hits"] == [_hit().to_dict()]
        assert out["hits"][0]["sid"] == "01234567"
        assert "text" not in out["hits"][0] and "truncated" not in out

    def test_not_ready_on_open(self, env, monkeypatch):
        from common.embed_model import NotReady
        from llm_bridge import chat_index

        def _open(embedder=None):
            raise NotReady("model ruri-v3-30m: missing")

        monkeypatch.setattr(chat_index.ChatIndex, "open", staticmethod(_open))
        out = self._call({"query": "q"})
        assert out["error"].startswith("chat recall is not ready")

    def test_expand_reads_full_sid_and_truncates(self, env, monkeypatch):
        from llm_bridge import chat_index
        idx, _ = env
        sids = [f"{n:x}" * 8 for n in (10, 11, 12)]
        idx.hits = [_hit(sid=s, idx=i) for i, s in enumerate(sids, 1)]
        reads = []
        bodies = {sids[0]: "a" * 15000, sids[1]: "b" * 8000, sids[2]: "c" * 10}

        def fake_read(dataset, sid, i):
            reads.append((dataset, sid, i))
            return bodies[sid]

        monkeypatch.setattr(chat_index, "read_message", fake_read)
        out = self._call({"query": "q", "expand": True})
        assert reads == [("dsA", s, i) for i, s in enumerate(sids, 1)]   # 完全な id で読む
        h1, h2, h3 = out["hits"]
        assert h1["text"] == "a" * 15000 and "text_truncated" not in h1
        assert h2["text"] == "b" * 5000 and h2["text_truncated"] is True
        assert h3["text"] == "" and h3["text_truncated"] is True
        assert out["truncated"] is True

    def test_expand_unreadable_message_is_null(self, env, monkeypatch):
        from llm_bridge import chat_index
        monkeypatch.setattr(chat_index, "read_message", lambda *a: None)
        out = self._call({"query": "q", "expand": True})
        assert out["hits"][0]["text"] is None and out["truncated"] is False
