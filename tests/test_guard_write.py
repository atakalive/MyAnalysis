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
from collections.abc import Callable

import pytest

from common import fs_kind
from llm_bridge import guard_write


def _run(
    monkeypatch, event, *, fragile: bool | Callable[[object], bool]
) -> tuple[int, dict | None]:
    """fragile は bool（全パス同じ）か、パスを受けて bool を返す関数。"""
    monkeypatch.setattr(fs_kind, "is_fragile",
                        fragile if callable(fragile) else (lambda _p: fragile))
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
        "tool_input": {"file_path": "G:/data/ds/analyses/my_analysis/analysis.py"},
    }, fragile=True)
    reason = res["hookSpecificOutput"]["permissionDecisionReason"]
    assert "draft-analysis my_analysis" in reason
    assert "apply-analysis my_analysis" in reason


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


def _python_c_lines(reason: str) -> list[str]:
    """拒否理由の `python -c "<code>"` から <code> を取り出す。"""
    return [ln.split('python -c "', 1)[1].rsplit('"', 1)[0]
            for ln in reason.splitlines() if 'python -c "' in ln]


def _mount_only(path: object) -> bool:
    """データセット側（G:）だけを同期マウント扱いにする（stage は tmp_path でローカル）。"""
    return str(path).startswith("G:")


def _own_stage(monkeypatch):
    from common.paths import AGENT_STAGE_ENV, prepare_agent_stage
    stage = prepare_agent_stage("chat")             # conftest により tmp_path/agent_stage/chat
    monkeypatch.setenv(AGENT_STAGE_ENV, str(stage))
    return stage


def test_guidance_routes_body_through_own_stage(monkeypatch):
    """拒否理由は「このチャットの stage へ Write → src= にファイル名」の 2 手順（Issue #118）。"""
    stage = _own_stage(monkeypatch)
    staged = (stage / "x.py").as_posix()
    rc, res = _run(monkeypatch, {
        "tool_name": "Write",
        "tool_input": {"file_path": "G:/data/ds/_work/code/x.py"},
    }, fragile=_mount_only)
    reason = res["hookSpecificOutput"]["permissionDecisionReason"]
    assert f"1. Write ツールで本文を {staged} に書く" in reason
    assert "save_code('<dataset>', '<label>', src='x.py')" in reason
    assert "save_text('<dataset>', '<relpath>', src='x.py')" in reason
    assert "content)" not in reason              # 本文を引数に埋める旧案内は残さない
    codes = _python_c_lines(reason)
    assert len(codes) == 2
    for code in codes:
        compile(code, "<guidance>", "exec")      # 案内どおりのコードが構文として通る
    # 案内した Write 先は guard 自身が許可する（stage はローカルなので FS 判定で通る）。
    for tool in ("Write", "Edit", "MultiEdit"):
        rc, res = _run(monkeypatch, {
            "tool_name": tool, "tool_input": {"file_path": staged}}, fragile=_mount_only)
        assert rc == 0 and res is None


def test_guidance_stage_name_is_quote_safe(monkeypatch):
    """宛先の名前に ' や空白があっても、案内のコマンドは Python / シェルで壊れない。"""
    stage = _own_stage(monkeypatch)
    rc, res = _run(monkeypatch, {
        "tool_name": "Write",
        "tool_input": {"file_path": "G:/data/ds/_work/code/x's $file.py"},
    }, fragile=_mount_only)
    reason = res["hookSpecificOutput"]["permissionDecisionReason"]
    assert f"{(stage / 'x_s__file.py').as_posix()} に書く" in reason
    for code in _python_c_lines(reason):
        compile(code, "<guidance>", "exec")
        assert not set(code) & set('"$`\\!')     # bash の "..." の中で意味を持つ文字が無い


def test_guidance_without_stage_falls_back_to_content(monkeypatch):
    """チャットの stage が無ければ stage を案内しない（従来の content= と長さの目安）。"""
    rc, res = _run(monkeypatch, {
        "tool_name": "Write",
        "tool_input": {"file_path": "G:/data/ds/_work/code/x.py"},
    }, fragile=True)
    reason = res["hookSpecificOutput"]["permissionDecisionReason"]
    assert "src=" not in reason
    assert "save_code('<dataset>', '<label>', content)" in reason
    assert "7,000 文字未満" in reason


def test_force_fragile_drops_stage_guidance(monkeypatch):
    """repo も fragile と判定される環境（MYANALYSIS_FORCE_FRAGILE）では stage を案内しない。

    guard は stage を特別扱いしないので、そのとき stage への Write も拒否される。案内しない先を
    拒否するだけで、案内と拒否は食い違わない。
    """
    from common import fs_kind
    real_is_fragile = fs_kind.is_fragile
    stage = _own_stage(monkeypatch)
    monkeypatch.setenv("MYANALYSIS_FORCE_FRAGILE", "1")
    rc, res = _run(monkeypatch, {
        "tool_name": "Write",
        "tool_input": {"file_path": "G:/data/ds/_work/code/x.py"},
    }, fragile=real_is_fragile)
    reason = res["hookSpecificOutput"]["permissionDecisionReason"]
    assert "src=" not in reason
    assert "save_code('<dataset>', '<label>', content)" in reason
    rc, res = _run(monkeypatch, {
        "tool_name": "Write", "tool_input": {"file_path": str(stage / "x.py")},
    }, fragile=real_is_fragile)
    assert res["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("resolve_fails", [False, True], ids=["link", "link_unresolvable"])
def test_stage_root_through_link_is_not_advertised(monkeypatch, tmp_path, resolve_fails):
    """root が（同期マウントかもしれない）リンクを経ていれば、stage を案内しない。

    リンク先で Path.resolve() が失敗する場合（WinFsp の WinError 1005）も同じ（字句に倒さない）。
    """
    import os
    from pathlib import Path
    import common.paths as cp
    elsewhere = tmp_path / "mount"
    elsewhere.mkdir()
    root = tmp_path / "linked_root"
    try:
        os.symlink(elsewhere, root, target_is_directory=True)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"symlink unavailable: {e}")
    (elsewhere / "chat").mkdir()
    monkeypatch.setattr(cp, "agent_stage_dir", lambda: root)
    monkeypatch.setenv(cp.AGENT_STAGE_ENV, str(root / "chat"))
    if resolve_fails:
        real = Path.resolve

        def resolve(self, *a, **kw):
            if str(self).startswith(str(root)):
                raise OSError(1005, "unrecognized file system")
            return real(self, *a, **kw)
        monkeypatch.setattr(Path, "resolve", resolve)
    rc, res = _run(monkeypatch, {
        "tool_name": "Write", "tool_input": {"file_path": str(root / "chat" / "x.py")},
    }, fragile=True)
    hso = res["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "src=" not in hso["permissionDecisionReason"]


def test_allows_editing_the_draft(monkeypatch):
    """draft は Write/Edit させる（MOUNT_SAFE_EDITS がそう案内している）。

    ここを拒否していたため、宣伝している draft→apply ループが機械的に成立して
    いなかった。apply-analysis が strip / ast.parse / build_tab の 3 ゲートを通すので、
    draft が 0 バイト化しても analysis.py には伝播しない。
    """
    rc, res = _run(monkeypatch, {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": "G:/data/ds/_work/analyses/my_analysis/analysis.draft.py"},
    }, fragile=True)
    assert rc == 0 and res is None       # 出力なし = 許可


def test_still_denies_the_canonical_analysis_py(monkeypatch):
    """draft の許可が本体 analysis.py まで緩めていないこと。"""
    rc, res = _run(monkeypatch, {
        "tool_name": "Edit",
        "tool_input": {"file_path": "G:/data/ds/analyses/my_analysis/analysis.py"},
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
