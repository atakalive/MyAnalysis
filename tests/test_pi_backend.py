"""Tests for PiCodingAgentBackend: command assembly, JSONL parsing, env build."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from llm_backend.base import Message, TextDelta
from llm_backend.pi import PiCodingAgentBackend, _SYSTEM_PROMPT_PI


# ---------------------------------------------------------------------------
# Command assembly
# ---------------------------------------------------------------------------


class TestCommandAssembly:
    """Verify that stream() builds the right pi CLI command."""

    def _capture_cmd(
        self, config: dict, monkeypatch, *, session_id=None,
        env: dict[str, str] | None = None,
    ):
        """Set up a backend, monkeypatch Popen, and return the cmd list."""
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(
            "llm_backend.pi.repo_root", lambda: __import__("pathlib").Path("/repo")
        )
        monkeypatch.delenv("PI_API_KEY", raising=False)
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)

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

    def test_cmd_disables_context_files(self, monkeypatch, tmp_path):
        """開発者向け CLAUDE.md / AGENTS.md を読ませない（cwd を指定しても常に）。"""
        for config in ({}, {"cwd": str(tmp_path)}):
            cmd, _ = self._capture_cmd(config, monkeypatch)
            assert "--no-context-files" in cmd

    def test_pi_api_key_is_not_passed_on_the_command_line(self, monkeypatch):
        """PI_API_KEY をプロセス一覧から見える引数に載せない。"""
        cmd, kwargs = self._capture_cmd(
            {}, monkeypatch, env={"PI_API_KEY": "secret"})
        assert "--api-key" not in cmd
        assert "secret" not in cmd
        # 環境変数としては子プロセスに渡っている（キーを消して空振りしていない）。
        assert kwargs["env"]["PI_API_KEY"] == "secret"

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
        # windowless GUI から spawn しても cmd 窓を出さないよう creationflags を配線
        # している（no_window_kwargs 経由）。POSIX CI では両辺 0 となり通る。
        assert captured["kwargs"].get("creationflags") == getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )

    def test_model_and_provider(self, monkeypatch):
        cmd, _ = self._capture_cmd(
            {"model": "gpt-5", "provider": "openai"}, monkeypatch
        )
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "gpt-5"
        assert "--provider" in cmd
        assert cmd[cmd.index("--provider") + 1] == "openai"

    def test_effort_maps_to_thinking_after_provider(self, monkeypatch):
        cmd, _ = self._capture_cmd(
            {"model": "gpt-5", "provider": "openai", "effort": "high"}, monkeypatch
        )
        i = cmd.index("--thinking")
        assert cmd[i + 1] == "high"
        assert i > cmd.index("--provider")

    @pytest.mark.parametrize("config", [{"effort": ""}, {"effort": "  "}, {}])
    def test_no_thinking_flag_without_effort(self, monkeypatch, config):
        cmd, _ = self._capture_cmd(config, monkeypatch)
        assert "--thinking" not in cmd


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
            proc.stdout = iter([line.encode() + b"\n" for line in lines])
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

    def test_thinking_end_yields_thinking_line(self, monkeypatch):
        lines = [
            json.dumps({
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "thinking_end", "contentIndex": 0, "content": "a\nb"},
            }),
            json.dumps({"type": "agent_end"}),
        ]
        backend = self._make_backend_with_lines(lines, monkeypatch)
        events = list(backend.stream([Message(role="user", content="hi")]))
        assert len(events) == 1
        assert isinstance(events[0], TextDelta)
        assert events[0].text == "\n💭 a b\n"

    def test_thinking_end_empty_yields_nothing(self, monkeypatch):
        lines = [
            json.dumps({
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "thinking_end", "contentIndex": 0, "content": ""},
            }),
            json.dumps({"type": "agent_end"}),
        ]
        backend = self._make_backend_with_lines(lines, monkeypatch)
        events = list(backend.stream([Message(role="user", content="hi")]))
        assert events == []

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
        # resume 中（session_id あり）は replay=False なので prompt は最後の user のみ。
        backend._session_id = "sid"
        captured_stdin = {}

        def fake_popen(cmd, **kwargs):
            proc = MagicMock()
            proc.stdin = MagicMock()
            proc.stdout = iter([line.encode() + b"\n" for line in lines])
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
# History replay wiring (Issue #63)
# ---------------------------------------------------------------------------


class TestHistoryReplayWiring:
    """Verify stream() wires build_prompt_with_history with the right replay flag."""

    def _capture_prompt(self, msgs, monkeypatch, *, session_id=None, chat_dataset=None):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/pi")
        monkeypatch.setattr(
            "llm_backend.pi.repo_root", lambda: __import__("pathlib").Path("/repo")
        )
        monkeypatch.delenv("PI_API_KEY", raising=False)
        backend = PiCodingAgentBackend({})
        if session_id:
            backend._session_id = session_id
        if chat_dataset is not None:
            backend.set_chat_dataset(chat_dataset)
        captured = {}

        def fake_popen(cmd, **kwargs):
            proc = MagicMock()
            proc.stdin = MagicMock()
            proc.stdin.write = lambda data: captured.__setitem__("data", data)
            proc.stdout = iter([json.dumps({"type": "agent_end"}).encode() + b"\n"])
            proc.stderr = iter([])
            proc.poll.return_value = 0
            proc.wait.return_value = 0
            proc.returncode = 0
            return proc

        monkeypatch.setattr("subprocess.Popen", fake_popen)
        list(backend.stream(msgs))
        return captured["data"].decode("utf-8")

    def _msgs(self):
        return [
            Message(role="system", content="sys"),
            Message(role="user", content="user1"),
            Message(role="assistant", content="assistant1"),
            Message(role="user", content="user2"),
        ]

    def test_fresh_session_replays_history(self, monkeypatch):
        prompt = self._capture_prompt(self._msgs(), monkeypatch, session_id=None)
        assert "<prior_conversation>" in prompt
        assert "user1" in prompt
        assert prompt.endswith("user2")

    def test_resume_session_no_replay(self, monkeypatch):
        prompt = self._capture_prompt(self._msgs(), monkeypatch, session_id="sid")
        assert "<prior_conversation>" not in prompt
        assert prompt == "user2"

    # ----- チャットの DS（Issue #111） -----

    _CTX_A = '<myanalysis_context>\nchat_dataset: "dsA"\n</myanalysis_context>\n\n'

    def test_fresh_session_prepends_chat_context(self, monkeypatch):
        prompt = self._capture_prompt(self._msgs(), monkeypatch, chat_dataset="dsA")
        assert prompt.startswith(self._CTX_A)
        assert "<prior_conversation>" in prompt
        assert prompt.endswith("user2")

    def test_resume_session_does_not_resend_chat_context(self, monkeypatch):
        prompt = self._capture_prompt(self._msgs(), monkeypatch, session_id="sid",
                                      chat_dataset="dsA")
        assert prompt == "user2"

    def test_no_chat_dataset_prompt_is_unchanged(self, monkeypatch):
        """I1: DS の無いチャットのプロンプトは現行の式と一致する。"""
        from llm_backend.base import build_prompt_with_history
        msgs = self._msgs()
        assert self._capture_prompt(msgs, monkeypatch) == \
            build_prompt_with_history(msgs, replay=True)
        assert self._capture_prompt(msgs, monkeypatch, session_id="sid") == \
            build_prompt_with_history(msgs, replay=False)


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
