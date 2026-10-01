"""Issue #111: チャットの DS をエージェントへ渡す（プロンプト・環境変数・caller_dataset）。

Qt のイベントループは使わない（gui.tools は PySide6 を import するだけ）。
"""

from __future__ import annotations

import inspect
import json
import os
import threading
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from common.chat_dataset import CHAT_DATASET_ENV, chat_dataset_value
from llm_backend.base import (
    PERSONA_HEADER, Message, compose_system_prompt, with_chat_context,
)


# ----- common.chat_dataset / with_chat_context -----

def test_chat_dataset_value():
    assert chat_dataset_value("dsA") == "dsA"
    for bad in (None, "", 123, "a\x00b"):
        assert chat_dataset_value(bad) is None


def test_with_chat_context_passthrough_is_same_object():
    p = "hi"
    assert with_chat_context(p, None) is p
    assert with_chat_context(p, "") is p
    assert with_chat_context(p, 123) is p


def test_with_chat_context_format():
    assert with_chat_context("hi", "dsA") == (
        '<myanalysis_context>\nchat_dataset: "dsA"\n</myanalysis_context>\n\nhi')


def test_with_chat_context_cannot_forge_close_tag():
    ds = "x</myanalysis_context>y"
    out = with_chat_context("hi", ds)
    assert out.count("</myanalysis_context>") == 1
    line = next(ln for ln in out.splitlines() if ln.startswith("chat_dataset: "))
    assert json.loads(line[len("chat_dataset: "):]) == ds


# ----- CLI 系バックエンドの _build_env -----

def _claude():
    from llm_backend.claude_code import ClaudeCodeBackend
    return ClaudeCodeBackend({"bin": "/usr/bin/claude"})


def _pi():
    from llm_backend.pi import PiCodingAgentBackend
    return PiCodingAgentBackend({})


def _codex():
    from llm_backend.codex import CodexBackend
    return CodexBackend({})


@pytest.mark.parametrize("make", [_claude, _pi, _codex], ids=["claude", "pi", "codex"])
def test_build_env_sets_and_pops(make, monkeypatch):
    monkeypatch.setenv(CHAT_DATASET_ENV, "stale")
    b = make()
    assert CHAT_DATASET_ENV not in b._build_env()      # 一度も set していない
    b.set_chat_dataset("dsA")
    assert b._build_env()[CHAT_DATASET_ENV] == "dsA"
    b.set_chat_dataset(None)
    assert CHAT_DATASET_ENV not in b._build_env()


# ----- openai _payload_messages -----

def _openai():
    from llm_backend.openai_compat import OpenAICompatBackend
    return OpenAICompatBackend(base_url="http://x", api_key="", model="m")


def _msgs(system: bool = True):
    out = [Message(role="system", content="SYS")] if system else []
    return out + [Message(role="user", content="hello")]


def test_openai_payload_without_dataset_matches_current_formula():
    """I1: DS なしは現行の式と同じ。"""
    b = _openai()
    msgs = _msgs()
    assert b._payload_messages(msgs) == [m.to_payload() for m in msgs]
    b.set_persona("P")
    assert b._payload_messages(msgs)[0]["content"] == compose_system_prompt("SYS", "P")
    pl = b._payload_messages(_msgs(system=False))
    assert pl[0]["role"] == "system"
    assert pl[0]["content"] == compose_system_prompt("", "P").lstrip("\n")


def test_openai_payload_with_dataset_and_persona_orders_note_first():
    b = _openai()
    b.set_persona("P")
    b.set_chat_dataset("dsA")
    content = b._payload_messages(_msgs())[0]["content"]
    assert content.startswith("SYS")
    assert content.index("## Chat dataset") < content.index(PERSONA_HEADER)


def test_openai_payload_with_dataset_no_persona():
    from llm_backend.openai_compat import _chat_dataset_note
    b = _openai()
    b.set_chat_dataset("dsA")
    msgs = _msgs()
    assert b._payload_messages(msgs)[0]["content"] == (
        "SYS" + "\n\n" + _chat_dataset_note("dsA"))
    pl = b._payload_messages(_msgs(system=False))
    assert pl[0] == {"role": "system", "content": _chat_dataset_note("dsA")}
    # 保存側の Message は変異しない
    assert msgs[0].content == "SYS"


def test_mock_has_inert_set_chat_dataset():
    from llm_backend.mock import MockBackend
    assert MockBackend(model="m").set_chat_dataset("dsA") is None


# ----- commands._execute / submit -----

@pytest.fixture()
def cmd_log(monkeypatch, tmp_path):
    from llm_bridge import commands
    log = tmp_path / "command_log.jsonl"
    monkeypatch.setattr(commands, "command_log_path", lambda: log)
    monkeypatch.setattr(commands, "rotated_command_log_path",
                        lambda: tmp_path / "command_log.1.jsonl")
    return log


def _last_log(log) -> dict:
    return json.loads(log.read_text(encoding="utf-8").splitlines()[-1])


class _Tab:
    def __init__(self, name, ds):
        self.name = name
        self.session_spec = {"dataset": ds}
        self.calls: list = []

    def has_command(self, verb):
        return True

    def dispatch_command(self, verb, **kw):
        self.calls.append((verb, kw))
        return "ok"


class _Win:
    """group model（open_dataset_names / find_tab）と command_accepts を持つ duck window。"""

    def __init__(self, handlers, open_ds=("dsA", "dsB"), tabs=None):
        self._handlers = handlers
        self._open = list(open_ds)
        self._tabs = tabs or {}
        self.received: list = []
        self.find_calls: list = []

    def open_dataset_names(self):
        return list(self._open)

    def find_tab(self, name, dataset=None):
        self.find_calls.append((name, dataset))
        return self._tabs.get((name, dataset))

    def set_active_tab(self, name, dataset=None):
        return True

    def has_command(self, verb):
        return verb in self._handlers

    def dispatch_command(self, verb, **kw):
        self.received.append((verb, kw))
        return self._handlers[verb](**kw)

    def command_accepts(self, verb, arg):
        h = self._handlers.get(verb)
        if h is None:
            return False
        p = inspect.signature(h).parameters.get(arg)
        return p is not None and p.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)


def _handlers():
    return {
        "add-tab": lambda name, dataset=None: "added",
        "chat-search": lambda **kw: "searched",
        "reload": lambda scope="patch", target=None, dataset=None: "reloaded",
    }


def _payload(tier, verb, args, *, target=None, caller=None):
    p = {"id": uuid.uuid4().hex, "ts": "t", "tier": tier, "target": target,
         "verb": verb, "args": args}
    if caller is not None:
        p["caller_dataset"] = caller
    return p


def _run(win, payload):
    from llm_bridge import commands
    commands._execute(win, payload)


def test_execute_defaults_add_tab_to_caller_dataset(cmd_log):
    win = _Win(_handlers())
    _run(win, _payload("window", "add-tab", {"name": "a"}, caller="dsA"))
    assert win.received == [("add-tab", {"name": "a", "dataset": "dsA"})]
    entry = _last_log(cmd_log)
    assert entry["status"] == "ok"
    assert entry["caller_dataset"] == "dsA"
    assert "dataset" not in entry["args"]


def test_execute_explicit_dataset_wins(cmd_log):
    win = _Win(_handlers())
    _run(win, _payload("window", "add-tab", {"name": "a", "dataset": "dsB"}, caller="dsA"))
    assert win.received == [("add-tab", {"name": "a", "dataset": "dsB"})]


def test_execute_does_not_default_kwargs_only_handler(cmd_log):
    win = _Win(_handlers())
    _run(win, _payload("window", "chat-search", {"query": "q"}, caller="dsA"))
    assert win.received == [("chat-search", {"query": "q"})]


def test_execute_old_reload_handler_gets_no_dataset(cmd_log):
    handlers = _handlers()
    handlers["reload"] = lambda scope="patch", target=None: "reloaded"
    win = _Win(handlers)
    _run(win, _payload("window", "reload", {"scope": "tab", "target": "a"}, caller="dsA"))
    assert win.received == [("reload", {"scope": "tab", "target": "a"})]
    assert _last_log(cmd_log)["status"] == "ok"


def test_execute_reload_defaults_only_for_scope_tab(cmd_log):
    win = _Win(_handlers())
    _run(win, _payload("window", "reload", {"scope": "patch"}, caller="dsA"))
    _run(win, _payload("window", "reload", {"scope": "tab", "target": "a"}, caller="dsA"))
    assert win.received == [
        ("reload", {"scope": "patch"}),
        ("reload", {"scope": "tab", "target": "a", "dataset": "dsA"}),
    ]


def test_execute_closed_caller_dataset_errors(cmd_log):
    win = _Win(_handlers())
    _run(win, _payload("window", "add-tab", {"name": "a"}, caller="dsZ"))
    entry = _last_log(cmd_log)
    assert entry["status"] == "error"
    assert "is not open" in entry["error"]
    assert win.received == []


def test_execute_tab_tier_uses_caller_dataset(cmd_log):
    tab = _Tab("t", "dsA")
    win = _Win(_handlers(), tabs={("t", "dsA"): tab})
    _run(win, _payload("tab", "list-panes", {}, target="t", caller="dsA"))
    assert win.find_calls == [("t", "dsA")]
    assert tab.calls == [("list-panes", {})]
    assert _last_log(cmd_log)["status"] == "ok"


def test_execute_tab_tier_missing_in_caller_dataset(cmd_log):
    win = _Win(_handlers())
    _run(win, _payload("tab", "list-panes", {}, target="t", caller="dsA"))
    entry = _last_log(cmd_log)
    assert entry["status"] == "error"
    assert "this chat's dataset" in entry["error"]


def test_execute_without_caller_keeps_args(cmd_log):
    win = _Win(_handlers())
    _run(win, _payload("window", "add-tab", {"name": "a"}))
    assert win.received == [("add-tab", {"name": "a"})]
    assert _last_log(cmd_log)["caller_dataset"] is None
    _run(win, _payload("tab", "list-panes", {}, target="t"))
    assert win.find_calls == [("t", None)]


def test_submit_payload_keys(monkeypatch, tmp_path):
    from llm_bridge import commands
    monkeypatch.setattr(commands, "commands_queue_dir", lambda: tmp_path)
    commands.submit("window", None, "add-tab", {"name": "a"})
    (f,) = list(tmp_path.glob("*.json"))
    payload = json.loads(f.read_text(encoding="utf-8"))
    assert set(payload) == {"id", "ts", "tier", "target", "verb", "args"}   # I1
    f.unlink()
    commands.submit("window", None, "add-tab", {"name": "a"}, caller_dataset="dsA")
    (f,) = list(tmp_path.glob("*.json"))
    payload = json.loads(f.read_text(encoding="utf-8"))
    assert payload["caller_dataset"] == "dsA"
    assert payload["args"] == {"name": "a"}


@pytest.mark.parametrize("bad", ["", 5])
def test_execute_rejects_invalid_explicit_dataset(cmd_log, bad):
    win = _Win(_handlers())
    _run(win, _payload("window", "add-tab", {"name": "a", "dataset": bad}, caller="dsA"))
    entry = _last_log(cmd_log)
    assert entry["status"] == "error"
    assert "non-empty dataset name" in entry["error"]
    assert win.received == []


def test_execute_null_dataset_is_omitted(cmd_log):
    win = _Win(_handlers())
    _run(win, _payload("window", "add-tab", {"name": "a", "dataset": None}, caller="dsA"))
    _run(win, _payload("window", "add-tab", {"name": "a", "dataset": None}))
    assert win.received == [
        ("add-tab", {"name": "a", "dataset": "dsA"}),
        ("add-tab", {"name": "a"}),
    ]


def test_execute_tab_tier_rejects_empty_dataset(cmd_log):
    win = _Win(_handlers())
    _run(win, _payload("tab", "list-panes", {"dataset": ""}, target="t", caller="dsA"))
    assert _last_log(cmd_log)["status"] == "error"
    assert win.find_calls == []


def test_execute_unrelated_verb_args_untouched(cmd_log):
    win = _Win(_handlers())
    _run(win, _payload("window", "chat-search", {"query": "q", "dataset": ""}, caller="dsA"))
    assert win.received == [("chat-search", {"query": "q", "dataset": ""})]


def test_execute_window_without_group_model_is_not_defaulted(cmd_log):
    class _Legacy:
        def __init__(self):
            self.received = []
            self.tab = _Tab("t", None)

        def has_command(self, verb):
            return True

        def dispatch_command(self, verb, **kw):
            self.received.append((verb, kw))
            return "ok"

        def command_accepts(self, verb, arg):
            return True

        def set_active_tab(self, name):
            return True

        def active_tab(self):
            return self.tab

    win = _Legacy()
    _run(win, _payload("window", "add-tab", {"name": "a"}, caller="dsA"))
    assert win.received == [("add-tab", {"name": "a"})]

    class _NoOpen(_Win):
        open_dataset_names = None   # group model 無し

    tab = _Tab("t", None)
    win2 = _NoOpen(_handlers(), tabs={("t", None): tab})
    _run(win2, _payload("window", "add-tab", {"name": "a"}, caller="dsA"))
    assert win2.received == [("add-tab", {"name": "a"})]
    _run(win2, _payload("tab", "list-panes", {}, target="t", caller="dsA"))
    assert win2.find_calls == [("t", None)]


# ----- CLI（llm_bridge.__main__） -----

@pytest.fixture()
def cli(monkeypatch, tmp_path):
    p = tmp_path / "active.json"
    monkeypatch.setattr("llm_bridge.__main__.active_state_path", lambda: p)
    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)
    return p


def test_resolve_dataset_priority(cli, monkeypatch):
    import argparse
    import llm_bridge.__main__ as m
    cli.write_text(json.dumps({"dataset": "dsF", "active_analysis_dataset": "dsG"}),
                   encoding="utf-8")
    ns = argparse.Namespace
    assert m._resolve_dataset(ns(dataset=None)) == "dsF"
    monkeypatch.setenv(CHAT_DATASET_ENV, "dsA")
    assert m._resolve_dataset(ns(dataset=None)) == "dsA"
    assert m._resolve_dataset(ns(dataset="dsB")) == "dsB"
    assert m._resolve_dataset(ns(dataset=None), use_active_analysis=True) == "dsG"


@pytest.mark.parametrize("argv", [
    ["state", "a", "--dataset", ""],
    ["apply-analysis", "--dataset=", "--", "a"],
])
def test_cli_rejects_empty_explicit_dataset(cli, monkeypatch, argv):
    import llm_bridge.__main__ as m
    monkeypatch.setenv(CHAT_DATASET_ENV, "dsA")
    with pytest.raises(SystemExit) as ei:
        m.main(argv)
    assert "non-empty dataset name" in str(ei.value)


def test_cli_state_reads_closed_chat_dataset(cli, monkeypatch, tmp_path, capsys):
    import llm_bridge.__main__ as m
    monkeypatch.setenv(CHAT_DATASET_ENV, "dsA")
    cli.write_text(json.dumps({"active_tab": "a", "dataset": "dsB",
                               "open_datasets": ["dsB"]}), encoding="utf-8")
    (tmp_path / "dsA" / "analyses" / "a").mkdir(parents=True)
    (tmp_path / "dsA" / "analyses" / "a" / "analysis.py").write_text("", encoding="utf-8")
    sd = tmp_path / "dsA" / "_work" / "analyses" / "a" / "state"
    sd.mkdir(parents=True)
    (sd / "current.json").write_text(json.dumps({"from": "dsA"}), encoding="utf-8")
    assert m.main(["state", "a"]) == 0
    assert json.loads(capsys.readouterr().out) == {"from": "dsA"}


def test_cli_active_reports_chat_dataset(cli, monkeypatch, capsys):
    import llm_bridge.__main__ as m
    original = {"active_tab": "a", "dataset": "dsB", "active_dataset": "dsB",
                "open_datasets": ["dsA", "dsB"], "active_analysis_dataset": "dsB"}
    cli.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setenv(CHAT_DATASET_ENV, "dsA")
    assert m.main(["active"]) == 0
    assert json.loads(capsys.readouterr().out) == {**original, "chat_dataset": "dsA"}
    monkeypatch.delenv(CHAT_DATASET_ENV)
    assert m.main(["active"]) == 0
    assert json.loads(capsys.readouterr().out)["chat_dataset"] is None
    cli.unlink()
    monkeypatch.setenv(CHAT_DATASET_ENV, "dsA")
    assert m.main(["active"]) == 0
    assert json.loads(capsys.readouterr().out) == {"active_tab": None, "chat_dataset": "dsA"}


@pytest.mark.parametrize("raw", ["{bad", "[1]"])
def test_cli_active_unreadable(cli, monkeypatch, capsys, raw):
    import llm_bridge.__main__ as m
    cli.write_text(raw, encoding="utf-8")
    monkeypatch.setenv(CHAT_DATASET_ENV, "dsA")
    assert m.main(["active"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "active_tab": None, "error": "active.json is unreadable", "chat_dataset": "dsA"}


@pytest.mark.parametrize("argv", [["window", "list-tabs"], ["tab", "t", "list-panes"]])
def test_cli_window_tab_pass_caller_dataset(cli, monkeypatch, capsys, argv):
    import llm_bridge.__main__ as m
    calls = []

    def fake(*a, **k):
        calls.append(k)
        return "cid"

    monkeypatch.setattr(m.commands, "submit", fake)
    assert m.main(argv) == 0
    monkeypatch.setenv(CHAT_DATASET_ENV, "dsA")
    assert m.main(argv) == 0
    assert calls == [{}, {"caller_dataset": "dsA"}]


# ----- gui.tools -----

@pytest.fixture()
def tools_submit(monkeypatch):
    import gui.tools as tools
    calls = []

    def fake_submit(*a, **k):
        calls.append(k)
        return "cid"

    monkeypatch.setattr(tools.commands, "submit", fake_submit)
    monkeypatch.setattr(tools.commands, "wait_for",
                        lambda *a, **k: {"status": "ok", "error": None, "result": None})
    return calls


def test_tools_dispatch_passes_caller_dataset(tools_submit):
    import gui.tools as tools
    d = tools.make_dispatch(object())
    d("set_active_tab", {"name": "t"}, chat_dataset="dsA")
    d("set_active_tab", {"name": "t"})
    assert tools_submit == [{"caller_dataset": "dsA"}, {}]
    assert tools._CHAT_DATASET.get() is None


def test_tools_dispatch_resets_on_exception(monkeypatch):
    import gui.tools as tools

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(tools, "_dispatch", boom)
    out = json.loads(tools.make_dispatch(None)("x", {}, chat_dataset="dsA"))
    assert "error" in out
    assert tools._CHAT_DATASET.get() is None


def test_tools_dispatch_concurrent_threads_keep_their_dataset(monkeypatch):
    import gui.tools as tools
    barrier = threading.Barrier(2)

    def fake(window, name, args, cancelled=None):
        barrier.wait(timeout=5)
        return tools._CHAT_DATASET.get()

    monkeypatch.setattr(tools, "_dispatch", fake)
    d = tools.make_dispatch(None)
    results: dict = {}

    def run(ds):
        results[ds] = d("x", {}, chat_dataset=ds)

    threads = [threading.Thread(target=run, args=(ds,)) for ds in ("dsA", "dsB")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads)
    assert results == {"dsA": "dsA", "dsB": "dsB"}


def test_tools_get_active_tab_reports_chat_dataset(monkeypatch, tmp_path):
    import gui.tools as tools
    p = tmp_path / "active.json"
    monkeypatch.setattr(tools, "active_state_path", lambda: p)
    p.write_text(json.dumps({"active_tab": "a", "dataset": "dsB"}), encoding="utf-8")
    d = tools.make_dispatch(None)
    assert json.loads(d("get_active_tab", {}, chat_dataset="dsA")) == {
        "active_tab": "a", "dataset": "dsB", "chat_dataset": "dsA"}
    p.write_text("{bad", encoding="utf-8")
    assert json.loads(d("get_active_tab", {}, chat_dataset="dsA")) == {
        "active_tab": None, "error": "active.json is unreadable", "chat_dataset": "dsA"}


def test_tools_get_state_defaults_to_chat_dataset():
    import gui.tools as tools
    calls = []

    class _W:
        def find_tab(self, name, dataset=None):
            calls.append((name, dataset))
            return None

    d = tools.make_dispatch(_W())
    d("get_state", {"name": "t"}, chat_dataset="dsA")
    d("get_state", {"name": "t", "dataset": "dsB"}, chat_dataset="dsA")
    assert calls == [("t", "dsA"), ("t", "dsB")]
