"""llm_bridge CLI の stdout/stderr が cp932 で落ちない（Issue #102）。"""
from __future__ import annotations

import io
import sys

import pytest

import llm_bridge.__main__ as cli


def _cp932_stream():
    buf = io.BytesIO()
    return buf, io.TextIOWrapper(buf, encoding="cp932", errors="strict")


def test_harden_stdio_backslashreplace(monkeypatch):
    buf, s = _cp932_stream()
    monkeypatch.setattr(sys, "stdout", s)
    monkeypatch.setattr(sys, "stderr", s)
    cli._harden_stdio()
    print("a—b")
    s.flush()
    assert buf.getvalue().startswith(b"a\\u2014b")


def test_harden_stdio_tolerates_streams_without_reconfigure(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", None)
    cli._harden_stdio()


def test_main_calls_harden_stdio_first(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_harden_stdio", lambda: calls.append(1))
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert calls == [1]


def test_main_cp932_strict_stdout_does_not_crash(monkeypatch):
    buf, s = _cp932_stream()
    monkeypatch.setattr(sys, "stdout", s)
    assert cli.main(["list-commands", "tab"]) == 0
    s.flush()
    assert b"\\u2014" in buf.getvalue()
