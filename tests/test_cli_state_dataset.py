"""Issue #37: CLI `state` reads via active.json's two dataset fields.

no-name `state` uses `active_analysis_dataset` (the active *analysis* tab's
dataset), so it stays correct when `current_dataset` (sticky) has moved on, and
returns {} when the active tab is a figure viewer.
"""

from __future__ import annotations

import json

import pytest

import llm_bridge.__main__ as bridge_main


@pytest.fixture()
def active_json(monkeypatch, tmp_path):
    p = tmp_path / "active.json"
    monkeypatch.setattr("llm_bridge.__main__.active_state_path", lambda: p)
    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)
    return p


def _write_state(tmp_path, ds, name, payload):
    d = tmp_path / ds / "_work" / "analyses" / name / "state"
    d.mkdir(parents=True)
    (d / "current.json").write_text(json.dumps(payload), encoding="utf-8")


def test_noname_state_uses_active_analysis_dataset(active_json, tmp_path, capsys):
    _write_state(tmp_path, "dsA", "a", {"from": "dsA"})
    active_json.write_text(
        json.dumps(
            {"active_tab": "a", "dataset": "dsB", "active_analysis_dataset": "dsA"}
        ),
        encoding="utf-8",
    )
    assert bridge_main.main(["state"]) == 0
    assert json.loads(capsys.readouterr().out) == {"from": "dsA"}


def test_noname_state_figure_viewer_returns_empty(active_json, tmp_path, capsys):
    # current_dataset=dsA, but active tab is a figure viewer → analysis ds is None.
    _write_state(tmp_path, "dsA", "viewer", {"should": "not be read"})
    active_json.write_text(
        json.dumps(
            {"active_tab": "viewer", "dataset": "dsA", "active_analysis_dataset": None}
        ),
        encoding="utf-8",
    )
    assert bridge_main.main(["state"]) == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_named_state_dataset_flag(active_json, tmp_path, capsys):
    _write_state(tmp_path, "dsA", "a", {"v": 1})
    (tmp_path / "dsA" / "analyses" / "a").mkdir(parents=True)
    (tmp_path / "dsA" / "analyses" / "a" / "analysis.py").write_text("", encoding="utf-8")
    assert bridge_main.main(["state", "a", "--dataset", "dsA"]) == 0
    assert json.loads(capsys.readouterr().out) == {"v": 1}


def test_named_state_no_dataset_errors(active_json, capsys):
    active_json.write_text(json.dumps({}), encoding="utf-8")
    with pytest.raises(SystemExit):
        bridge_main.main(["state", "a"])
