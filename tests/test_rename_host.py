"""rename-host（Issue #122）: 旧キーの登録を新キーへ移し、旧キーを tombstone つきで消す。

conftest の autouse が登録簿と config_share._state_path を tmp に向け、R2 の自動同期を止めている。
"""

from __future__ import annotations

import json

import pytest

import config
import config_share
import dataset_registry
from llm_bridge.__main__ import main


def _state_bytes():
    path = config_share._state_path()
    return path.read_bytes() if path.exists() else None


def test_moved():
    config.write_registry({"ds": {"OLD": "/p"}})
    res = config.rename_host("old", "new")
    assert res["moved"] == ["ds"]
    assert res["removed"] == [] and res["conflicts"] == []
    assert config.read_registry() == {"ds": {"NEW": "/p"}}
    state = config_share._local_state()
    assert "ds/NEW" in state["entry_meta"]
    assert "ds/OLD" in state["deleted"]


def test_removed_and_conflicts():
    config.write_registry({
        "a": {"OLD": "/p", "NEW": "/p"},
        "b": {"OLD": "/x", "NEW": "/y"},
        "c": {"OTHER": "/z"},
    })
    res = config.rename_host("OLD", "NEW")
    assert res["moved"] == []
    assert res["removed"] == ["a"]
    assert res["conflicts"] == [{"dataset": "b", "old_path": "/x", "new_path": "/y"}]
    reg = config.read_registry()
    assert reg["a"] == {"NEW": "/p"}
    assert reg["b"] == {"OLD": "/x", "NEW": "/y"}
    assert reg["c"] == {"OTHER": "/z"}
    deleted = config_share._local_state()["deleted"]
    assert "a/OLD" in deleted
    assert not any(k.startswith("b/") for k in deleted)


def test_dry_run_writes_nothing():
    config.write_registry({"ds": {"OLD": "/p"}})
    reg_path = dataset_registry.registry_path()
    before = reg_path.read_bytes()
    state_before = _state_bytes()
    res = config.rename_host("OLD", "NEW", dry_run=True)
    assert res["dry_run"] is True
    assert res["moved"] == ["ds"]
    assert reg_path.read_bytes() == before
    assert _state_bytes() == state_before


@pytest.mark.parametrize("old,new", [("bad name", "NEW"), ("OLD", "bad/name"), ("same", "SAME")])
def test_invalid(old, new):
    with pytest.raises(ValueError):
        config.rename_host(old, new)


def test_cli_rename_host(capsys):
    config.write_registry({"ds": {"OLD": "/p"}, "x": {"OLD": "/a", "NEW": "/b"}})
    rc = main(["rename-host", "old", "--to", "new"])
    out = capsys.readouterr()
    assert rc == 0
    data = json.loads(out.out)
    assert data["old"] == "OLD" and data["new"] == "NEW"
    assert data["moved"] == ["ds"]
    assert data["sync"] is None
    assert "warning: x: NEW already has a different path; OLD kept" in out.err

    assert main(["rename-host", "a", "--to", "A"]) == 1
    assert "error:" in capsys.readouterr().err


def test_cli_host(capsys):
    from common.host_key import host_key
    assert main(["host"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {"host": host_key(), "aliases": []}
