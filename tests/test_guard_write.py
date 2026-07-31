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


def test_non_py_write_is_pointed_at_save_text(monkeypatch):
    """`.py` 以外の宛先には save_text を案内する。

    以前は save_code（.py 強制）しか案内しておらず、エージェントは拒否されたあと
    Markdown を書く正規手段を持たなかった。
    """
    rc, res = _run(monkeypatch, {
        "tool_name": "Write",
        "tool_input": {"file_path": r"G:\data\ds\_work\summary.md"},
    }, fragile=True)
    assert res["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "save_text" in res["hookSpecificOutput"]["permissionDecisionReason"]


def test_allows_editing_the_draft(monkeypatch):
    """draft は Write/Edit させる（MOUNT_SAFE_EDITS がそう案内している）。

    ここを拒否していたため、宣伝している draft→apply ループが機械的に成立して
    いなかった。apply-analysis が strip / ast.parse / build_tab の 3 ゲートを通すので、
    draft が 0 バイト化しても analysis.py には伝播しない。
    """
    rc, res = _run(monkeypatch, {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": r"G:\data\ds\_work\analyses\cov3\analysis.draft.py"},
    }, fragile=True)
    assert rc == 0 and res is None       # 出力なし = 許可


def test_still_denies_the_canonical_analysis_py(monkeypatch):
    """draft の許可が本体 analysis.py まで緩めていないこと。"""
    rc, res = _run(monkeypatch, {
        "tool_name": "Edit",
        "tool_input": {"file_path": r"G:\data\ds\analyses\cov3\analysis.py"},
    }, fragile=True)
    assert res["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_decision_json_is_pure_ascii(monkeypatch):
    """stdout は ASCII だけで構成すること（deny を落とさないため）。

    ensure_ascii=False だと非 ASCII が stdout のロケール既定で出るので、日本語
    Windows では cp932 バイト列になり、UTF-8 で読む消費側が JSON をパースできず
    **deny が失われて fail-open する**。実測でこの経路に乗っていた。
    案内文を英語にしても救われない — 拒否理由には対象パスを埋め込むので、
    G:\\測定\\... のような日本語パスだけで同じ事故になる。
    """
    rc, res = _run(monkeypatch, {
        "tool_name": "Write",
        "tool_input": {"file_path": "G:\\測定\\ds\\_work\\summary.md"},
    }, fragile=True)
    raw = json.dumps(res, ensure_ascii=False)   # _run が復元した dict
    assert res["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "測定" in res["hookSpecificOutput"]["permissionDecisionReason"], raw
    # 実際に guard_write が書いたバイト列が ASCII のみであることを直接見る。
    out = io.StringIO()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"tool_input": {"file_path": "G:\\測定\\ds\\_work\\summary.md"}})))
    monkeypatch.setattr("sys.stdout", out)
    guard_write.main()
    emitted = out.getvalue()
    assert emitted.isascii(), f"non-ASCII in hook stdout: {emitted[:200]!r}"
    # どのエンコーディングで往復しても同じ JSON に戻る。
    for enc in ("utf-8", "cp932", "latin-1"):
        assert json.loads(emitted.encode(enc).decode(enc)) == json.loads(emitted)


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
