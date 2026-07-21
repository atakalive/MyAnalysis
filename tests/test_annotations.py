"""Tests for llm_bridge.annotations core IO — non-destructive on the synced mount.

A transient/unreadable annotations.json (0-byte / 未同期 on rclone/WinFsp) must
never be overwritten with an empty file (that would erase hand-placed markers /
notes), and a lost primary must recover from its durable `.bak`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import dataset_config
from llm_bridge import annotations


@pytest.fixture()
def ann_env(monkeypatch, tmp_path):
    """state_dir('ds','a1') → a single tmp dir where annotations.json lives."""
    sd = tmp_path / "state"
    sd.mkdir()
    monkeypatch.setattr(dataset_config, "state_dir",
                        lambda dataset, name, create=True: sd)
    return sd


def _p(sd: Path) -> Path:
    return sd / "annotations.json"


def _blind_reads(monkeypatch, sd: Path):
    """Make annotations.json + .bak raise OSError on read (simulate mount 1005)."""
    real = Path.read_text
    targets = {str(_p(sd)), str(_p(sd)) + ".bak"}

    def blind(self, *a, **k):
        if str(self) in targets:
            raise OSError("mount 1005")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", blind)


def test_submit_then_read_roundtrip(ann_env):
    annotations.submit("ds", "a1", "marker", x=1)
    data = annotations.read("ds", "a1")
    assert data["markers"] == [{"x": 1}]
    assert data["notes"] == []


def test_submit_writes_bak(ann_env):
    annotations.submit("ds", "a1", "note", text="hi")
    assert (ann_env / "annotations.json.bak").is_file()


def test_submit_does_not_wipe_on_unreadable(ann_env, monkeypatch):
    annotations.submit("ds", "a1", "marker", x=1)
    annotations.submit("ds", "a1", "note", text="keep")
    saved = _p(ann_env).read_bytes()               # read_bytes は patch されない
    _blind_reads(monkeypatch, ann_env)
    annotations.submit("ds", "a1", "marker", x=2)   # unreadable → mutation 中止
    assert _p(ann_env).read_bytes() == saved         # 既存 markers/notes 無傷


def test_read_recovers_from_bak_when_primary_zeroed(ann_env):
    annotations.submit("ds", "a1", "marker", x=1)
    _p(ann_env).write_bytes(b"")                     # primary evicted → 0-byte
    data = annotations.read("ds", "a1")
    assert data["markers"] == [{"x": 1}]


def test_clear_marker_keeps_notes(ann_env):
    annotations.submit("ds", "a1", "marker", x=1)
    annotations.submit("ds", "a1", "note", text="n")
    annotations.clear("ds", "a1", "marker")
    data = annotations.read("ds", "a1")
    assert data["markers"] == []
    assert data["notes"] == [{"text": "n"}]


def test_clear_all_writes_empty(ann_env):
    annotations.submit("ds", "a1", "marker", x=1)
    annotations.clear("ds", "a1", None)              # 意図的な全消去は許可
    assert annotations.read("ds", "a1") == {"markers": [], "notes": []}


def test_clear_marker_does_not_wipe_notes_on_unreadable(ann_env, monkeypatch):
    annotations.submit("ds", "a1", "note", text="keep")
    saved = _p(ann_env).read_bytes()
    _blind_reads(monkeypatch, ann_env)
    annotations.clear("ds", "a1", "marker")          # unreadable → 中止（notes を巻き添えにしない）
    assert _p(ann_env).read_bytes() == saved


def test_read_absent_returns_empty(ann_env):
    assert annotations.read("ds", "a1") == {"markers": [], "notes": []}
