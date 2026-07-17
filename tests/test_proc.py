"""Unit tests for common.proc.no_window_kwargs().

Verifies the cross-platform contract: {} off Windows, a creationflags entry on
Windows. Uses getattr on both sides so the win32 case is safe on POSIX CI where
subprocess.CREATE_NO_WINDOW does not exist (the helper monkeypatches sys.platform
globally, mirroring how test_pi_backend simulates Windows).
"""
from __future__ import annotations

import subprocess

from common.proc import no_window_kwargs


def test_posix_returns_empty(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    assert no_window_kwargs() == {}


def test_win32_sets_creationflags(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    kw = no_window_kwargs()
    assert set(kw) == {"creationflags"}
    # On real Windows this is CREATE_NO_WINDOW; on POSIX CI both sides are 0.
    assert kw["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
