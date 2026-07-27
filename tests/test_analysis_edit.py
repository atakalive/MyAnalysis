"""Issue #89: mount-safe analysis edit path — draft / apply / recover.

Hermetic: config.get_dataset_dir points at tmp_path, so analyses live at
tmp_path/<ds>/analyses/<name>/ and drafts under tmp_path/<ds>/_work/... .
All verbs are headless (no PySide6); tested by direct function calls.
"""

from __future__ import annotations

import ast

import pytest

from llm_bridge import analysis_edit
from llm_bridge.analysis_edit import _defines_build_tab

DS = "dsX"
NAME = "demo"

_GOOD = (
    "from gui.tab import AnalysisTab\n"
    "def build_tab(parent, data):\n"
    "    return AnalysisTab('demo')\n"
)


@pytest.fixture()
def ds(monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path / name)
    (tmp_path / DS).mkdir()
    return tmp_path


def _write_analysis(tmp_path, source=_GOOD, name=NAME):
    d = tmp_path / DS / "analyses" / name
    d.mkdir(parents=True, exist_ok=True)
    af = d / "analysis.py"
    af.write_text(source, encoding="utf-8")
    return af


def _draft_path(tmp_path, name=NAME):
    return (tmp_path / DS / "_work" / "analyses" / name / "analysis.draft.py")


# --- draft-analysis ---------------------------------------------------------

def test_draft_creates_draft_and_prints_followup(ds, capsys):
    _write_analysis(ds)
    analysis_edit.draft_analysis(DS, NAME)
    out = capsys.readouterr().out
    draft = _draft_path(ds)
    assert draft.is_file()
    assert draft.read_text(encoding="utf-8") == _GOOD
    assert str(draft) in out                       # absolute draft path printed
    # --dataset=<ds> 形 + positional は `--` の後（option 誤認防止・reviewer code P2）
    assert f"apply-analysis --dataset={DS} -- {NAME}" in out


def test_draft_missing_analysis_points_at_newanalysis(ds):
    with pytest.raises(SystemExit) as ei:
        analysis_edit.draft_analysis(DS, NAME)
    assert "newanalysis" in str(ei.value)


def test_draft_empty_analysis_points_at_recover(ds):
    _write_analysis(ds, source="")   # 0-byte
    with pytest.raises(SystemExit) as ei:
        analysis_edit.draft_analysis(DS, NAME)
    assert "recover-analysis" in str(ei.value)


def test_draft_followup_command_is_shell_quoted(ds, capsys):
    """reviewer code P1: 名前にシェルメタ文字が入っても出力コマンドが安全に quote される。

    ';' は validate_identifier_name を通る（禁止文字は <>:"|?* と区切り/制御のみ）が
    shell では危険。follow-up の apply/set-active-dataset/reload 全トークンで確認する。
    """
    import shlex

    evil = "a;b"
    _write_analysis(ds, name=evil)
    analysis_edit.draft_analysis(DS, evil)
    out = capsys.readouterr().out
    # 生の "a;b" が unquoted のまま positional に出ていないこと（回帰で bare に戻ると
    # "apply-analysis a;b " が現れて落ちる）。
    assert f"apply-analysis {evil} " not in out
    # quote 形（'a;b' / 'target=a;b'）で埋め込まれていること。bare 出力ではこの
    # クォート付き部分文字列は現れないので、これが quote 実施の確証になる。
    assert shlex.quote(evil) in out                    # positional
    assert shlex.quote(f"target={evil}") in out        # reload の k=v トークン全体


def test_guidance_safe_for_option_looking_names(ds, capsys):
    """reviewer code P2 R2: 先頭ハイフンの有効名（-x）でも案内コマンドが壊れない。

    validate_identifier_name は先頭ハイフンを拒否しないので `-x` は有効。旧形
    `apply-analysis -x --dataset ds` は argparse が -x を option 誤認して失敗する。
    --dataset=<ds> 形＋positional を `--` の後に置く形へ寄せる。draft の follow-up
    （apply）と、missing→newanalysis / empty→recover 案内の各出力を検証。
    """
    # follow-up (apply) — 既存 analysis から draft を作る
    _write_analysis(ds, name="-x")
    analysis_edit.draft_analysis(DS, "-x")
    out = capsys.readouterr().out
    assert f"apply-analysis --dataset={DS} -- -x" in out
    assert "apply-analysis -x --dataset" not in out    # 旧・壊れる形が無いこと
    # missing analysis → newanalysis 案内
    with pytest.raises(SystemExit) as ei:
        analysis_edit.draft_analysis(DS, "-nope")
    assert f"newanalysis --dataset={DS} -- -nope" in str(ei.value)
    # empty analysis → recover 案内
    _write_analysis(ds, source="", name="-e")
    with pytest.raises(SystemExit) as ei2:
        analysis_edit.draft_analysis(DS, "-e")
    assert f"recover-analysis --dataset={DS} -- -e" in str(ei2.value)


def test_cli_option_looking_name_parses(ds):
    """`--dataset=<ds> -- -x` を CLI が argparse エラーにせず name=-x として受理する。

    draft 不在で verb レベルの "no draft" に到達＝argparse が -x を positional name
    として受理した証拠（option 誤認なら usage エラーで別メッセージになる）。
    """
    _write_analysis(ds, name="-x")                     # analysis はあるが draft 無し
    from llm_bridge.__main__ import main

    with pytest.raises(SystemExit) as ei:
        main(["apply-analysis", f"--dataset={DS}", "--", "-x"])
    assert "no draft" in str(ei.value)


# --- apply-analysis ---------------------------------------------------------

def test_apply_promotes_and_keeps_draft(ds, capsys):
    af = _write_analysis(ds, source="# stale\n")
    draft = _draft_path(ds)
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text(_GOOD, encoding="utf-8")
    analysis_edit.apply_analysis(DS, NAME)
    out = capsys.readouterr().out
    assert af.read_text(encoding="utf-8") == _GOOD
    assert draft.is_file()                          # draft kept for the next iter
    assert f"applied: {af}" in out


def test_apply_no_draft_points_at_draft_analysis(ds):
    _write_analysis(ds)
    with pytest.raises(SystemExit) as ei:
        analysis_edit.apply_analysis(DS, NAME)
    assert "draft-analysis" in str(ei.value)


def test_apply_empty_draft_retries_then_errors(ds):
    af = _write_analysis(ds, source="# original\n")
    draft = _draft_path(ds)
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("   \n", encoding="utf-8")   # whitespace-only → gate rejects
    with pytest.raises(SystemExit) as ei:
        analysis_edit.apply_analysis(DS, NAME)
    assert "read failed after retries" in str(ei.value)
    assert af.read_text(encoding="utf-8") == "# original\n"   # target untouched


def test_apply_non_utf8_draft_errors(ds):
    af = _write_analysis(ds, source="# original\n")
    draft = _draft_path(ds)
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_bytes(b"\xff\xfe build_tab")       # invalid UTF-8
    with pytest.raises(SystemExit) as ei:
        analysis_edit.apply_analysis(DS, NAME)
    assert "read failed after retries" in str(ei.value)
    assert af.read_text(encoding="utf-8") == "# original\n"


def test_apply_syntax_error_draft_errors(ds):
    af = _write_analysis(ds, source="# original\n")
    draft = _draft_path(ds)
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("def build_tab(:\n", encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        analysis_edit.apply_analysis(DS, NAME)
    assert "syntax" in str(ei.value)
    assert af.read_text(encoding="utf-8") == "# original\n"


def test_apply_missing_build_tab_errors(ds):
    af = _write_analysis(ds, source="# original\n")
    draft = _draft_path(ds)
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        analysis_edit.apply_analysis(DS, NAME)
    assert "build_tab" in str(ei.value)
    assert af.read_text(encoding="utf-8") == "# original\n"


def test_apply_accepts_build_tab_assignment_form(ds):
    af = _write_analysis(ds, source="# original\n")
    draft = _draft_path(ds)
    draft.parent.mkdir(parents=True, exist_ok=True)
    src = "def _impl(parent, data):\n    return None\nbuild_tab = _impl\n"
    draft.write_text(src, encoding="utf-8")
    analysis_edit.apply_analysis(DS, NAME)
    assert af.read_text(encoding="utf-8") == src


def test_apply_accepts_build_tab_import_form(ds):
    af = _write_analysis(ds, source="# original\n")
    draft = _draft_path(ds)
    draft.parent.mkdir(parents=True, exist_ok=True)
    src = "from helper import build_tab\n"
    draft.write_text(src, encoding="utf-8")
    analysis_edit.apply_analysis(DS, NAME)
    assert af.read_text(encoding="utf-8") == src


# --- recover-analysis -------------------------------------------------------

def _bak_path(af):
    return af.with_name(af.name + ".bak")


def test_recover_from_zero_byte_removes_stale_draft(ds, capsys):
    af = _write_analysis(ds, source="")            # 0-byte analysis.py
    _bak_path(af).write_text(_GOOD, encoding="utf-8")
    draft = _draft_path(ds)
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("# stale draft\n", encoding="utf-8")
    analysis_edit.recover_analysis(DS, NAME)
    out = capsys.readouterr().out
    assert af.read_text(encoding="utf-8") == _GOOD
    assert not draft.exists()
    assert "recovered:" in out
    assert "stale draft removed" in out


def test_recover_from_absent_no_stale_draft(ds, capsys):
    af = ds / DS / "analyses" / NAME / "analysis.py"
    af.parent.mkdir(parents=True, exist_ok=True)
    _bak_path(af).write_text(_GOOD, encoding="utf-8")
    # analysis.py absent, no draft
    analysis_edit.recover_analysis(DS, NAME)
    out = capsys.readouterr().out
    assert af.read_text(encoding="utf-8") == _GOOD
    assert "recovered:" in out
    assert "no stale draft to remove" in out


def test_recover_refuses_non_empty_analysis(ds):
    af = _write_analysis(ds, source="print('real code')\n")
    _bak_path(af).write_text(_GOOD, encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        analysis_edit.recover_analysis(DS, NAME)
    assert "not empty" in str(ei.value)
    assert af.read_text(encoding="utf-8") == "print('real code')\n"


def test_recover_refuses_whitespace_only_analysis(ds):
    """byte-exact: 空白のみでも 1 バイト中身があれば上書きしない（reviewer P1）。"""
    af = _write_analysis(ds, source="   \n")
    _bak_path(af).write_text(_GOOD, encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        analysis_edit.recover_analysis(DS, NAME)
    assert "not empty" in str(ei.value)
    assert af.read_text(encoding="utf-8") == "   \n"


def test_recover_no_backup_errors(ds):
    _write_analysis(ds, source="")                 # 0-byte, no .bak
    with pytest.raises(SystemExit) as ei:
        analysis_edit.recover_analysis(DS, NAME)
    assert "no usable backup" in str(ei.value)


def test_recover_empty_backup_errors(ds):
    af = _write_analysis(ds, source="")            # 0-byte analysis.py
    _bak_path(af).write_text("", encoding="utf-8")  # 0-byte .bak
    with pytest.raises(SystemExit) as ei:
        analysis_edit.recover_analysis(DS, NAME)
    assert "empty" in str(ei.value)


# --- CLI dataset resolution -------------------------------------------------

def test_cli_unresolved_dataset_errors(monkeypatch, tmp_path):
    """main(["apply-analysis", "foo"]) with no active.json → SystemExit."""
    from llm_bridge import paths as lb_paths

    state_dir = tmp_path / "llm_state"

    def _fake_dir():
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir

    monkeypatch.setattr(lb_paths, "global_state_dir", _fake_dir)
    from llm_bridge.__main__ import main

    with pytest.raises(SystemExit) as ei:
        main(["apply-analysis", "foo"])
    assert "no dataset open" in str(ei.value)


# --- _defines_build_tab unit ------------------------------------------------

@pytest.mark.parametrize("src, expected", [
    ("def build_tab(p, d):\n    pass\n", True),
    ("async def build_tab(p, d):\n    pass\n", True),
    ("build_tab = _impl\n", True),
    ("build_tab: object = None\n", True),
    # annotation-only: no value → only __annotations__, no module attr (reviewer P2)
    ("build_tab: object\n", False),
    ("from m import build_tab\n", True),
    ("import m as build_tab\n", True),
    ("def other(p, d):\n    pass\n", False),
    ("other = 1\n", False),
    ("", False),
])
def test_defines_build_tab(src, expected):
    assert _defines_build_tab(ast.parse(src)) is expected
