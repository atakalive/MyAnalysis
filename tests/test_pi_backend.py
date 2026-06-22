"""Tests for PiCodingAgentBackend: command assembly, JSONL parsing, env build."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from llm_backend.base import Message, TextDelta
from llm_backend.pi import PiCodingAgentBackend, _SYSTEM_PROMPT_PI


# ---------------------------------------------------------------------------
# Command assembly
# ---------------------------------------------------------------------------


class TestCommandAssembly:
    """Verify that stream() builds the right pi CLI command."""

    def _capture_cmd(self, config: dict, monkeypatch, *, session_id=None):
        """Set up a backend, monkeypatch Popen, and return the cmd list."""
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(
            "llm_backend.pi.repo_root", lambda: __import__("pathlib").Path("/repo")
        )
        monkeypatch.delenv("PI_API_KEY", raising=False)

        backend = PiCodingAgentBackend(config)
        if session_id:
            backend._session_id = session_id

        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            proc = MagicMock()
            proc.stdin = MagicMock()
            proc.stdout = iter([
                json.dumps({"type": "agent_end"}).encode() + b"\n"
            ])
            proc.stderr = iter([])
            proc.poll.return_value = 0
            proc.wait.return_value = 0
            proc.returncode = 0
            return proc

        monkeypatch.setattr("subprocess.Popen", fake_popen)

        msgs = [Message(role="user", content="hello")]
        list(backend.stream(msgs))  # consume the generator
        return captured["cmd"], captured["kwargs"]

    def test_append_system_prompt_always_present(self, monkeypatch):
        cmd, _ = self._capture_cmd({}, monkeypatch)
        assert "--append-system-prompt" in cmd
        idx = cmd.index("--append-system-prompt")
        assert cmd[idx + 1] == _SYSTEM_PROMPT_PI

    def test_session_flag_when_set(self, monkeypatch):
        cmd, _ = self._capture_cmd({}, monkeypatch, session_id="abc-123")
        assert "--session" in cmd
        idx = cmd.index("--session")
        assert cmd[idx + 1] == "abc-123"

    def test_no_session_flag_initially(self, monkeypatch):
        cmd, _ = self._capture_cmd({}, monkeypatch)
        assert "--session" not in cmd

    def test_tools_empty_omits_flag(self, monkeypatch):
        cmd, _ = self._capture_cmd({"tools": ""}, monkeypatch)
        assert "--tools" not in cmd
        assert "--no-tools" not in cmd

    def test_tools_allowlist(self, monkeypatch):
        cmd, _ = self._capture_cmd({"tools": "read,write"}, monkeypatch)
        idx = cmd.index("--tools")
        assert cmd[idx + 1] == "read,write"

    def test_tools_none_uses_no_tools(self, monkeypatch):
        cmd, _ = self._capture_cmd({"tools": "none"}, monkeypatch)
        assert "--no-tools" in cmd
        assert "--tools" not in cmd

    def test_cmd_wrapper_on_windows(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr("shutil.which", lambda name: r"C:\npm\pi.cmd")
        monkeypatch.setattr(
            "llm_backend.pi.repo_root", lambda: __import__("pathlib").Path("/repo")
        )
        monkeypatch.delenv("PI_API_KEY", raising=False)

        backend = PiCodingAgentBackend({})
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            proc = MagicMock()
            proc.stdin = MagicMock()
            proc.stdout = iter([
                json.dumps({"type": "agent_end"}).encode() + b"\n"
            ])
            proc.stderr = iter([])
            proc.poll.return_value = 0
            proc.wait.return_value = 0
            proc.returncode = 0
            return proc

        monkeypatch.setattr("subprocess.Popen", fake_popen)

        msgs = [Message(role="user", content="hi")]
        list(backend.stream(msgs))
        assert captured["cmd"][0] == "cmd.exe"
        assert captured["cmd"][1] == "/c"
        assert captured["cmd"][2] == r"C:\npm\pi.cmd"

    def test_no_cmd_wrapper_on_posix(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        cmd, _ = self._capture_cmd({}, monkeypatch)
        assert cmd[0] == "/usr/bin/pi"

    def test_start_new_session_posix(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        _, kwargs = self._capture_cmd({}, monkeypatch)
        assert kwargs["start_new_session"] is True

    def test_no_start_new_session_windows(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr("shutil.which", lambda name: r"C:\npm\pi.cmd")
        monkeypatch.setattr(
            "llm_backend.pi.repo_root", lambda: __import__("pathlib").Path("/repo")
        )
        monkeypatch.delenv("PI_API_KEY", raising=False)

        backend = PiCodingAgentBackend({})
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["kwargs"] = kwargs
            proc = MagicMock()
            proc.stdin = MagicMock()
            proc.stdout = iter([
                json.dumps({"type": "agent_end"}).encode() + b"\n"
            ])
            proc.stderr = iter([])
            proc.poll.return_value = 0
            proc.wait.return_value = 0
            proc.returncode = 0
            return proc

        monkeypatch.setattr("subprocess.Popen", fake_popen)
        list(backend.stream([Message(role="user", content="hi")]))
        assert captured["kwargs"]["start_new_session"] is False

    def test_model_and_provider(self, monkeypatch):
        cmd, _ = self._capture_cmd(
            {"model": "gpt-5", "provider": "openai"}, monkeypatch
        )
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "gpt-5"
        assert "--provider" in cmd
        assert cmd[cmd.index("--provider") + 1] == "openai"


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------


class TestJSONLParsing:
    def _make_backend_with_lines(self, lines: list[str], monkeypatch):
        """Create a backend that reads from given JSONL lines."""
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/pi")
        monkeypatch.setattr(
            "llm_backend.pi.repo_root", lambda: __import__("pathlib").Path("/repo")
        )
        monkeypatch.delenv("PI_API_KEY", raising=False)

        backend = PiCodingAgentBackend({})

        def fake_popen(cmd, **kwargs):
            proc = MagicMock()
            proc.stdin = MagicMock()
            proc.stdout = iter([l.encode() + b"\n" for l in lines])
            proc.stderr = iter([])
            proc.poll.return_value = 0
            proc.wait.return_value = 0
            proc.returncode = 0
            return proc

        monkeypatch.setattr("subprocess.Popen", fake_popen)
        return backend

    def test_text_delta_extracted(self, monkeypatch):
        lines = [
            json.dumps({
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "hello"},
            }),
            json.dumps({"type": "agent_end"}),
        ]
        backend = self._make_backend_with_lines(lines, monkeypatch)
        events = list(backend.stream([Message(role="user", content="hi")]))
        assert len(events) == 1
        assert isinstance(events[0], TextDelta)
        assert events[0].text == "hello"

    def test_session_id_captured(self, monkeypatch):
        lines = [
            json.dumps({"type": "session", "id": "sess-42"}),
            json.dumps({"type": "agent_end"}),
        ]
        backend = self._make_backend_with_lines(lines, monkeypatch)
        list(backend.stream([Message(role="user", content="hi")]))
        assert backend._session_id == "sess-42"

    def test_tool_execution_start_yields_status(self, monkeypatch):
        lines = [
            json.dumps({"type": "tool_execution_start", "toolName": "bash"}),
            json.dumps({"type": "agent_end"}),
        ]
        backend = self._make_backend_with_lines(lines, monkeypatch)
        events = list(backend.stream([Message(role="user", content="hi")]))
        assert len(events) == 1
        assert "[bash...]" in events[0].text

    def test_auto_retry_end_raises_after_cleanup(self, monkeypatch):
        lines = [
            json.dumps({
                "type": "auto_retry_end",
                "finalError": "rate limit exceeded",
            }),
        ]
        backend = self._make_backend_with_lines(lines, monkeypatch)
        with pytest.raises(RuntimeError, match="rate limit exceeded"):
            list(backend.stream([Message(role="user", content="hi")]))
        assert backend._proc is None  # cleanup ran

    def test_non_json_lines_skipped(self, monkeypatch):
        lines = [
            "some debug output",
            json.dumps({
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "ok"},
            }),
            json.dumps({"type": "agent_end"}),
        ]
        backend = self._make_backend_with_lines(lines, monkeypatch)
        events = list(backend.stream([Message(role="user", content="hi")]))
        assert len(events) == 1
        assert events[0].text == "ok"

    def test_no_user_message_raises(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/pi")
        backend = PiCodingAgentBackend({})
        with pytest.raises(RuntimeError, match="no user message"):
            list(backend.stream([Message(role="system", content="sys")]))

    def test_reverse_scan_finds_last_user(self, monkeypatch):
        """Ensure reverse scan skips tool messages at end."""
        lines = [
            json.dumps({"type": "agent_end"}),
        ]
        backend = self._make_backend_with_lines(lines, monkeypatch)
        captured_stdin = {}

        orig_popen = subprocess.Popen.__init__

        def fake_popen(cmd, **kwargs):
            proc = MagicMock()
            proc.stdin = MagicMock()
            proc.stdout = iter([l.encode() + b"\n" for l in lines])
            proc.stderr = iter([])
            proc.poll.return_value = 0
            proc.wait.return_value = 0
            proc.returncode = 0

            def capture_write(data):
                captured_stdin["data"] = data

            proc.stdin.write = capture_write
            return proc

        monkeypatch.setattr("subprocess.Popen", fake_popen)

        msgs = [
            Message(role="user", content="first"),
            Message(role="assistant", content="reply"),
            Message(role="user", content="second"),
            Message(role="tool", tool_call_id="t1", content="result"),
        ]
        list(backend.stream(msgs))
        assert captured_stdin["data"] == b"second"


# ---------------------------------------------------------------------------
# _build_env
# ---------------------------------------------------------------------------


class TestBuildEnv:
    def test_pathsep_join(self, monkeypatch):
        repo = Path("/repo")
        monkeypatch.setattr("llm_backend.pi.repo_root", lambda: repo)
        monkeypatch.setenv("PYTHONPATH", "/existing")
        monkeypatch.setenv("PATH", "/usr/bin")

        backend = PiCodingAgentBackend({})
        env = backend._build_env()

        assert env["PYTHONPATH"].startswith(str(repo))
        parts = env["PYTHONPATH"].split(os.pathsep)
        assert parts[0] == str(repo)
        assert "/existing" in parts

        assert env["PYTHONUTF8"] == "1"

        py_dir = os.path.dirname(sys.executable)
        path_parts = env["PATH"].split(os.pathsep)
        assert path_parts[0] == py_dir

    def test_empty_pythonpath(self, monkeypatch):
        repo = Path("/repo")
        monkeypatch.setattr("llm_backend.pi.repo_root", lambda: repo)
        monkeypatch.delenv("PYTHONPATH", raising=False)

        backend = PiCodingAgentBackend({})
        env = backend._build_env()
        assert env["PYTHONPATH"] == str(repo)
