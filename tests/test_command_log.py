"""command_log.jsonl: traceback の記録（Issue #100 D-4）、秘密の結果の伏せ字・
ローテーション・wait_for の回転追従（D-9）。実 data/llm_state には触れない。"""

from __future__ import annotations

import json
import os
import time

import pytest

from llm_bridge import commands

_ID = "0" * 32


@pytest.fixture()
def env(monkeypatch, tmp_path):
    log = tmp_path / "command_log.jsonl"
    old = tmp_path / "command_log.jsonl.1"
    results = tmp_path / "results"

    def _results_dir():
        results.mkdir(exist_ok=True)
        return results

    monkeypatch.setattr(commands, "command_log_path", lambda: log)
    monkeypatch.setattr(commands, "rotated_command_log_path", lambda: old)
    monkeypatch.setattr(commands, "command_results_dir", _results_dir)
    return log, old, results


class _Win:
    def __init__(self, fn):
        self._fn = fn

    def has_command(self, verb):
        return True

    def dispatch_command(self, verb, **kwargs):
        return self._fn()


def _entries(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _payload(verb, cmd_id=_ID):
    return {"id": cmd_id, "tier": "window", "verb": verb, "args": {}}


# ---- D-4: error / traceback ----

def test_execute_error_records_type_message_and_traceback(env):
    log, _, _ = env

    def boom():
        raise ValueError("boom")

    commands._execute(_Win(boom), _payload("x"))
    e = _entries(log)[-1]
    assert e["status"] == "error"
    assert e["error"] == "ValueError: boom"
    assert "Traceback" in e["traceback"] and "boom" in e["traceback"]


def test_execute_ok_has_null_traceback(env):
    log, _, _ = env
    commands._execute(_Win(lambda: "fine"), _payload("x"))
    e = _entries(log)[-1]
    assert e["status"] == "ok"
    assert e["traceback"] is None


def test_execute_error_traceback_includes_cause_frames(env):
    log, _, _ = env

    def inner_analysis_frame():
        raise ValueError("bad state")

    def outer():
        try:
            inner_analysis_frame()
        except ValueError as e:
            raise RuntimeError("outer") from e

    commands._execute(_Win(outer), _payload("x"))
    tb = _entries(log)[-1]["traceback"]
    assert "inner_analysis_frame" in tb
    assert "bad state" in tb
    assert "The above exception was the direct cause" in tb


# ---- D-9: 秘密の結果 ----

def test_secret_result_not_in_log(env):
    log, _, results = env
    commands._execute(_Win(lambda: "tok-SECRET"), _payload("meeting-token"))
    assert "tok-SECRET" not in log.read_text(encoding="utf-8")
    e = _entries(log)[-1]
    assert e["result"] == "<redacted>"
    assert e["result_redacted"] is True
    assert (results / f"{_ID}.json").is_file()


def test_wait_for_resolves_secret_and_deletes_file(env):
    _, _, results = env
    commands._execute(_Win(lambda: "tok-SECRET"), _payload("meeting-token"))
    entry = commands.wait_for(_ID, timeout=1.0)
    assert entry["result"] == "tok-SECRET"
    assert "result_redacted" not in entry
    assert not (results / f"{_ID}.json").exists()


def test_wait_for_keeps_redacted_when_results_dir_fails(env, monkeypatch):
    """results/ を用意できない（mkdir が OSError）ときも、wait_for は例外を出さず
    <redacted> のままエントリを返す。"""
    commands._execute(_Win(lambda: "tok-SECRET"), _payload("meeting-token"))

    def _broken_results_dir():
        raise PermissionError("results dir unavailable")

    monkeypatch.setattr(commands, "command_results_dir", _broken_results_dir)
    entry = commands.wait_for(_ID, timeout=1.0)
    assert entry is not None
    assert entry["result"] == "<redacted>"
    assert entry["result_redacted"] is True


def test_secret_with_invalid_id_is_redacted_without_file(env):
    log, _, results = env
    commands._execute(_Win(lambda: "tok-SECRET"), _payload("meeting-token", "../x"))
    assert not results.exists() or list(results.iterdir()) == []
    e = _entries(log)[-1]
    assert e["result"] == "<redacted>"
    assert e["result_redacted"] is False
    assert "tok-SECRET" not in log.read_text(encoding="utf-8")


def test_non_secret_verb_logged_as_is(env):
    log, _, _ = env
    commands._execute(_Win(lambda: ["a", "b"]), _payload("list-tabs"))
    e = _entries(log)[-1]
    assert e["result"] == ["a", "b"]
    assert "result_redacted" not in e


def test_prune_removes_old_result_files(env):
    _, _, results = env
    results.mkdir()
    old = results / "old.json"
    old.write_text("{}", encoding="utf-8")
    past = time.time() - 7200
    os.utime(old, (past, past))
    recent = results / "recent.json"
    recent.write_text("{}", encoding="utf-8")
    commands._write_result("1" * 32, "x")
    assert not old.exists()
    assert recent.exists()
    assert (results / f"{'1' * 32}.json").exists()


# ---- D-9: ローテーション ----

def _ids(path):
    return [e["id"] for e in _entries(path)]


def test_rotation_moves_log_to_dot1(env, monkeypatch):
    log, old, _ = env
    monkeypatch.setattr(commands, "_LOG_ROTATE_BYTES", 1)
    for i in ("e1", "e2", "e3"):
        commands._append_log({"id": i})
    assert _ids(log) == ["e3"]
    assert _ids(old) == ["e2"]


def test_wait_for_finds_entry_in_rotated_file(env, monkeypatch):
    monkeypatch.setattr(commands, "_LOG_ROTATE_BYTES", 1)
    commands._append_log({"id": "T", "status": "ok"})
    commands._append_log({"id": "X"})
    entry = commands.wait_for("T", timeout=1.0)
    assert entry is not None and entry["id"] == "T"


def test_rotate_failure_is_ignored(env, monkeypatch):
    log, old, _ = env
    monkeypatch.setattr(commands, "_LOG_ROTATE_BYTES", 1)
    commands._append_log({"id": "e1"})

    def deny(*a, **k):
        raise PermissionError("in use")

    monkeypatch.setattr(commands.os, "replace", deny)
    commands._append_log({"id": "e2"})
    assert _ids(log) == ["e1", "e2"]
    assert not old.exists()


def _line(cmd_id: str, pad: int = 0) -> bytes:
    return (json.dumps({"id": cmd_id, "pad": "x" * pad}) + "\n").encode("utf-8")


def _line_of_size(cmd_id: str, size: int) -> bytes:
    base = len(_line(cmd_id, 0))
    assert size >= base
    out = _line(cmd_id, size - base)
    assert len(out) == size
    return out


def _seed(log) -> int:
    """本体に pad 付きのエントリ A・B を書き、その大きさ（off）を返す。"""
    log.write_bytes(_line("A", 300) + _line("B", 300))
    return log.stat().st_size


def _append(path, data: bytes) -> None:
    with open(path, "ab") as f:
        f.write(data)


def _once(fn):
    done = [False]

    def fake(_secs):
        if not done[0]:
            done[0] = True
            fn()

    return fake


def test_wait_for_rotation_new_main_equal_size(env, monkeypatch):
    log, old, _ = env
    off = _seed(log)

    def rotate():
        _append(log, _line("T"))
        os.replace(log, old)
        log.write_bytes(_line_of_size("X", off))
        assert log.stat().st_size == off

    monkeypatch.setattr(commands.time, "sleep", _once(rotate))
    entry = commands.wait_for("T", timeout=2.0, poll=0.0)
    assert entry is not None and entry["id"] == "T"


def test_wait_for_rotation_new_main_larger(env, monkeypatch):
    log, old, _ = env
    off = _seed(log)

    def rotate():
        _append(log, _line("T"))
        os.replace(log, old)
        log.write_bytes(_line_of_size("X", off + 4000))

    monkeypatch.setattr(commands.time, "sleep", _once(rotate))
    entry = commands.wait_for("T", timeout=2.0, poll=0.0)
    assert entry is not None and entry["id"] == "T"


def test_wait_for_rotation_between_sig_and_open(env, monkeypatch):
    """ループ 2 周目で .1 のシグネチャを読んだ直後に回転する。2 周目は新しい本体を
    旧 offset から読んで空振りし、3 周目でシグネチャの変化を検知して .1 から拾う。"""
    log, old, _ = env
    off = _seed(log)
    real = commands._log_sig
    calls = [0]

    def wrapped(path):
        calls[0] += 1
        value = real(path)
        if calls[0] == 3:
            _append(log, _line("T"))
            os.replace(log, old)
            log.write_bytes(_line_of_size("X", off + 100))
        return value

    monkeypatch.setattr(commands, "_log_sig", wrapped)
    entry = commands.wait_for("T", timeout=2.0, poll=0.0)
    assert entry is not None and entry["id"] == "T"
    assert calls[0] >= 4


def test_wait_for_rotation_rereads_new_main_from_zero(env, monkeypatch):
    log, old, _ = env
    off = _seed(log)

    def rotate():
        os.replace(log, old)
        log.write_bytes(_line("T") + _line_of_size("X", off + 100))

    monkeypatch.setattr(commands.time, "sleep", _once(rotate))
    entry = commands.wait_for("T", timeout=2.0, poll=0.0)
    assert entry is not None and entry["id"] == "T"


def test_wait_for_double_rotation_times_out(env, monkeypatch):
    log, old, _ = env
    _seed(log)

    def rotate_twice():
        _append(log, _line("T"))
        os.replace(log, old)
        log.write_bytes(_line("X"))
        os.replace(log, old)
        log.write_bytes(_line("Y"))

    monkeypatch.setattr(commands.time, "sleep", _once(rotate_twice))
    assert commands.wait_for("T", timeout=0.3, poll=0.0) is None


def test_scan_from_reads_complete_lines_only(tmp_path):
    p = tmp_path / "log.jsonl"
    assert commands._scan_from(p, 7, "T") == (None, 7)        # 無い
    head = _line("A")
    p.write_bytes(head + _line("T") + b'{"id": "U"')         # 末尾は改行なし
    entry, pos = commands._scan_from(p, len(head), "T")
    assert entry["id"] == "T" and pos == len(head) + len(_line("T"))
    entry, pos = commands._scan_from(p, len(head), "U")
    assert entry is None and pos == len(head) + len(_line("T"))  # 未完の行の手前
    entry, pos = commands._scan_from(p, 10_000, "A")          # start > size → 0 から
    assert entry["id"] == "A"
