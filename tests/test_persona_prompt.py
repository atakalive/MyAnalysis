"""ペルソナ合成（compose_system_prompt）と各バックエンドへの注入のテスト。

契約: 既定＝ペルソナなしは従来プロンプトと**バイト同一**（compose が同一オブジェクト
を返す）。ペルソナありは base の後置合成で、共有ルール定数（NO_LOCAL_PERSISTENCE /
MOUNT_SAFE_EDITS）は消えない。バックエンドは stream/送信時に自分の base へ合成する。
Qt 不要・subprocess/network は全てフェイク。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from llm_backend import codex as codex_mod
from llm_backend.base import (
    MOUNT_SAFE_EDITS, NO_LOCAL_PERSISTENCE, PERSONA_HEADER, Message,
    compose_system_prompt,
)
from llm_backend.claude_code import (
    ClaudeCodeBackend, _SYSTEM_PROMPT, _system_prompt_args,
)
from llm_backend.codex import CodexBackend, _SYSTEM_PROMPT_CODEX
from llm_backend.mock import MockBackend
from llm_backend.openai_compat import OpenAICompatBackend
from llm_backend.pi import PiCodingAgentBackend, _SYSTEM_PROMPT_PI

PERSONA = "すぐ本題に入る。常に批判的。乱暴な口調だが説明は懇切丁寧。"


# ---------------------------------------------------------------------------
# compose_system_prompt
# ---------------------------------------------------------------------------


def test_compose_blank_persona_returns_same_object():
    # 既定はバイト同一どころか同一オブジェクト — 既存 pinned テスト群の回帰ガード。
    base = "BASE PROMPT"
    for empty in ("", None, "   ", " \n\t "):
        assert compose_system_prompt(base, empty) is base


def test_compose_keeps_header_and_shared_constants():
    # 実バックエンド prompt（両共有定数を内包）に合成しても定数が残存すること。
    out = compose_system_prompt(_SYSTEM_PROMPT, PERSONA)
    assert out.startswith(_SYSTEM_PROMPT)
    assert PERSONA_HEADER in out
    assert NO_LOCAL_PERSISTENCE in out
    assert MOUNT_SAFE_EDITS in out
    assert out.endswith(PERSONA)


def test_compose_strips_persona_whitespace():
    out = compose_system_prompt("B", "  X \n")
    assert out.endswith("\nX")


# ---------------------------------------------------------------------------
# setter（各バックエンド共通の実装パターン）
# ---------------------------------------------------------------------------


def test_setters_store_and_normalise_none_to_empty():
    backends = (
        ClaudeCodeBackend({}),
        PiCodingAgentBackend({}),
        CodexBackend({}),
        OpenAICompatBackend(base_url="http://x", api_key="k", model="m"),
    )
    for b in backends:
        assert b._persona == ""
        b.set_persona(PERSONA)
        assert b._persona == PERSONA
        b.set_persona(None)
        assert b._persona == ""


# ---------------------------------------------------------------------------
# claude
# ---------------------------------------------------------------------------


def test_claude_system_prompt_args_with_persona():
    composed = compose_system_prompt(_SYSTEM_PROMPT, "X")
    assert _system_prompt_args(True, "X") == ["--append-system-prompt", composed]
    assert _system_prompt_args(False, "X") == ["--system-prompt", composed]


def test_claude_system_prompt_args_default_is_identity():
    # 1 引数呼び（既存 pinned テストの経路）はペルソナなし＝同一オブジェクト。
    assert _system_prompt_args(True)[1] is _SYSTEM_PROMPT
    assert _system_prompt_args(True, "")[1] is _SYSTEM_PROMPT


def test_claude_stream_passes_instance_persona(monkeypatch, tmp_path):
    # stream の呼び出し口が self._persona を渡していることを argv で固定する。
    backend = ClaudeCodeBackend({"bin": "/usr/bin/claude", "cwd": str(tmp_path)})
    backend.set_persona(PERSONA)
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = iter([
            json.dumps({"type": "result", "subtype": "success"}).encode() + b"\n"
        ])
        proc.stderr = iter([])
        proc.poll.return_value = 0
        proc.wait.return_value = 0
        proc.returncode = 0
        return proc

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    list(backend.stream([Message(role="user", content="hi")]))
    cmd = captured["cmd"]
    idx = cmd.index("--append-system-prompt")
    assert cmd[idx + 1] == compose_system_prompt(_SYSTEM_PROMPT, PERSONA)


# ---------------------------------------------------------------------------
# pi
# ---------------------------------------------------------------------------


def _pi_capture_cmd(monkeypatch, *, persona=None):
    """test_pi_backend._capture_cmd と同型: Popen をフェイクし argv を返す。"""
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("llm_backend.pi.repo_root", lambda: Path("/repo"))
    monkeypatch.delenv("PI_API_KEY", raising=False)

    backend = PiCodingAgentBackend({})
    if persona is not None:
        backend.set_persona(persona)
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = iter([json.dumps({"type": "agent_end"}).encode() + b"\n"])
        proc.stderr = iter([])
        proc.poll.return_value = 0
        proc.wait.return_value = 0
        proc.returncode = 0
        return proc

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    list(backend.stream([Message(role="user", content="hello")]))
    return captured["cmd"]


def test_pi_persona_composed_into_argv(monkeypatch):
    cmd = _pi_capture_cmd(monkeypatch, persona=PERSONA)
    idx = cmd.index("--append-system-prompt")
    assert cmd[idx + 1] == compose_system_prompt(_SYSTEM_PROMPT_PI, PERSONA)
    assert PERSONA_HEADER in cmd[idx + 1]


def test_pi_no_persona_argv_is_identity(monkeypatch):
    cmd = _pi_capture_cmd(monkeypatch)
    idx = cmd.index("--append-system-prompt")
    assert cmd[idx + 1] is _SYSTEM_PROMPT_PI


# ---------------------------------------------------------------------------
# codex — AGENTS.md が内容も比較キーも合成文字列
# ---------------------------------------------------------------------------


class _FakeCodexPopen:
    """test_codex_backend._FakePopen と同型（events はクラス変数、毎回 fresh iter）。"""

    events: list[dict] = []

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.stdin = MagicMock()
        self.stdout = iter(
            [(json.dumps(e) + "\n").encode() for e in type(self).events]
        )
        self.stderr = iter([])
        self.returncode = 0

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


def test_codex_agents_md_composed_and_rewritten(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_mod.subprocess, "Popen", _FakeCodexPopen)
    _FakeCodexPopen.events = [
        {"type": "thread.started", "thread_id": "th-1"},
        {"type": "turn.completed", "usage": {}},
    ]
    b = CodexBackend({"bin": str(tmp_path / "codex.exe"), "cwd": str(tmp_path)})
    agents = tmp_path / "AGENTS.md"

    b.set_persona(PERSONA)
    list(b.stream([Message(role="user", content="hi")]))
    assert agents.read_text(encoding="utf-8") == compose_system_prompt(
        _SYSTEM_PROMPT_CODEX, PERSONA
    )

    # ペルソナ変更 → 次の stream（resume ターン）で書き換わる。
    b.set_persona("別のペルソナ。")
    list(b.stream([Message(role="user", content="next")]))
    assert agents.read_text(encoding="utf-8") == compose_system_prompt(
        _SYSTEM_PROMPT_CODEX, "別のペルソナ。"
    )

    # ペルソナ解除 → 素の定数へ戻る。
    b.set_persona("")
    list(b.stream([Message(role="user", content="again")]))
    assert agents.read_text(encoding="utf-8") == _SYSTEM_PROMPT_CODEX


# ---------------------------------------------------------------------------
# openai-compat — 送信 payload のみ合成、保存済み Message は非変異
# ---------------------------------------------------------------------------


class _FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter([b"data: [DONE]\n"])


def _openai_capture_payload(monkeypatch, backend, msgs):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data
        return _FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    list(backend.stream(msgs))
    return captured["data"]


def _openai_backend():
    return OpenAICompatBackend(base_url="http://x", api_key="k", model="m")


def test_openai_persona_composed_into_first_system(monkeypatch):
    b = _openai_backend()
    b.set_persona(PERSONA)
    msgs = [
        Message(role="system", content="SYS"),
        Message(role="user", content="hi"),
    ]
    body = json.loads(_openai_capture_payload(monkeypatch, b, msgs).decode("utf-8"))
    assert body["messages"][0] == {
        "role": "system", "content": compose_system_prompt("SYS", PERSONA)
    }
    assert body["messages"][1] == {"role": "user", "content": "hi"}
    # 保存済み Message は非変異（セッションは同期・永続 — mint 時の system は凍結のまま）。
    assert msgs[0].content == "SYS"


def test_openai_only_first_system_composed(monkeypatch):
    b = _openai_backend()
    b.set_persona(PERSONA)
    msgs = [
        Message(role="system", content="A"),
        Message(role="system", content="B"),
        Message(role="user", content="hi"),
    ]
    body = json.loads(_openai_capture_payload(monkeypatch, b, msgs).decode("utf-8"))
    assert body["messages"][0]["content"] == compose_system_prompt("A", PERSONA)
    assert body["messages"][1]["content"] == "B"


def test_openai_no_persona_payload_byte_identical(monkeypatch):
    # ペルソナなしは従来コードの payload とバイト同一。
    b = _openai_backend()
    msgs = [
        Message(role="system", content="SYS"),
        Message(role="user", content="hi"),
    ]
    data = _openai_capture_payload(monkeypatch, b, msgs)
    expected = json.dumps({
        "model": "m",
        "messages": [m.to_payload() for m in msgs],
        "stream": True,
    }).encode("utf-8")
    assert data == expected


def test_openai_prepends_system_when_absent(monkeypatch):
    b = _openai_backend()
    b.set_persona(PERSONA)
    msgs = [Message(role="user", content="hi")]
    body = json.loads(_openai_capture_payload(monkeypatch, b, msgs).decode("utf-8"))
    assert len(body["messages"]) == 2
    assert body["messages"][0]["role"] == "system"
    head = body["messages"][0]["content"]
    assert head.startswith(PERSONA_HEADER)
    assert PERSONA in head
    assert body["messages"][1] == {"role": "user", "content": "hi"}
    assert len(msgs) == 1  # 入力リストにも先頭挿入しない


# ---------------------------------------------------------------------------
# mock
# ---------------------------------------------------------------------------


def test_mock_set_persona_is_inert():
    # GUI の duck-typed 注入が mock でも同じ経路を通るための対称性のみ。
    b = MockBackend(model="m")
    assert b.set_persona(PERSONA) is None
