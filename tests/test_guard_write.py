"""PreToolUse hook `guard-write`（Issue #96）。

エージェント自身の Write/Edit は我々の書込 chokepoint を通らず、独自の tmp+rename で
マウントを叩くため 22% の確率で 0 バイト化を起こす。プロンプトによる抑止は実測で
守られなかったので機械的に拒否する。

**fail-open が最重要**: ガードのバグでエージェントが完全に止まる方が、たまに 0 バイト化
するより有害。想定外の入力はすべて許可に倒すこと。
"""
from __future__ import annotations

import io
import json

import pytest

from common import fs_kind
from llm_bridge import guard_write


def _run(monkeypatch, event, *, fragile: bool) -> tuple[int, dict | None]:
    monkeypatch.setattr(fs_kind, "is_fragile", lambda _p: fragile)
    monkeypatch.setattr("sys.stdin", io.StringIO(
        event if isinstance(event, str) else json.dumps(event)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    rc = guard_write.main()
    text = out.getvalue().strip()
    return rc, (json.loads(text) if text else None)


def test_denies_write_to_mount(monkeypatch):
    rc, res = _run(monkeypatch, {
        "tool_name": "Write",
        "tool_input": {"file_path": r"G:\data\ds\_work\code\x.py"},
    }, fragile=True)
    assert rc == 0                       # hook は常に 0 で終わる（決定は JSON で返す）
    hso = res["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert "save_code" in hso["permissionDecisionReason"]


def test_analysis_py_gets_draft_apply_guidance(monkeypatch):
    """analysis.py だけは draft→apply の具体コマンドを案内する。"""
    rc, res = _run(monkeypatch, {
        "tool_name": "Edit",
        "tool_input": {"file_path": r"G:\data\ds\analyses\cov3\analysis.py"},
    }, fragile=True)
    reason = res["hookSpecificOutput"]["permissionDecisionReason"]
    assert "draft-analysis cov3" in reason
    assert "apply-analysis cov3" in reason


def test_allows_local_path(monkeypatch):
    rc, res = _run(monkeypatch, {
        "tool_name": "Write",
        "tool_input": {"file_path": r"D:\repo\tool.py"},
    }, fragile=False)
    assert rc == 0 and res is None       # 出力なし = 許可


def test_notebook_path_is_covered(monkeypatch):
    rc, res = _run(monkeypatch, {
        "tool_name": "NotebookEdit",
        "tool_input": {"notebook_path": r"G:\data\ds\_work\nb.ipynb"},
    }, fragile=True)
    assert res["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("event", [
    "",                                    # 空 stdin
    "not json at all",                     # 壊れた JSON
    {"tool_name": "Bash"},                 # tool_input なし
    {"tool_input": {}},                    # パスなし
    {"tool_input": {"file_path": 123}},    # 型違い
])
def test_fail_open_on_bad_input(monkeypatch, event):
    rc, res = _run(monkeypatch, event, fragile=True)
    assert rc == 0 and res is None         # 何があっても許可に倒す


def test_fail_open_when_probe_raises(monkeypatch):
    def boom(_p):
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(fs_kind, "is_fragile", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"tool_input": {"file_path": r"G:\x.py"}})))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert guard_write.main() == 0
    assert out.getvalue().strip() == ""
