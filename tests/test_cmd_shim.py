"""npm .cmd シム解決（common.proc.resolve_cmd_shim）と各バックエンドの spawn 経路。

背景: cmd.exe /c ラップは引数中の最初の改行で残り全部を切断する（実測:
5603 文字の system プロンプトが 372 文字に切れ、後続の --resume ごと消えた）。
複数行の --append-system-prompt を渡す claude/pi はシムを cmd.exe 経由で
起動してはならず、シムの指す実体を直接 spawn する。ここではその解決規則と、
3 バックエンドが解決結果を argv 先頭に採用する（cmd.exe を挟まない）ことを固定する。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from common.proc import resolve_cmd_shim
from llm_backend.base import Message


# npm cmd-shim の実物 2 形式（claude = exe 直接型 / pi・codex = node+JS 型）。
_EXE_SHIM = (
    "@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n"
    ":start\r\nSETLOCAL\r\nCALL :find_dp0\r\n"
    '"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe"   %*\r\n'
)
_NODE_SHIM = (
    "@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n"
    ":start\r\nSETLOCAL\r\nCALL :find_dp0\r\n\r\n"
    'IF EXIST "%dp0%\\node.exe" (\r\n  SET "_prog=%dp0%\\node.exe"\r\n) ELSE (\r\n'
    '  SET "_prog=node"\r\n  SET PATHEXT=%PATHEXT:;.JS;=;%\r\n)\r\n\r\n'
    "endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "
    '"%_prog%"  "%dp0%\\node_modules\\@earendil-works\\pi-coding-agent\\dist\\cli.js" %*\r\n'
)


def _make_exe_shim(tmp_path: Path) -> tuple[Path, Path]:
    shim = tmp_path / "claude.CMD"
    shim.write_text(_EXE_SHIM, encoding="utf-8")
    target = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"MZ")
    return shim, target


def _make_node_shim(tmp_path: Path, *, local_node: bool) -> tuple[Path, Path]:
    shim = tmp_path / "pi.cmd"
    shim.write_text(_NODE_SHIM, encoding="utf-8")
    entry = tmp_path / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "cli.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("// cli", encoding="utf-8")
    if local_node:
        (tmp_path / "node.exe").write_bytes(b"MZ")
    return shim, entry


class TestResolveCmdShim:
    def test_exe_direct_shim_resolves_to_exe(self, tmp_path):
        shim, target = _make_exe_shim(tmp_path)
        assert resolve_cmd_shim(str(shim)) == [str(target)]

    def test_node_shim_prefers_sibling_node(self, tmp_path):
        shim, entry = _make_node_shim(tmp_path, local_node=True)
        assert resolve_cmd_shim(str(shim)) == [
            str(tmp_path / "node.exe"), str(entry)
        ]

    def test_node_shim_falls_back_to_path_node(self, tmp_path, monkeypatch):
        shim, entry = _make_node_shim(tmp_path, local_node=False)
        monkeypatch.setattr(
            "common.proc.shutil.which",
            lambda name: r"C:\nodejs\node.exe" if name == "node" else None,
        )
        assert resolve_cmd_shim(str(shim)) == [r"C:\nodejs\node.exe", str(entry)]

    def test_node_shim_without_node_anywhere_fails(self, tmp_path, monkeypatch):
        shim, _ = _make_node_shim(tmp_path, local_node=False)
        monkeypatch.setattr("common.proc.shutil.which", lambda name: None)
        assert resolve_cmd_shim(str(shim)) == []

    def test_tilde_dp0_variant(self, tmp_path):
        shim = tmp_path / "tool.cmd"
        shim.write_text(
            '@node  "%~dp0\\node_modules\\pkg\\cli.js" %*\r\n', encoding="utf-8"
        )
        entry = tmp_path / "node_modules" / "pkg" / "cli.js"
        entry.parent.mkdir(parents=True)
        entry.write_text("// cli", encoding="utf-8")
        (tmp_path / "node.exe").write_bytes(b"MZ")
        # %~dp0 は末尾 \ 込みで展開されるため参照は "%~dp0\..." → \\ になるが
        # 解決結果は同じでなければならない。
        assert resolve_cmd_shim(str(shim))[-1] == str(entry)

    def test_hand_written_shim_without_dp0_fails(self, tmp_path):
        shim = tmp_path / "custom.bat"
        shim.write_text("@ECHO off\r\nsome-tool %*\r\n", encoding="utf-8")
        assert resolve_cmd_shim(str(shim)) == []

    def test_missing_shim_file_fails(self, tmp_path):
        assert resolve_cmd_shim(str(tmp_path / "nope.cmd")) == []

    def test_dangling_target_fails(self, tmp_path):
        shim = tmp_path / "claude.CMD"
        shim.write_text(_EXE_SHIM, encoding="utf-8")  # 実体ファイルは作らない
        assert resolve_cmd_shim(str(shim)) == []

    def test_node_exe_itself_is_never_the_direct_target(self, tmp_path):
        shim = tmp_path / "t.cmd"
        shim.write_text('"%dp0%\\node.exe"  "%dp0%\\x.js" %*\r\n', encoding="utf-8")
        (tmp_path / "node.exe").write_bytes(b"MZ")
        (tmp_path / "x.js").write_text("// x", encoding="utf-8")
        resolved = resolve_cmd_shim(str(shim))
        assert resolved == [str(tmp_path / "node.exe"), str(tmp_path / "x.js")]


# ---------------------------------------------------------------------------
# バックエンド spawn 統合: シム bin → 実体 argv、cmd.exe を挟まない
# ---------------------------------------------------------------------------


class TestBackendSpawnBypassesCmdExe:
    def _fake_popen_factory(self, captured: dict, stdout_lines: list[bytes]):
        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            proc = MagicMock()
            proc.stdin = MagicMock()
            proc.stdout = iter(stdout_lines)
            proc.stderr = iter([])
            proc.poll.return_value = 0
            proc.wait.return_value = 0
            proc.returncode = 0
            return proc
        return fake_popen

    def test_claude_shim_resolved_no_cmd_exe(self, tmp_path, monkeypatch):
        from llm_backend.claude_code import ClaudeCodeBackend
        shim, target = _make_exe_shim(tmp_path)
        monkeypatch.setattr(sys, "platform", "win32")
        captured = {}
        monkeypatch.setattr(
            "llm_backend.claude_code.subprocess.Popen",
            self._fake_popen_factory(
                captured,
                [b'{"type": "result", "subtype": "success", "session_id": "s1"}\n'],
            ),
        )
        backend = ClaudeCodeBackend({"bin": str(shim), "cwd": str(tmp_path)})
        list(backend.stream([Message(role="user", content="hi")]))
        cmd = captured["cmd"]
        assert cmd[0] == str(target)
        assert "cmd.exe" not in [str(a).lower() for a in cmd]
        # シム解決で argv[0] を差し替えても後続フラグは無傷。
        assert "--append-system-prompt" in cmd

    def test_pi_shim_resolved_no_cmd_exe(self, tmp_path, monkeypatch):
        from llm_backend.pi import PiCodingAgentBackend
        shim, entry = _make_node_shim(tmp_path, local_node=True)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr("shutil.which", lambda name: str(shim))
        monkeypatch.delenv("PI_API_KEY", raising=False)
        captured = {}
        monkeypatch.setattr(
            "subprocess.Popen",
            self._fake_popen_factory(
                captured, [json.dumps({"type": "agent_end"}).encode() + b"\n"]
            ),
        )
        backend = PiCodingAgentBackend({"bin": str(shim), "cwd": str(tmp_path)})
        list(backend.stream([Message(role="user", content="hi")]))
        cmd = captured["cmd"]
        assert cmd[:2] == [str(tmp_path / "node.exe"), str(entry)]
        assert "cmd.exe" not in [str(a).lower() for a in cmd]
        assert "--append-system-prompt" in cmd

    def test_codex_shim_resolved_no_cmd_exe(self, tmp_path, monkeypatch):
        from llm_backend.codex import CodexBackend
        shim, entry = _make_node_shim(tmp_path, local_node=True)
        monkeypatch.setattr(sys, "platform", "win32")
        captured = {}
        monkeypatch.setattr(
            "llm_backend.codex.subprocess.Popen",
            self._fake_popen_factory(
                captured,
                [json.dumps({"type": "turn.completed"}).encode() + b"\n"],
            ),
        )
        backend = CodexBackend({"bin": str(shim), "cwd": str(tmp_path)})
        list(backend.stream([Message(role="user", content="hi")]))
        cmd = captured["cmd"]
        assert cmd[:2] == [str(tmp_path / "node.exe"), str(entry)]
        assert "cmd.exe" not in [str(a).lower() for a in cmd]

    def test_unresolvable_shim_falls_back_to_cmd_exe(self, tmp_path, monkeypatch):
        from llm_backend.pi import PiCodingAgentBackend
        shim = tmp_path / "pi.cmd"
        shim.write_text("@ECHO off\r\npi %*\r\n", encoding="utf-8")  # 自作シム
        monkeypatch.setattr(sys, "platform", "win32")
        # common.proc も同じ shutil モジュールを見るので 1 本の patch で両方を制御:
        # pi bin 解決はシムを返し、シム内 node 解決は失敗させる。
        monkeypatch.setattr(
            "shutil.which", lambda name: str(shim) if name != "node" else None
        )
        monkeypatch.delenv("PI_API_KEY", raising=False)
        captured = {}
        monkeypatch.setattr(
            "subprocess.Popen",
            self._fake_popen_factory(
                captured, [json.dumps({"type": "agent_end"}).encode() + b"\n"]
            ),
        )
        backend = PiCodingAgentBackend({"bin": str(shim), "cwd": str(tmp_path)})
        list(backend.stream([Message(role="user", content="hi")]))
        assert captured["cmd"][:2] == ["cmd.exe", "/c"]
