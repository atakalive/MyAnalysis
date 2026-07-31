"""Tests for llm_backend.codex — command construction + JSONL event handling.

Spawning is faked (no codex binary, no network): a stub Popen records the argv
and replays canned `codex exec --json` event lines.
"""

from __future__ import annotations

import json

import pytest

from llm_backend import codex as codex_mod
from llm_backend.base import (
    Message, TextDelta, TOOL_CALL_MARKER, TOOL_ERROR_MARKER, TOOL_RESULT_MARKER,
)
from llm_backend.codex import CodexBackend


class _FakeStdin:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, b):
        self.data += b

    def close(self):
        self.closed = True


class _FakePopen:
    """Stands in for subprocess.Popen; replays the events set on the class."""

    events: list[dict] = []
    last: "_FakePopen | None" = None

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.stdin = _FakeStdin()
        self.stdout = iter(
            [(json.dumps(e) + "\n").encode() for e in type(self).events]
        )
        self.stderr = iter([])
        self.returncode = 0
        type(self).last = self

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def fake_popen(monkeypatch):
    monkeypatch.setattr(codex_mod.subprocess, "Popen", _FakePopen)
    _FakePopen.events = []
    _FakePopen.last = None
    return _FakePopen


def _backend(tmp_path, **cfg):
    # cwd → tmp so _agent_home's AGENTS.md lands outside the real home dir.
    return CodexBackend({"bin": str(tmp_path / "codex.exe"), "cwd": str(tmp_path), **cfg})


def _run(backend, prompt="hello"):
    return list(backend.stream([Message(role="user", content=prompt)]))


_TURN = [
    {"type": "thread.started", "thread_id": "th-123"},
    {"type": "item.started",
     "item": {"id": "i1", "type": "command_execution", "command": "echo hi"}},
    {"type": "item.completed",
     "item": {"id": "i1", "type": "command_execution", "command": "echo hi",
              "aggregated_output": "hi\n", "exit_code": 0}},
    {"type": "item.completed",
     "item": {"id": "i2", "type": "agent_message", "text": "done."}},
    {"type": "turn.completed",
     "usage": {"input_tokens": 1000, "cached_input_tokens": 800,
               "output_tokens": 50}},
]


def test_first_turn_cmd_and_events(fake_popen, tmp_path):
    b = _backend(tmp_path)
    events = _run(b)

    cmd = fake_popen.last.cmd
    assert cmd[1] == "exec"
    assert "--json" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "--skip-git-repo-check" in cmd
    assert "--cd" in cmd
    assert cmd[-1] == "-"                      # prompt read from stdin
    assert "resume" not in cmd
    assert fake_popen.last.stdin.data == b"hello"
    assert fake_popen.last.stdin.closed

    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert f"{TOOL_CALL_MARKER} shell  echo hi" in text
    assert f"{TOOL_RESULT_MARKER} hi" in text
    assert "done." in text

    assert b._session_id == "th-123"
    assert b.last_usage == {
        "input": 1000, "output": 50, "context": 1000, "context_window": 0,
    }


def test_second_turn_resumes_thread(fake_popen, tmp_path):
    b = _backend(tmp_path)
    _run(b)
    fake_popen.events = _TURN
    _run(b, "next")

    cmd = fake_popen.last.cmd
    assert cmd[1:3] == ["exec", "resume"]
    assert "th-123" in cmd
    assert cmd[-1] == "-"
    assert "--cd" not in cmd                   # resume inherits the session cwd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd


def test_sandbox_mode_replaces_bypass(fake_popen, tmp_path):
    b = _backend(tmp_path, sandbox_mode="workspace-write")
    _run(b)
    cmd = fake_popen.last.cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"


def test_model_and_effort_flags(fake_popen, tmp_path):
    b = _backend(tmp_path, model="gpt-5.5", effort="high")
    _run(b)
    cmd = fake_popen.last.cmd
    assert cmd[cmd.index("--model") + 1] == "gpt-5.5"
    assert cmd[cmd.index("-c") + 1] == "model_reasoning_effort=high"


def test_failed_command_renders_error_marker(fake_popen, tmp_path):
    fake_popen.events = [
        {"type": "thread.started", "thread_id": "th-1"},
        {"type": "item.completed",
         "item": {"id": "i1", "type": "command_execution", "command": "boom",
                  "aggregated_output": "", "exit_code": 2}},
        {"type": "turn.completed", "usage": {}},
    ]
    text = "".join(e.text for e in _run(_backend(tmp_path)))
    assert f"{TOOL_ERROR_MARKER} exit 2" in text


def test_mcp_tool_call_renders_result_line(fake_popen, tmp_path):
    fake_popen.events = [
        {"type": "thread.started", "thread_id": "th-1"},
        {"type": "item.started",
         "item": {"id": "i1", "type": "mcp_tool_call", "server": "s", "tool": "t"}},
        {"type": "item.completed",
         "item": {"id": "i1", "type": "mcp_tool_call", "server": "s", "tool": "t",
                  "status": "failed", "error": "boom"}},
        {"type": "turn.completed", "usage": {}},
    ]
    text = "".join(e.text for e in _run(_backend(tmp_path)))
    assert f"{TOOL_CALL_MARKER} s.t" in text
    assert f"{TOOL_ERROR_MARKER} boom" in text


def test_turn_failed_raises(fake_popen, tmp_path):
    fake_popen.events = [
        {"type": "thread.started", "thread_id": "th-1"},
        {"type": "turn.failed", "error": {"message": "quota exceeded"}},
    ]
    with pytest.raises(RuntimeError, match="quota exceeded"):
        _run(_backend(tmp_path))


def test_item_type_key_variant_is_accepted(fake_popen, tmp_path):
    # Some codex builds emit item_type instead of type inside the item object.
    fake_popen.events = [
        {"type": "thread.started", "thread_id": "th-1"},
        {"type": "item.completed",
         "item": {"id": "i1", "item_type": "agent_message", "text": "alt"}},
        {"type": "turn.completed", "usage": {}},
    ]
    text = "".join(e.text for e in _run(_backend(tmp_path)))
    assert "alt" in text


def test_agents_md_written_to_cwd(fake_popen, tmp_path):
    _run(_backend(tmp_path))
    content = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert content == codex_mod._SYSTEM_PROMPT_CODEX


@pytest.fixture(autouse=True)
def _default_turn(fake_popen):
    fake_popen.events = list(_TURN)
