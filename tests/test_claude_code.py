"""Adapter wiring tests for ClaudeCodeBackend history replay (Issue #63).

Fakes subprocess.Popen and captures the JSON written to stdin (the `text`
field carries the prompt) to prove stream() wires build_prompt_with_history
with replay=(session_id is None).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from llm_backend.base import Message
from llm_backend.claude_code import ClaudeCodeBackend


def _capture_prompt(msgs, monkeypatch, tmp_path, *, session_id=None):
    # cwd をテスト用 tmp_path に固定し、_agent_home() が実ユーザー home 配下へ
    # ディレクトリを mkdir する副作用を避ける（hermetic 化）。
    backend = ClaudeCodeBackend({"bin": "/usr/bin/claude", "cwd": str(tmp_path)})
    if session_id:
        backend._session_id = session_id
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
