"""Tests for devtools.migrate_dataset_registry (offline legacy → JSON migration)."""

from __future__ import annotations

import json

import pytest

from devtools import migrate_dataset_registry as mig


LEGACY_ANNOTATED = '''\
"""doc"""
import socket

DATASETS: dict[str, dict[str, str]] = {
    "sample_dataset": {
        "HOST_A": r"C:\\example-data\\sample",
        "HOST_B": "/example-data/sample",
    },
}


def after():
    return DATASETS
'''

LEGACY_PLAIN = '''\
DATASETS = {"sample_dataset": {"HOST_A": "/example-data/sample"}}
'''

EXPECTED_ANNOTATED = {
    "sample_dataset": {
        "HOST_A": "C:\\example-data\\sample",
        "HOST_B": "/example-data/sample",
    },
}


@pytest.fixture()
def src(tmp_path):
    p = tmp_path / "config.py.legacy"
    p.write_text(LEGACY_ANNOTATED, encoding="utf-8")
    return p


@pytest.fixture()
def out(tmp_path):
    return tmp_path / "datasets.local.json"


def _run(src, out, *extra):
    return mig.main(["--source", str(src), "--output", str(out), *extra])


def _read(out):
    return json.loads(out.read_text(encoding="utf-8-sig"))


# --------------------------------------------------------------------------- #
# 受理される旧形式
# --------------------------------------------------------------------------- #
def test_annotated_assignment(src, out):
    assert _run(src, out) == 0
    assert _read(out) == EXPECTED_ANNOTATED


def test_plain_assignment(tmp_path, out):
    src = tmp_path / "legacy.py"
    src.write_text(LEGACY_PLAIN, encoding="utf-8")
    assert _run(src, out) == 0
    assert _read(out) == {"sample_dataset": {"HOST_A": "/example-data/sample"}}


def test_bom_source_accepted(tmp_path, out):
    src = tmp_path / "legacy.py"
    src.write_text(LEGACY_PLAIN, encoding="utf-8-sig")
    assert _run(src, out) == 0


def test_source_is_never_modified(src, out):
    before = src.read_bytes()
    assert _run(src, out) == 0
    assert src.read_bytes() == before


def test_source_is_not_executed(tmp_path, out):
    """import 文・副作用のある式は実行されない（ast.literal_eval のみ）。"""
    marker = tmp_path / "side_effect.txt"
    src = tmp_path / "legacy.py"
    src.write_text(
        "import pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text('boom')\n"
        "raise SystemExit('should never run')\n"
        'DATASETS = {"sample_dataset": {"HOST_A": "/example-data/sample"}}\n',
        encoding="utf-8",
    )
    assert _run(src, out) == 0
    assert not marker.exists()
    assert _read(out) == {"sample_dataset": {"HOST_A": "/example-data/sample"}}


def test_comments_and_functions_not_carried_over(src, out):
    assert _run(src, out) == 0
    text = out.read_text(encoding="utf-8")
    assert "def after" not in text and "doc" not in text


# --------------------------------------------------------------------------- #
# 変換元の拒否条件
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("body", [
    "X = 1\n",                                          # 代入なし
    'DATASETS = {"a": {"H": "/p"}}\nDATASETS = {}\n',   # 複数
    'DATASETS = OTHER = {"a": {"H": "/p"}}\n',          # 複数ターゲット
    'DATASETS = {"a": {"H": some_call()}}\n',           # 動的式
    "DATASETS = [1, 2, 3]\n",                           # 型不正
    'DATASETS = {"a": "notdict"}\n',                    # 型不正
    "DATASETS = {1: {}}\n",                             # 外側キーが非 str
    'DATASETS = {"a": {1: "/p"}}\n',                    # 内側キーが非 str
    'DATASETS = {"a": {"H": 2}}\n',                     # path が非 str
    "DATASETS = {\n",                                   # 構文不正
])
def test_rejected_sources(tmp_path, out, body):
    src = tmp_path / "legacy.py"
    src.write_text(body, encoding="utf-8")
    assert _run(src, out) == 1
    assert not out.exists()


def test_missing_source_file(tmp_path, out):
    assert _run(tmp_path / "nope.py", out) == 1
    assert not out.exists()


def test_missing_output_directory(src, tmp_path):
    assert _run(src, tmp_path / "nodir" / "reg.json") == 1


# --------------------------------------------------------------------------- #
# 出力先の状態別の振る舞い
# --------------------------------------------------------------------------- #
def test_same_content_is_noop(src, out):
    assert _run(src, out) == 0
    before = out.read_bytes()
    assert _run(src, out) == 0          # 2 回目 = 同内容 no-op
    assert out.read_bytes() == before


def test_different_content_is_refused(src, out):
    out.write_text(json.dumps({"other": {"H": "/p"}}) + "\n", encoding="utf-8")
    before = out.read_bytes()
    assert _run(src, out) == 1
    assert out.read_bytes() == before


def test_existing_empty_registry_is_not_overwritten(src, out):
    """既存の `{}`（存在する空登録簿）× 非空の変換元 → 拒否（未作成と混同しない）。"""
    out.write_text("{}\n", encoding="utf-8")
    before = out.read_bytes()
    assert _run(src, out) == 1
    assert out.read_bytes() == before
    # dry-run も同じ判定
    assert _run(src, out, "--dry-run") == 1
    assert out.read_bytes() == before


def test_absent_output_with_empty_source_is_written(tmp_path, out):
    src = tmp_path / "legacy.py"
    src.write_text("DATASETS = {}\n", encoding="utf-8")
    assert _run(src, out) == 0
    assert out.exists()
    assert _read(out) == {}


def test_corrupt_output_is_not_treated_as_absent(src, out):
    out.write_text("{broken", encoding="utf-8")
    assert _run(src, out) == 1
    assert out.read_text(encoding="utf-8") == "{broken"


# --------------------------------------------------------------------------- #
# dry-run
# --------------------------------------------------------------------------- #
def test_dry_run_does_not_create_output(src, out):
    assert _run(src, out, "--dry-run") == 0
    assert not out.exists()


def test_dry_run_then_real_run(src, out):
    assert _run(src, out, "--dry-run") == 0
    assert _run(src, out) == 0
    assert _read(out) == EXPECTED_ANNOTATED


def test_dry_run_same_content_is_noop(src, out):
    assert _run(src, out) == 0
    before = out.read_bytes()
    assert _run(src, out, "--dry-run") == 0
    assert out.read_bytes() == before


def test_dry_run_runs_the_same_encode_validation(tmp_path, out):
    """孤立サロゲートは dry-run でも通らない（終了 1・本体未作成）。"""
    src = tmp_path / "legacy.py"
    src.write_text(
        'DATASETS = {"sample_dataset": {"HOST_A": "\\ud800"}}\n', encoding="utf-8"
    )
    assert _run(src, out, "--dry-run") == 1
    assert not out.exists()
    assert _run(src, out) == 1
    assert not out.exists()


# --------------------------------------------------------------------------- #
# CLI インターフェース
# --------------------------------------------------------------------------- #
def test_missing_source_argument_is_systemexit_2():
    with pytest.raises(SystemExit) as ei:
        mig.main([])
    assert ei.value.code == 2


def test_unknown_argument_is_systemexit_2(src, out):
    with pytest.raises(SystemExit) as ei:
        mig.main(["--source", str(src), "--force"])
    assert ei.value.code == 2


def test_default_output_is_registry_path(src):
    """--output 未指定なら registry_path()（conftest で tmp へ差し替え済み）。"""
    import dataset_registry

    assert mig.main(["--source", str(src)]) == 0
    assert _read(dataset_registry.registry_path()) == EXPECTED_ANNOTATED


def test_stdout_does_not_list_registrations(src, out, capsys):
    assert _run(src, out) == 0
    captured = capsys.readouterr().out
    assert "sample_dataset" not in captured
    assert "HOST_A" not in captured
    assert "1 dataset" in captured


def test_does_not_import_config_or_sync():
    import ast

    from common.paths import repo_root

    src = (repo_root() / "devtools" / "migrate_dataset_registry.py").read_text(
        encoding="utf-8"
    )
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    assert not {"config", "config_share", "gui"} & {n.split(".")[0] for n in names}
