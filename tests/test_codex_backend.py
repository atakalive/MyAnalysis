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


def test_first_turn_prepends_chat_context(fake_popen, tmp_path):
    """チャットの DS は新規スレッドの最初の送信だけに前置する（Issue #111）。"""
    fake_popen.events = _TURN
    b = _backend(tmp_path)
    b.set_chat_dataset("dsA")
    _run(b)
    assert fake_popen.last.stdin.data.startswith(
        b'<myanalysis_context>\nchat_dataset: "dsA"\n</myanalysis_context>\n\n')
    assert b._session_id == "th-123"
    _run(b, "next")
    assert b"<myanalysis_context>" not in fake_popen.last.stdin.data


def test_first_turn_without_chat_dataset_is_unchanged(fake_popen, tmp_path):
    """I1: DS の無いチャットの初回ターンは従来と同じ stdin。"""
    fake_popen.events = _TURN
    b = _backend(tmp_path)
    b.set_chat_dataset(None)
    _run(b)
    assert fake_popen.last.stdin.data == b"hello"


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


def test_reasoning_renders_full_thinking_line(fake_popen, tmp_path):
    fake_popen.events = [
        {"type": "thread.started", "thread_id": "th-1"},
        {"type": "item.completed",
         "item": {"id": "r1", "type": "reasoning", "text": "x" * 300}},
        {"type": "turn.completed",
         "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    events = _run(_backend(tmp_path))
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "\n💭 " + "x" * 300 + "\n" in text
    assert "…" not in text


# ---------------------------------------------------------------------------
# supported_efforts / codex_home（Issue #117）
# ---------------------------------------------------------------------------

def _write_catalog(home, models):
    home.mkdir(parents=True, exist_ok=True)
    (home / "models_cache.json").write_text(json.dumps({"models": models}), encoding="utf-8")


def _levels(*names):
    return [{"effort": n, "description": "d"} for n in names]


@pytest.fixture
def codex_home_dir(tmp_path, monkeypatch):
    home = tmp_path / "ch"
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def test_supported_efforts_filters_by_model(codex_home_dir):
    _write_catalog(codex_home_dir, [
        {"slug": "other", "supported_reasoning_levels": _levels("low")},
        {"slug": "gpt-x", "supported_reasoning_levels":
            _levels("none", "high", "low", "ultra", "high")},
    ])
    assert codex_mod.supported_efforts("gpt-x") == ("high", "low", "ultra")
    assert codex_mod.supported_efforts(" gpt-x ") == ("high", "low", "ultra")


def test_supported_efforts_empty_model_uses_config_default(codex_home_dir):
    _write_catalog(codex_home_dir, [
        {"slug": "gpt-x", "supported_reasoning_levels": _levels("low", "max")},
    ])
    (codex_home_dir / "config.toml").write_text('model = "gpt-x"\n', encoding="utf-8")
    assert codex_mod.supported_efforts("") == ("low", "max")


def test_supported_efforts_empty_model_without_default_is_none(codex_home_dir):
    _write_catalog(codex_home_dir, [
        {"slug": "gpt-x", "supported_reasoning_levels": _levels("low")},
    ])
    assert codex_mod.supported_efforts("") is None              # config.toml が無い
    (codex_home_dir / "config.toml").write_text('approval = "x"\n', encoding="utf-8")
    assert codex_mod.supported_efforts("") is None              # model が無い


def test_supported_efforts_missing_or_broken_catalog_is_none(codex_home_dir):
    assert codex_mod.supported_efforts("gpt-x") is None         # ファイルが無い
    codex_home_dir.mkdir(parents=True, exist_ok=True)
    (codex_home_dir / "models_cache.json").write_text("{not json", encoding="utf-8")
    assert codex_mod.supported_efforts("gpt-x") is None


@pytest.mark.parametrize("levels", [
    "low",                                       # list でない
    [],                                          # 空リスト
    [{"x": 1}, 7, {"effort": "  "}],             # 有効な値が無い
])
def test_supported_efforts_bad_levels_is_none(codex_home_dir, levels):
    _write_catalog(codex_home_dir, [{"slug": "gpt-x", "supported_reasoning_levels": levels}])
    assert codex_mod.supported_efforts("gpt-x") is None


def test_supported_efforts_unknown_model_is_none(codex_home_dir):
    _write_catalog(codex_home_dir, [
        {"slug": "gpt-x", "supported_reasoning_levels": _levels("low")},
    ])
    assert codex_mod.supported_efforts("gpt-y") is None


def test_supported_efforts_none_only_is_empty(codex_home_dir):
    _write_catalog(codex_home_dir, [
        {"slug": "gpt-x", "supported_reasoning_levels": [{"effort": "none"}]},
    ])
    assert codex_mod.supported_efforts("gpt-x") == ()


def test_supported_efforts_rereads_changed_file(codex_home_dir):
    _write_catalog(codex_home_dir, [
        {"slug": "gpt-x", "supported_reasoning_levels": _levels("low")},
    ])
    assert codex_mod.supported_efforts("gpt-x") == ("low",)
    _write_catalog(codex_home_dir, [
        {"slug": "gpt-x", "supported_reasoning_levels": _levels("low", "medium", "ultra")},
    ])
    assert codex_mod.supported_efforts("gpt-x") == ("low", "medium", "ultra")


def test_codex_home_blank_env_is_home_dot_codex(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", "   ")
    monkeypatch.setattr(codex_mod.Path, "home", lambda: tmp_path)
    assert codex_mod.codex_home() == tmp_path / ".codex"
