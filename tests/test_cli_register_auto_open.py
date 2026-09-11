"""CLI tests for register-dataset auto-open (commands.submit/wait_for mocked)."""
import socket
import sys
from pathlib import Path
from unittest.mock import patch


def _run_register(args_list, submit_result=None, wait_result=None):
    """Run register-dataset via main() with monkeypatched commands."""
    from llm_bridge.__main__ import main
    from llm_bridge import commands

    submitted = []

    def fake_submit(tier, target, verb, kwargs):
        submitted.append((tier, target, verb, kwargs))
        return "cmd-123"

    def fake_wait(cmd_id, timeout=None):
        return wait_result

    with patch.object(commands, "submit", fake_submit), \
         patch.object(commands, "wait_for", fake_wait), \
         patch("config.register_dataset", return_value={"name": "test", "host": "H", "path": "/p", "created": True}), \
         patch.object(sys, "argv", ["llm_bridge"] + args_list):
        rc = main()
    return rc, submitted


def test_register_auto_open(tmp_path):
    """GUI 起動中: register → open-dataset が submit される。"""
    d = tmp_path / "data"
    d.mkdir()
    rc, submitted = _run_register(
        ["register-dataset", "test", str(d)],
        wait_result={"status": "ok", "result": "no-session:test"},
    )
    assert rc == 0
    assert len(submitted) == 1
    assert submitted[0][2] == "open-dataset"


def test_register_no_open_flag(tmp_path):
    """--no-open: submit されない。"""
    d = tmp_path / "data"
    d.mkdir()
    rc, submitted = _run_register(
        ["register-dataset", "test", str(d), "--no-open"],
    )
    assert rc == 0
    assert submitted == []


def test_register_different_host_no_open():
    """ホスト不一致: submit されない（exit 0 維持）。"""
    rc, submitted = _run_register(
        ["register-dataset", "test", "/some/path", "--host", "OTHERHOST"],
    )
    assert rc == 0
    assert submitted == []


def test_register_open_error_result(tmp_path):
    """status ok + result error: → 登録成功 exit 0。"""
    d = tmp_path / "data"
    d.mkdir()
    rc, submitted = _run_register(
        ["register-dataset", "test", str(d)],
        wait_result={"status": "ok", "result": "error:test"},
    )
    assert rc == 0
    assert len(submitted) == 1


def test_register_open_stale_status(tmp_path):
    """status stale → 登録成功 exit 0、open 失敗扱い。"""
    d = tmp_path / "data"
    d.mkdir()
    rc, submitted = _run_register(
        ["register-dataset", "test", str(d)],
        wait_result={"status": "stale"},
    )
    assert rc == 0
    assert len(submitted) == 1


def test_register_rejects_with_analysis(tmp_path):
    """--with-analysis は撤去済み: argparse が未知引数として SystemExit する。"""
    import pytest
    d = tmp_path / "data"
    d.mkdir()
    with pytest.raises(SystemExit):
        _run_register(["register-dataset", "test", str(d), "--with-analysis", "x"])


# --------------------------------------------------------------------------- #
# 実 register → JSON → reload → list-datasets の結合（Issue #95）
# --------------------------------------------------------------------------- #
def _run_real(argv, tmp_path, monkeypatch, capsys, wait_result=None):
    """register をモックせず main(argv) を通す。GUI 通知と同期だけ stub 化。"""
    import json

    import dataset_registry
    from llm_bridge import commands
    from llm_bridge.__main__ import main

    reg = tmp_path / "datasets.local.json"
    monkeypatch.setattr(dataset_registry, "registry_path", lambda: reg)

    submitted = []
    monkeypatch.setattr(
        commands, "submit",
        lambda tier, target, verb, kwargs: (submitted.append(verb) or "cmd-1"),
    )
    monkeypatch.setattr(commands, "wait_for", lambda cmd_id, timeout=None: wait_result)

    import config_share
    monkeypatch.setattr(config_share, "try_sync", lambda *a, **k: None)

    monkeypatch.setattr(sys, "argv", ["llm_bridge"] + argv)
    rc = main()
    out = capsys.readouterr().out
    data = json.loads(reg.read_text(encoding="utf-8-sig")) if reg.exists() else None
    return rc, submitted, out, data


def test_real_register_writes_json_and_lists(tmp_path, monkeypatch, capsys):
    import config

    d = tmp_path / "sample_dataset"
    d.mkdir()
    rc, submitted, _out, data = _run_real(
        ["register-dataset", "sample_dataset", str(d), "--no-open"],
        tmp_path, monkeypatch, capsys,
    )
    assert rc == 0
    assert submitted == []
    host = socket.gethostname().upper()
    assert data == {"sample_dataset": {host: str(d)}}
    # register 後の reload_datasets() でメモリにも載る。
    assert config.DATASETS["sample_dataset"][host] == str(d)

    rc2, _s, out2, _d = _run_real(
        ["list-datasets"], tmp_path, monkeypatch, capsys,
    )
    assert rc2 == 0


def test_real_register_auto_open_submits(tmp_path, monkeypatch, capsys):
    d = tmp_path / "sample_dataset"
    d.mkdir()
    rc, submitted, _out, data = _run_real(
        ["register-dataset", "sample_dataset", str(d)],
        tmp_path, monkeypatch, capsys,
        wait_result={"status": "ok", "result": "no-session:sample_dataset"},
    )
    assert rc == 0
    assert submitted == ["open-dataset"]
    assert "sample_dataset" in data


def test_real_register_other_host_does_not_open(tmp_path, monkeypatch, capsys):
    rc, submitted, _out, data = _run_real(
        ["register-dataset", "sample_dataset", "/example-data/sample",
         "--host", "OTHERHOST"],
        tmp_path, monkeypatch, capsys,
    )
    assert rc == 0
    assert submitted == []
    assert data == {"sample_dataset": {"OTHERHOST": "/example-data/sample"}}


def test_real_register_rejects_relative_path(tmp_path, monkeypatch, capsys):
    rc, submitted, _out, data = _run_real(
        ["register-dataset", "sample_dataset", "relative/path", "--no-open"],
        tmp_path, monkeypatch, capsys,
    )
    assert rc == 1
    assert submitted == []
    assert data is None
