"""Adapter wiring tests for ClaudeCodeBackend history replay (Issue #63).

Fakes subprocess.Popen and captures the JSON written to stdin (the `text`
field carries the prompt) to prove stream() wires build_prompt_with_history
with replay=(session_id is None).
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest

from llm_backend.base import Message
from llm_backend.claude_code import (
    _DEFAULT_PERMISSION_MODE,
    ClaudeCodeBackend,
    _resolve_permission_mode,
    permission_mode_is_invalid,
)


def _capture_prompt(msgs, monkeypatch, tmp_path, *, session_id=None, chat_dataset=None):
    # cwd をテスト用 tmp_path に固定し、_agent_home() が実ユーザー home 配下へ
    # ディレクトリを mkdir する副作用を避ける（hermetic 化）。
    backend = ClaudeCodeBackend({"bin": "/usr/bin/claude", "cwd": str(tmp_path)})
    if session_id:
        backend._session_id = session_id
    if chat_dataset is not None:
        backend.set_chat_dataset(chat_dataset)
    captured = {}

    def fake_popen(cmd, **kwargs):
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.closed = False
        proc.stdin.write = lambda data: captured.__setitem__("data", data)
        # a single successful result event ends the stream loop
        proc.stdout = iter([
            json.dumps({
                "type": "result", "subtype": "success", "session_id": "s1",
            }).encode() + b"\n"
        ])
        proc.stderr = iter([])
        proc.poll.return_value = 0
        proc.wait.return_value = 0
        proc.returncode = 0
        return proc

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    list(backend.stream(msgs))
    payload = json.loads(captured["data"].decode("utf-8"))
    return payload["message"]["content"][0]["text"]


def _msgs():
    return [
        Message(role="system", content="sys"),
        Message(role="user", content="user1"),
        Message(role="assistant", content="assistant1"),
        Message(role="user", content="user2"),
    ]


def test_fresh_session_replays_history(monkeypatch, tmp_path):
    prompt = _capture_prompt(_msgs(), monkeypatch, tmp_path, session_id=None)
    assert "<prior_conversation>" in prompt
    assert "user1" in prompt
    assert prompt.endswith("user2")


def test_resume_session_no_replay(monkeypatch, tmp_path):
    prompt = _capture_prompt(_msgs(), monkeypatch, tmp_path, session_id="sid")
    assert "<prior_conversation>" not in prompt
    assert prompt == "user2"


# ----- チャットの DS（Issue #111） -----

_CTX_A = '<myanalysis_context>\nchat_dataset: "dsA"\n</myanalysis_context>\n\n'


def test_fresh_session_prepends_chat_context(monkeypatch, tmp_path):
    prompt = _capture_prompt(_msgs(), monkeypatch, tmp_path, chat_dataset="dsA")
    assert prompt.startswith(_CTX_A)
    assert "<prior_conversation>" in prompt
    assert prompt.endswith("user2")


def test_resume_session_does_not_resend_chat_context(monkeypatch, tmp_path):
    prompt = _capture_prompt(_msgs(), monkeypatch, tmp_path, session_id="sid",
                             chat_dataset="dsA")
    assert prompt == "user2"


def test_no_chat_dataset_prompt_is_unchanged(monkeypatch, tmp_path):
    """I1: DS の無いチャットのプロンプトは現行の式と一致する。"""
    from llm_backend.base import build_prompt_with_history
    msgs = _msgs()
    assert _capture_prompt(msgs, monkeypatch, tmp_path) == \
        build_prompt_with_history(msgs, replay=True)
    assert _capture_prompt(msgs, monkeypatch, tmp_path, session_id="sid") == \
        build_prompt_with_history(msgs, replay=False)


# ----- _resolve_bin (Issue #94) -----

def test_resolve_bin_bare_name_uses_which(monkeypatch):
    monkeypatch.setattr(
        "llm_backend.claude_code.shutil.which",
        lambda v: "/usr/local/bin/claude" if v == "claude" else None,
    )
    assert ClaudeCodeBackend._resolve_bin("claude") == "/usr/local/bin/claude"


def test_resolve_bin_absolute_is_literal(monkeypatch):
    monkeypatch.setattr("llm_backend.claude_code.shutil.which", lambda v: "/wrong")
    assert ClaudeCodeBackend._resolve_bin("/abs/path/claude") == "/abs/path/claude"


def test_resolve_bin_path_separator_is_literal(monkeypatch):
    monkeypatch.setattr("llm_backend.claude_code.shutil.which", lambda v: "/wrong")
    assert ClaudeCodeBackend._resolve_bin("dir/claude") == "dir/claude"
    assert ClaudeCodeBackend._resolve_bin("dir\\claude") == "dir\\claude"


def test_resolve_bin_which_miss_falls_back_to_literal(monkeypatch):
    monkeypatch.setattr("llm_backend.claude_code.shutil.which", lambda v: None)
    assert ClaudeCodeBackend._resolve_bin("claude") == "claude"


def test_resolve_bin_existing_file_is_literal(monkeypatch, tmp_path):
    f = tmp_path / "claude"
    f.write_text("x")
    monkeypatch.setattr("llm_backend.claude_code.shutil.which", lambda v: "/wrong")
    assert ClaudeCodeBackend._resolve_bin(str(f)) == str(f)


# ----- permission_mode（Issue #103 G-7） -----


@pytest.mark.parametrize("raw,invalid", [
    (None, False), ("plan", False), (" plan ", False),
    ("", True), ("   ", True), (False, True), (0, True),
])
def test_permission_mode_is_invalid(raw, invalid):
    assert permission_mode_is_invalid(raw) is invalid


def _perm_warnings(caplog):
    return [
        r for r in caplog.records
        if r.name == "llm_backend.claude_code" and r.levelno == logging.WARNING
    ]


@pytest.mark.parametrize("config,expected", [
    ({}, _DEFAULT_PERMISSION_MODE),
    ({"permission_mode": "plan"}, "plan"),
    ({"permission_mode": " plan "}, "plan"),
])
def test_resolve_permission_mode_valid(caplog, config, expected):
    with caplog.at_level(logging.WARNING, logger="llm_backend.claude_code"):
        assert _resolve_permission_mode(config) == expected
    assert _perm_warnings(caplog) == []


@pytest.mark.parametrize("raw", ["", "   ", False, 0])
def test_resolve_permission_mode_invalid_warns(caplog, raw):
    with caplog.at_level(logging.WARNING, logger="llm_backend.claude_code"):
        assert _resolve_permission_mode({"permission_mode": raw}) == _DEFAULT_PERMISSION_MODE
    assert len(_perm_warnings(caplog)) == 1


def test_empty_permission_mode_passes_default_to_cli(monkeypatch, tmp_path):
    backend = ClaudeCodeBackend(
        {"bin": "/usr/bin/claude", "cwd": str(tmp_path), "permission_mode": ""}
    )
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.closed = False
        proc.stdout = iter([
            json.dumps({
                "type": "result", "subtype": "success", "session_id": "s1",
            }).encode() + b"\n"
        ])
        proc.stderr = iter([])
        proc.poll.return_value = 0
        proc.wait.return_value = 0
        proc.returncode = 0
        return proc

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    list(backend.stream(_msgs()))
    cmd = captured["cmd"]
    i = cmd.index("--permission-mode")
    assert cmd[i + 1] == "bypassPermissions"


# ---- Issue #109: 思考サマリ（💭）の表示 ----

from llm_backend.base import TextDelta, format_thinking_line  # noqa: E402


def SE(ev, parent=None):
    return {"type": "stream_event", "event": ev, "parent_tool_use_id": parent}


MS = SE({"type": "message_start", "message": {}})
RESULT = {"type": "result", "subtype": "success", "session_id": "s1"}
TU = {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}


def B(i, thinking=""):
    return SE({"type": "content_block_start", "index": i,
               "content_block": {"type": "thinking", "thinking": thinking}})


def D(i, t, parent=None):
    return SE({"type": "content_block_delta", "index": i,
               "delta": {"type": "thinking_delta", "thinking": t}}, parent)


def S(i):
    return SE({"type": "content_block_stop", "index": i})


def TD(t, i=0):
    return SE({"type": "content_block_delta", "index": i,
               "delta": {"type": "text_delta", "text": t}})


def TH(t, **extra):
    return {"type": "thinking", "thinking": t, **extra}


def A(*content, parent=None):
    ev = {"type": "assistant", "message": {"content": list(content)}}
    if parent is not None:
        ev["parent_tool_use_id"] = parent
    return ev


def _run_events(events, monkeypatch, tmp_path, *, positions=None):
    """events（末尾に result を足す）を stdout に流し、TextDelta の (行番号, text) を返す。"""
    backend = ClaudeCodeBackend({"bin": "/usr/bin/claude", "cwd": str(tmp_path)})
    lines = [json.dumps(e).encode() + b"\n" for e in [*events, RESULT]]
    state = {"pos": -1}

    def gen():
        for k, ln in enumerate(lines):
            state["pos"] = k
            yield ln

    def fake_popen(cmd, **kwargs):
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.closed = False
        proc.stdout = gen()
        proc.stderr = iter([])
        proc.poll.return_value = 0
        proc.wait.return_value = 0
        proc.returncode = 0
        return proc

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    out = []
    for e in backend.stream(_msgs()):
        if isinstance(e, TextDelta):
            out.append((state["pos"], e.text))
    return out


def _joined(events, monkeypatch, tmp_path):
    return "".join(t for _, t in _run_events(events, monkeypatch, tmp_path))


def _abc_events():
    return [
        MS,
        B(0),
        SE({"type": "content_block_delta", "index": 0,
            "delta": {"type": "signature_delta", "signature": "s"}}),
        S(0),
        A(TH("", signature="s")),
        B(1),
        D(1, "データ構造を確認した。\n"),
        D(1, "続いて中核レポートを読む。"),
        S(1),
        A(TH("データ構造を確認した。\n続いて中核レポートを読む。")),
        A(TU),
        SE({"type": "message_stop"}),
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "a.csv"}]}},
        MS,
        SE({"type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""}}),
        TD("結論です。"),
        S(0),
    ]


def test_thinking_stream_order_one_line_no_dup(monkeypatch, tmp_path):
    # (a)(b)(c)
    assert _joined(_abc_events(), monkeypatch, tmp_path) == (
        "\n💭 データ構造を確認した。 続いて中核レポートを読む。\n"
        "\n🔧 Bash  ls\n   ↳ a.csv\n結論です。")


def test_thinking_from_assistant_without_delta(monkeypatch, tmp_path):
    # (d)
    events = [A(TH("計画\nを立てる"), TU)]
    assert _joined(events, monkeypatch, tmp_path) == "\n💭 計画 を立てる\n\n🔧 Bash  ls\n"


def test_thinking_initial_value_from_block_start(monkeypatch, tmp_path):
    # (e)
    events = [MS, B(0, "初期値"), D(0, "の続き"), S(0)]
    assert _joined(events, monkeypatch, tmp_path) == "\n💭 初期値の続き\n"


def test_thinking_subagent_hidden(monkeypatch, tmp_path):
    # (f)(g)
    events = [
        A(TH("サブ"), parent="toolu_1"),
        SE({"type": "message_start", "message": {}}, "toolu_1"),
        D(0, "サブ思考", "toolu_1"),
    ]
    assert _joined(events, monkeypatch, tmp_path) == ""


def test_thinking_closed_before_text(monkeypatch, tmp_path):
    # (h)
    events = [MS, D(0, "途中"), TD("本文")]
    assert _joined(events, monkeypatch, tmp_path) == "\n💭 途中\n本文"


def test_thinking_partial_only_some_blocks(monkeypatch, tmp_path):
    # (i1)
    events = [MS, B(0), D(0, "A"), S(0), A(TH("A")), B(1), S(1), A(TH("B"))]
    assert _joined(events, monkeypatch, tmp_path) == "\n💭 A\n\n💭 B\n"


def test_thinking_partial_assistant_bundled(monkeypatch, tmp_path):
    # (i2)
    events = [MS, B(0), D(0, "A"), S(0), B(1), S(1), A(TH("A"), TH("B"))]
    assert _joined(events, monkeypatch, tmp_path) == "\n💭 A\n\n💭 B\n"


def test_thinking_assistant_after_next_message_start(monkeypatch, tmp_path):
    # (i3)
    events = [MS, B(0), D(0, "A"), S(0), MS, A(TH("A"))]
    assert _joined(events, monkeypatch, tmp_path) == "\n💭 A\n"


def test_thinking_empty_signature_block_first(monkeypatch, tmp_path):
    # (i4)
    events = [
        MS,
        SE({"type": "content_block_start", "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""}}),
        S(0),
        A(TH("", signature="x")),
        B(1), D(1, "本文"), S(1),
        A(TH("本文")),
    ]
    assert _joined(events, monkeypatch, tmp_path) == "\n💭 本文\n"


def test_thinking_whitespace_across_pieces(monkeypatch, tmp_path):
    # (j)
    pieces = ["a ", " b\t c", " \n ", "　d "]
    events = [MS, B(0), *[D(0, p) for p in pieces], S(0)]
    got = _joined(events, monkeypatch, tmp_path)
    assert got == "\n💭 a b c d\n"
    assert got == format_thinking_line("".join(pieces))


def test_thinking_subagent_interrupt_silent(monkeypatch, tmp_path):
    # (k1)
    events = [
        MS, B(0), D(0, "親の"),
        A(TH("サブ"), {"type": "text", "text": "サブ本文"}, parent="toolu_1"),
        D(0, "思考"), S(0),
    ]
    assert _joined(events, monkeypatch, tmp_path) == "\n💭 親の思考\n"


def test_thinking_subagent_interrupt_with_tool(monkeypatch, tmp_path):
    # (k2)
    events = [
        MS, B(0), D(0, "親の"),
        A(TU, parent="toolu_1"),
        D(0, "思考"), S(0),
        A(TH("親の思考")),
    ]
    assert _joined(events, monkeypatch, tmp_path) == (
        "\n💭 親の\n\n🔧 Bash  ls\n\n💭 思考\n")


def _l1_events():
    return [MS, B(0), D(0, "A"), S(0), B(1), S(1), B(2), D(2, "C"), S(2),
            A(TH("A"), TH("B"), TH("C"))]


def test_thinking_chronology_l1(monkeypatch, tmp_path):
    assert _joined(_l1_events(), monkeypatch, tmp_path) == "\n💭 A\n\n💭 B\n\n💭 C\n"


def test_thinking_chronology_l2(monkeypatch, tmp_path):
    events = [MS, B(0), D(0, "A"), S(0), B(1), S(1), B(2), D(2, "C"), S(2),
              A(TH("A"), TH("B"), TH("C"), TU)]
    assert _joined(events, monkeypatch, tmp_path) == (
        "\n💭 A\n\n💭 B\n\n💭 C\n\n🔧 Bash  ls\n")


def test_thinking_chronology_l3_per_block_assistant(monkeypatch, tmp_path):
    events = [MS, B(0), D(0, "A"), S(0), A(TH("A")), B(1), S(1), A(TH("B")),
              B(2), D(2, "C"), S(2), A(TH("C")), A(TU)]
    assert _joined(events, monkeypatch, tmp_path) == (
        "\n💭 A\n\n💭 B\n\n💭 C\n\n🔧 Bash  ls\n")


def test_thinking_safety_valve_message_start(monkeypatch, tmp_path):
    # (l4)
    events = [MS, B(0), S(0), B(1), D(1, "X"), S(1), A(TH("X")), A(TU), MS, TD("次")]
    assert _joined(events, monkeypatch, tmp_path) == "\n💭 X\n\n🔧 Bash  ls\n次"


def test_thinking_safety_valve_turn_end(monkeypatch, tmp_path):
    # (l5)
    events = [MS, B(0), S(0), TD("本文")]
    assert _joined(events, monkeypatch, tmp_path) == "本文"


def test_thinking_empty_block_resolved_by_assistant(monkeypatch, tmp_path):
    # (l6)
    events = [MS, B(0), S(0), A(TH("")), B(1), D(1, "Y"), S(1), A(TH("Y")), A(TU)]
    assert _joined(events, monkeypatch, tmp_path) == "\n💭 Y\n\n🔧 Bash  ls\n"


def test_thinking_two_pending_frames(monkeypatch, tmp_path):
    # (l7)
    events = [MS, B(0), S(0), B(1), S(1), A(TH("P")), A(TH("Q"))]
    assert _joined(events, monkeypatch, tmp_path) == "\n💭 P\n\n💭 Q\n"


def test_thinking_subagent_output_between_frames(monkeypatch, tmp_path):
    # (l8)
    events = [
        MS, B(0), S(0),
        A({"type": "tool_use", "name": "Read", "input": {"file_path": "a"}},
          parent="toolu_1"),
        A(TH("B")),
    ]
    assert _joined(events, monkeypatch, tmp_path) == "\n💭 B\n\n🔧 Read  a\n"


def test_thinking_liveness(monkeypatch, tmp_path):
    # (m)
    assert _run_events(_abc_events(), monkeypatch, tmp_path) == [
        (6, "\n💭 データ構造を確認した。"),
        (7, " 続いて中核レポートを読む。"),
        (8, "\n"),
        (10, "\n🔧 Bash  ls\n"),
        (12, "   ↳ a.csv\n"),
        (15, "結論です。"),
    ]
    out = _run_events(_l1_events(), monkeypatch, tmp_path)
    assert (9, "\n💭 C") in out


# --------------------------------------------------------------------------- #
# CLAUDE_CODE_EFFORT_LEVEL（Issue #117）                                         #
# --------------------------------------------------------------------------- #

def test_build_env_drops_effort_env_when_effort_set(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "low")
    assert "CLAUDE_CODE_EFFORT_LEVEL" not in ClaudeCodeBackend({"effort": "high"})._build_env()


@pytest.mark.parametrize("config", [{}, {"effort": "  "}])
def test_build_env_keeps_effort_env_without_effort(monkeypatch, config):
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "low")
    assert ClaudeCodeBackend(config)._build_env()["CLAUDE_CODE_EFFORT_LEVEL"] == "low"
