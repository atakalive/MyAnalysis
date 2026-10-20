"""Adapter wiring tests for ClaudeCodeBackend history replay (Issue #63).

Fakes subprocess.Popen and captures the JSON written to stdin (the `text`
field carries the prompt) to prove stream() wires build_prompt_with_history
with replay=(session_id is None).
"""

from __future__ import annotations

import gc
import io
import json
import logging
import queue
import subprocess
import threading
import time
from unittest.mock import MagicMock

import pytest

from llm_backend import claude_code
from llm_backend.base import InjectSupport, Message, UserAbsorbed, UserUnknown
from llm_backend.claude_code import (
    _DEFAULT_PERMISSION_MODE,
    ClaudeCodeBackend,
    _resolve_permission_mode,
    permission_mode_is_invalid,
)
# conftest の autouse スタブより前に束縛される本物（判定そのもののテスト用。Issue #119）
from llm_backend.claude_code import _replay_supported as _real_replay_supported


@pytest.fixture(autouse=True, scope="module")
def _collect_garbage_on_main_thread():
    """前のモジュールの GUI テストが残したごみ（Qt オブジェクトを含む）を、メインスレッドで回収しておく。

    このファイルのテストは stream の書き手・stderr の読み手のスレッドを動かす。ごみが残ったままだと
    そのスレッドで循環 GC が走って Qt オブジェクトを破棄しうる。全体の実行（worktree）で、_shut_stdin が
    書き手を join している最中の segfault が 3 回中 2 回起きた（2026-10-20）ので、先に回収する。"""
    gc.collect()
    yield


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


# ---- Issue #119: 応答中の追加送信（inject） ----

SID = "s1"
INIT = {"type": "system", "subtype": "init", "session_id": SID, "model": "claude-test"}
RESULT_OK = {"type": "result", "subtype": "success", "session_id": SID,
             "usage": {"input_tokens": 1, "output_tokens": 1}}


class SignalStdin:
    """書かれる・閉じられるたびにテストへ知らせる stdin。"""

    def __init__(self):
        self.written: list[bytes] = []
        self.closed = False
        self.cond = threading.Condition()

    def write(self, b):
        with self.cond:
            if self.closed:
                raise ValueError("write to closed file")
            self.written.append(b)
            self.cond.notify_all()

    def flush(self):
        pass

    def close(self):
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def wait_for(self, pred, timeout=5.0) -> bool:
        with self.cond:
            return self.cond.wait_for(lambda: pred(self), timeout)

    def messages(self) -> list[dict]:
        return [json.loads(b) for b in list(self.written)]


class ScriptedPopen:
    """stdout の行をテストから足していける Popen の偽物（書かれた stdin を見てから続きを流す）。

    push(...) で行（dict は JSON にする）を足し、end() で EOF。行が timeout 秒来なければ EOF 扱い。
    wait_gate を渡すと wait() はその Event が立つまで戻らない（後始末を止めておく）。
    """

    def __init__(self, cmd, *, returncode=0, stderr_bytes=b"", timeout=10.0,
                 wait_gate=None, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.stdin = SignalStdin()
        self.stderr = io.BytesIO(stderr_bytes)
        self.returncode = None
        self._rc = returncode
        self._lines: queue.Queue = queue.Queue()
        self._timeout = timeout
        self._wait_gate = wait_gate
        self.wait_entered = threading.Event()
        self.stdout = self._read()
        self.pid = -1        # 実在しない PID（万一 kill に渡っても何も止めない）

    def _read(self):
        while True:
            try:
                item = self._lines.get(timeout=self._timeout)
            except queue.Empty:
                return
            if item is None:
                return
            yield (item if isinstance(item, bytes) else json.dumps(item).encode("utf-8")) + b"\n"

    def push(self, *items):
        for x in items:
            self._lines.put(x)

    def end(self):
        self._lines.put(None)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_entered.set()
        if self._wait_gate is not None:
            self._wait_gate.wait(10)
        self.returncode = self._rc
        return self._rc


def wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def lc(cu, state):
    return {"type": "command_lifecycle", "command_uuid": cu, "state": state,
            "uuid": "frame-" + state, "session_id": SID}


def echo(u, text="追加"):
    return {"type": "user", "isReplay": True, "uuid": u, "session_id": SID,
            "parent_tool_use_id": None,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def result_with(first, uuids=None, **kw):
    r = dict(RESULT_OK, user_message_uuid=first, **kw)
    if uuids is not None:
        r["user_message_uuids"] = list(uuids)
    return r


class ScriptedRun:
    def __init__(self, backend, created, events, errors, thread, killed, supports):
        self.backend = backend
        self.supports = supports    # InjectSupport.supported（events とは分けて記録する）
        self._created = created
        self.events = events
        self.errors = errors
        self.thread = thread
        self.killed = killed

    @property
    def proc(self) -> ScriptedPopen:
        assert wait_until(lambda: self._created), "claude was not launched"
        return self._created[-1]

    def ready(self):
        """最初の行を書き終え、書き手が動いている（cancel が kill に回らない）ところまで待つ。"""
        proc = self.proc
        assert wait_until(lambda: (cur := self.backend._cur) is not None
                          and cur.proc is proc and cur.writer is not None)

    def first_uuid(self):
        assert self.proc.stdin.wait_for(lambda s: len(s.written) >= 1)
        return json.loads(self.proc.stdin.written[0])["uuid"]

    def track(self):
        """最初の発言の lifecycle と init を流す（CLI が uuid を追跡している）。"""
        self.ready()
        u0 = self.first_uuid()
        self.proc.push(lc(u0, "queued"), lc(u0, "started"), INIT)
        assert wait_until(lambda: self.backend._cur.tracked)
        return u0

    def written(self, n):
        assert self.proc.stdin.wait_for(lambda s: len(s.written) >= n), self.proc.stdin.written
        return self.proc.stdin.messages()

    def wait_events(self, pred, timeout=5.0):
        assert wait_until(lambda: pred(list(self.events)), timeout), self.events

    def join(self, timeout=5.0):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "stream did not finish"
        return list(self.events)


@pytest.fixture
def scripted(monkeypatch, tmp_path):
    """ScriptedPopen で stream を裏のスレッドで回す。テストから行を足しながら確かめる。"""
    monkeypatch.setattr(claude_code, "_replay_supported", lambda claude_bin: True)
    killed: list = []
    monkeypatch.setattr(ClaudeCodeBackend, "_kill_tree", lambda self, proc: killed.append(proc))

    def start(*, backend=None, returncode=0, stderr=b"", wait_gate=None, supported=True,
              close_when=None):
        monkeypatch.setattr(claude_code, "_replay_supported", lambda claude_bin: supported)
        backend = backend or ClaudeCodeBackend({"bin": "/fake/claude", "cwd": str(tmp_path)})
        created: list[ScriptedPopen] = []

        def factory(cmd, **kwargs):
            p = ScriptedPopen(cmd, returncode=returncode, stderr_bytes=stderr,
                              wait_gate=wait_gate, **kwargs)
            created.append(p)
            return p

        monkeypatch.setattr(claude_code.subprocess, "Popen", factory)
        events: list = []
        errors: list = []
        supports: list = []
        gen = backend.stream([Message(role="user", content="hello")])

        def run():
            try:
                for ev in gen:
                    if isinstance(ev, InjectSupport):
                        supports.append(ev.supported)
                        continue
                    events.append(ev)
                    if close_when is not None and close_when.is_set():
                        gen.close()        # GUI の Stop と同じ: 次のイベントで抜けて close
                        return
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return ScriptedRun(backend, created, events, errors, t, killed, supports)

    return start


def test_replay_supported_bundled_path_without_process(monkeypatch):
    monkeypatch.setattr(claude_code, "_VERSION_CACHE", {})

    def boom(*a, **k):
        raise AssertionError("subprocess.run must not be called")

    monkeypatch.setattr(claude_code.subprocess, "run", boom)
    path = ("/home/u/.vscode/extensions/anthropic.claude-code-{}-linux-x64/"
            "resources/native-binary/claude")
    assert _real_replay_supported(path.format("2.1.282")) is True
    assert _real_replay_supported(path.format("2.1.281")) is False


def _fake_run(monkeypatch, outputs):
    calls: list = []

    def run(cmd, **kw):
        calls.append(cmd)
        out = outputs[len(calls) - 1]
        if isinstance(out, BaseException):
            raise out
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr=b"")

    monkeypatch.setattr(claude_code.subprocess, "run", run)
    return calls


def test_replay_supported_reads_version_and_caches(monkeypatch):
    monkeypatch.setattr(claude_code, "_VERSION_CACHE", {})
    calls = _fake_run(monkeypatch, [b"2.1.301 (Claude Code)\n", b"2.1.200 (Claude Code)\n"])
    assert _real_replay_supported("/opt/claude") is True
    assert calls == [["/opt/claude", "--version"]]
    assert _real_replay_supported("/opt/claude") is True
    assert len(calls) == 1                        # 2 回目はキャッシュ
    assert _real_replay_supported("/opt/old-claude") is False
    assert len(calls) == 2


def test_replay_supported_failure_not_cached(monkeypatch):
    monkeypatch.setattr(claude_code, "_VERSION_CACHE", {})
    calls = _fake_run(monkeypatch, [
        OSError("no such file"),
        subprocess.TimeoutExpired("claude", 10),
        b"no version here\n",
        b"2.1.301 (Claude Code)\n",
    ])
    assert _real_replay_supported("/opt/claude") is False
    assert _real_replay_supported("/opt/claude") is False
    assert _real_replay_supported("/opt/claude") is False
    assert len(calls) == 3                        # 失敗はキャッシュしない
    assert _real_replay_supported("/opt/claude") is True
    assert len(calls) == 4


def test_unsupported_cli_runs_as_before(scripted):
    r = scripted(supported=False)
    r.ready()
    assert "--replay-user-messages" not in r.proc.cmd
    first = r.written(1)[0]
    assert set(first) == {"type", "message"}
    assert first["type"] == "user"
    assert r.backend.inject("後から") is None
    assert r.backend.supports_inject() is False
    r.backend.cancel()
    assert r.proc.stdin.wait_for(lambda s: s.closed)
    msgs = r.proc.stdin.messages()
    assert len(msgs) == 2
    assert msgs[1]["type"] == "control_request"
    assert msgs[1]["request"] == {"subtype": "interrupt"}
    r.proc.push(RESULT_OK)
    r.join()
    assert r.errors == []


def test_supported_cmd_has_flag_and_first_uuid(scripted):
    r = scripted()
    r.ready()
    cmd = r.proc.cmd
    assert cmd[cmd.index("--include-partial-messages") + 1] == "--replay-user-messages"
    first = r.written(1)[0]
    assert isinstance(first["uuid"], str) and first["uuid"]
    assert first["session_id"] == ""
    assert first["parent_tool_use_id"] is None
    assert first["message"]["content"][0]["text"].endswith("hello")
    assert r.backend.supports_inject() is True
    r.proc.push(RESULT_OK)
    r.join()
    assert r.errors == []


def test_inject_held_until_first_lifecycle(scripted):
    r = scripted()
    r.ready()
    u0 = r.first_uuid()
    x = r.backend.inject("待って")
    assert isinstance(x, str) and x
    time.sleep(0.2)
    assert len(r.proc.stdin.written) == 1
    r.proc.push(lc(u0, "queued"))
    sent = r.written(2)[1]
    assert sent["uuid"] == x
    assert sent["message"]["content"] == [{"type": "text", "text": "待って"}]
    r.proc.push(INIT, result_with(u0, [u0, x]))
    assert r.join() == [UserAbsorbed(uid=x)]

    # lifecycle が一度も来ない（uuid を追跡しない CLI）: 書かれず、UserUnknown も出ない
    r = scripted()
    r.ready()
    u0 = r.first_uuid()
    assert r.backend.inject("待って") is not None
    r.proc.push(INIT, TD("ok"), result_with(u0, [u0]))
    events = r.join()
    assert len(r.proc.stdin.written) == 1
    assert events == [TextDelta(text="ok")]


def test_echo_absorbed_in_text_order_first_echo_ignored(scripted):
    r = scripted()
    u0 = r.track()
    x = r.backend.inject("追加")
    r.written(2)
    r.proc.push(TD("A"), echo(u0, "hello"), echo(x), TD("B"), result_with(u0, [u0, x]))
    assert r.join() == [TextDelta(text="A"), UserAbsorbed(uid=x), TextDelta(text="B")]

    # 空の思考ブロックの枠が未解決の間に来たエコーは、枠を埋める思考の行の後に出る
    r = scripted()
    u0 = r.track()
    y = r.backend.inject("追加")
    r.written(2)
    r.proc.push(MS, B(0), S(0), echo(y), A(TH("考えた")), TD("C"), result_with(u0, [u0, y]))
    assert r.join() == [TextDelta(text=format_thinking_line("考えた")), UserAbsorbed(uid=y),
                        TextDelta(text="C")]


def test_continuation_turn_one_stream_first_uuid_not_waited(scripted):
    r = scripted()
    u0 = r.track()
    x = r.backend.inject("追加")
    r.written(2)
    r.proc.push(TD("A"), result_with(u0, [u0]))
    r.wait_events(lambda evs: evs == [TextDelta(text="A")])
    time.sleep(0.1)
    assert r.thread.is_alive()                    # 書いた足し分が片付くのを待っている
    r.proc.push(dict(INIT), echo(x), TD("B"), result_with(u0, [x]))
    assert r.join() == [TextDelta(text="A"), UserAbsorbed(uid=x), TextDelta(text="B")]
    assert len(r._created) == 1

    # 足し分の無い送信は、最初の uuid の completed が来ていなくても 1 回目の result で終わる
    r = scripted()
    u0 = r.track()
    r.proc.push(TD("ok"), result_with(u0, [u0]))
    assert r.join() == [TextDelta(text="ok")]


def test_cancel_writes_interrupt_cancel_queued_then_close(scripted):
    r = scripted()
    u0 = r.track()
    x = r.backend.inject("追加")
    r.written(2)
    t0 = time.monotonic()
    r.backend.cancel()
    assert time.monotonic() - t0 < 0.5
    assert r.proc.stdin.wait_for(lambda s: s.closed)
    msgs = r.proc.stdin.messages()
    assert len(msgs) == 3
    assert msgs[2]["type"] == "control_request"
    assert msgs[2]["request"] == {"subtype": "interrupt", "cancel_queued": True}
    r.proc.push(result_with(u0, [u0]))
    assert r.join() == [UserUnknown(uid=x)]


def test_cancelled_receipt_not_unknown(scripted):
    r = scripted()
    u0 = r.track()
    a = r.backend.inject("A")
    b = r.backend.inject("B")
    r.written(3)
    r.backend.cancel()
    rid = r.written(4)[3]["request_id"]
    r.proc.push(
        lc(b, "started"),
        {"type": "control_response", "session_id": SID,
         "response": {"subtype": "success", "request_id": rid,
                      "response": {"cancelled": [a]}}},
        result_with(u0, [u0]),
    )
    assert r.join() == [UserUnknown(uid=b)]


def test_stall_gives_up_and_drops_later_result(scripted, monkeypatch):
    monkeypatch.setattr(claude_code, "_STALL_SEC", 0.05)
    monkeypatch.setattr(claude_code, "_DRAIN_SEC", 0.05)
    r = scripted()
    u0 = r.track()
    x = r.backend.inject("追加")
    r.written(2)
    r.proc.push(result_with(u0, [u0], total_cost_usd=0.1))
    assert r.proc.stdin.wait_for(lambda s: s.closed)
    msgs = r.proc.stdin.messages()
    assert msgs[2]["request"] == {"subtype": "interrupt", "cancel_queued": True}
    assert wait_until(lambda: r.proc in r.killed)
    usage = dict(r.backend.last_usage)
    total = r.backend.total_cost
    r.proc.push(dict(RESULT_OK, subtype="error_during_execution", is_error=True,
                     total_cost_usd=5.0, usage={"input_tokens": 99}))
    r.proc.end()
    assert r.join() == [UserUnknown(uid=x)]
    assert r.errors == []
    assert r.backend.last_usage == usage
    assert r.backend.total_cost == total


def test_cost_not_double_counted_across_results(scripted):
    r = scripted()
    u0 = r.track()
    x = r.backend.inject("追加")
    r.written(2)
    r.proc.push(result_with(u0, [u0], total_cost_usd=0.10))
    r.proc.push(dict(INIT), result_with(u0, [x], total_cost_usd=0.25))
    r.join()
    assert r.backend.total_cost == pytest.approx(0.25)
    assert r.backend.last_usage["cost"] == pytest.approx(0.25)


def test_cost_ignores_non_finite_and_huge(scripted):
    head = b'{"type": "result", "subtype": "success", "session_id": "s1", "total_cost_usd": '
    for raw in (b"1" + b"0" * 1000, b"1e400", b'"0.1"', b"true"):
        r = scripted()
        r.ready()
        r.proc.push(head + raw + b"}")
        r.join()
        assert r.errors == []
        assert r.backend.total_cost == 0.0
        assert r.backend.last_usage["cost"] == 0.0


def test_exit_code_after_result_is_not_failure(scripted):
    r = scripted(returncode=1)
    r.ready()
    r.proc.push(RESULT_OK)
    r.join()
    assert r.errors == []

    r = scripted(returncode=1, stderr=b"bad\n")
    r.ready()
    r.proc.end()
    r.join()
    assert len(r.errors) == 1
    assert isinstance(r.errors[0], RuntimeError)
    assert "claude exited with code 1" in str(r.errors[0])


def _stop_first_stream(scripted, *, stderr=b""):
    """1 本目を GUI と同じ止め方で止め、後始末（wait）の中で止めておく。"""
    gate = threading.Event()
    close_now = threading.Event()
    r1 = scripted(wait_gate=gate, close_when=close_now, stderr=stderr)
    r1.ready()
    r1.backend.cancel()
    assert r1.proc.stdin.wait_for(lambda s: s.closed)
    close_now.set()
    r1.proc.push(TD("late"))          # 次のイベントでジェネレーターを close させる
    assert r1.proc.wait_entered.wait(5)
    assert r1.thread.is_alive()
    return r1, gate


def test_stop_then_new_stream_old_cleanup_isolated(scripted):
    r1, gate = _stop_first_stream(scripted)
    backend = r1.backend
    r2 = scripted(backend=backend)
    r2.ready()
    r2.proc.push(dict(INIT, session_id="s2"), TD("X"), TD("Y"))
    r2.wait_events(lambda evs: len(evs) == 2)
    gate.set()
    r1.thread.join(5)
    assert not r1.thread.is_alive()
    assert not r2.proc.stdin.closed
    assert len(r2.proc.stdin.written) == 1
    assert backend._proc is r2.proc
    r2.proc.push(dict(RESULT_OK, session_id="s2", total_cost_usd=0.3))
    assert r2.join() == [TextDelta(text="X"), TextDelta(text="Y")]
    assert r2.errors == []
    assert backend._session_id == "s2"
    assert backend.last_usage["cost"] == pytest.approx(0.3)
    assert backend._proc is None


def test_exit_error_uses_own_stderr(scripted):
    r1, gate = _stop_first_stream(scripted, stderr=b"one\n")
    backend = r1.backend
    r2 = scripted(backend=backend, returncode=1, stderr=b"two\n")
    r2.ready()
    assert wait_until(lambda: "two" in list(backend._cur.stderr))
    gate.set()
    r1.thread.join(5)
    assert not r1.thread.is_alive()
    r2.proc.end()
    r2.join()
    assert len(r2.errors) == 1
    msg = str(r2.errors[0])
    assert "claude exited with code 1" in msg
    assert "two" in msg and "one" not in msg


@pytest.mark.parametrize("supported", [True, False])
def test_inject_support_yielded_before_launch(monkeypatch, tmp_path, supported):
    """版の判定の直後、起動の前に InjectSupport を 1 回だけ出す（GUI が送信ボタンを合わせ直す）。"""
    monkeypatch.setattr(claude_code, "_replay_supported", lambda claude_bin: supported)
    launched: list = []
    monkeypatch.setattr(claude_code.subprocess, "Popen",
                        lambda cmd, **kw: launched.append(cmd) or ScriptedPopen(cmd, **kw))
    backend = ClaudeCodeBackend({"bin": "/fake/claude", "cwd": str(tmp_path)})
    assert backend.supports_inject() is True              # 判定前は楽観的に True
    gen = backend.stream([Message(role="user", content="hello")])
    assert next(gen) == InjectSupport(supported=supported)
    assert launched == []                                 # まだ起動していない
    assert backend.supports_inject() is supported
    gen.close()
    assert launched == []


def test_scripted_run_reports_one_inject_support(scripted):
    r = scripted()
    u0 = r.track()
    r.proc.push(TD("ok"), result_with(u0, [u0]))
    assert r.join() == [TextDelta(text="ok")]
    assert r.supports == [True]


def test_wait_max_ends_wait_even_with_frames(scripted, monkeypatch):
    """待機中にフレームが _STALL_SEC より短い間隔で来続けても、_WAIT_MAX_SEC であきらめて
    interrupt（cancel_queued）と close を書く（フレームで上限は延びない）。"""
    monkeypatch.setattr(claude_code, "_STALL_SEC", 0.3)
    monkeypatch.setattr(claude_code, "_WAIT_MAX_SEC", 0.8)
    monkeypatch.setattr(claude_code, "_DRAIN_SEC", 0.05)
    r = scripted()
    u0 = r.track()
    x = r.backend.inject("追加")
    r.written(2)
    r.proc.push(result_with(u0, [u0]))
    assert wait_until(lambda: r.backend._cur.waiting)
    t0 = time.monotonic()
    while not r.proc.stdin.closed and time.monotonic() - t0 < 3.0:
        r.proc.push({"type": "system", "subtype": "status", "session_id": SID})  # init でも started でもないフレーム
        time.sleep(0.1)
    elapsed = time.monotonic() - t0
    assert r.proc.stdin.closed
    assert 0.6 <= elapsed < 2.0          # 0.3 秒の無フレームでは切れず（フレームが来続ける）、0.8 秒の上限で切れる
    msgs = r.proc.stdin.messages()
    assert msgs[2]["request"] == {"subtype": "interrupt", "cancel_queued": True}
    assert r.backend._cur.stalled
    assert wait_until(lambda: r.proc in r.killed)
    r.proc.end()
    assert r.join() == [UserUnknown(uid=x)]
    assert r.errors == []
