"""CLI tests for register-dataset auto-open (commands.submit/wait_for mocked)."""
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
