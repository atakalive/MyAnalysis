"""llm_bridge.doctor と common.rclone_paths（Issue #102）。

実際の登録簿・data/llm_state には触れない（config と global_state_dir を差し替える）。
"""
from __future__ import annotations

import ast
import io
import sys
from pathlib import Path

import pytest

import config
import llm_bridge.paths as lb_paths
from common import rclone_paths
from common.rclone_paths import ENV_CACHE, ENV_LOG, resolve
from llm_bridge import doctor

_REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "ds1"
    root.mkdir()
    datasets = {"ds1": root, "ds_other": RuntimeError("no path for this host")}
    monkeypatch.setattr(config, "DATASETS", {k: {} for k in datasets})

    def fake_get(name):
        v = datasets.get(name, KeyError(f"Unknown dataset {name!r}"))
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setattr(config, "get_dataset_dir", fake_get)
    state = tmp_path / "llm_state"
    state.mkdir()
    monkeypatch.setattr(lb_paths, "global_state_dir", lambda: state)
    monkeypatch.setattr(doctor, "check_toml", lambda name: None)
    monkeypatch.delenv(rclone_paths.ENV_CACHE, raising=False)
    monkeypatch.delenv(rclone_paths.ENV_LOG, raising=False)
    return root, datasets, tmp_path


# --------------------------------------------------------------------------- #
# common.rclone_paths.resolve
# --------------------------------------------------------------------------- #
def test_resolve_arg_wins_over_env(monkeypatch):
    monkeypatch.delenv(ENV_CACHE, raising=False)
    monkeypatch.setenv(ENV_CACHE, "y")
    assert resolve("x", ENV_CACHE) == Path("x")


def test_resolve_falls_back_to_env(monkeypatch):
    monkeypatch.delenv(ENV_CACHE, raising=False)
    monkeypatch.setenv(ENV_CACHE, "y")
    assert resolve(None, ENV_CACHE) == Path("y")
    assert resolve("", ENV_CACHE) == Path("y")


def test_resolve_unspecified(monkeypatch):
    monkeypatch.delenv(ENV_CACHE, raising=False)
    assert resolve(None, ENV_CACHE) is None
    assert resolve("   ", ENV_CACHE) is None
    assert resolve(Path(""), ENV_CACHE) is None
    monkeypatch.setenv(ENV_CACHE, "   ")
    assert resolve(None, ENV_CACHE) is None


# --------------------------------------------------------------------------- #
# doctor.run
# --------------------------------------------------------------------------- #
def test_rclone_skipped_when_unspecified(env, capsys):
    assert doctor.run() == 0
    out = capsys.readouterr().out
    assert "キャッシュ: 未指定のためスキップ" in out
    assert "ログ: 未指定のためスキップ" in out
    assert "孤児 tmp: なし" not in out
    assert "ログに失敗イベントなし" not in out


def test_rclone_paths_from_env(env, monkeypatch, capsys):
    _root, _ds, tmp = env
    cache = tmp / "cache"
    cache.mkdir()
    log = tmp / "rclone.log"
    log.write_bytes(b"")
    monkeypatch.setenv(ENV_CACHE, str(cache))
    monkeypatch.setenv(ENV_LOG, str(log))
    assert doctor.run() == 0
    out = capsys.readouterr().out
    assert "孤児 tmp: なし" in out
    assert "ログに失敗イベントなし" in out


def test_missing_rclone_paths_reported(env, capsys):
    _root, _ds, tmp = env
    assert doctor.run(cache=tmp / "nocache", log=tmp / "no.log") == 1
    out = capsys.readouterr().out
    assert "! キャッシュが見つからない" in out
    assert "! ログが見つからない" in out
    for s in ("孤児 tmp: なし", "ログに失敗イベントなし", "doctor --repair", "doctor --rescue"):
        assert s not in out


def test_unknown_dataset_exit2(env, capsys):
    assert doctor.run("nope") == 2
    cap = capsys.readouterr()
    assert "'nope'" in cap.err
    assert cap.out == ""


def test_other_host_dataset_exit2(env, capsys):
    assert doctor.run("ds_other") == 2
    assert capsys.readouterr().out == ""


def test_invisible_dataset_dir_counted(env, capsys):
    root, _ds, _tmp = env
    root.rmdir()
    assert doctor.run() == 1
    assert "フォルダが見えない" in capsys.readouterr().out


def test_empty_ok_files_not_reported(env):
    root, _ds, _tmp = env
    (root / "pkg").mkdir()
    for rel in ("pkg/__init__.py", "py.typed", ".gitkeep", "a.lock"):
        (root / rel).write_bytes(b"")
    assert doctor.find_zero_byte_files(root) == []
    assert doctor.run() == 0


def test_zero_byte_reported_without_cache(env, capsys):
    root, _ds, _tmp = env
    (root / "x.csv").write_bytes(b"")
    assert doctor.run() == 1
    out = capsys.readouterr().out
    assert "x.csv" in out
    assert "復旧候補は探していない" in out
    assert "doctor --rescue --cache <rclone のキャッシュ>" in out


def test_rescue_requires_cache(env, capsys):
    _root, _ds, tmp = env
    assert doctor.run(rescue=True) == 2
    assert capsys.readouterr().out == ""
    assert doctor.run(rescue=True, cache=tmp / "nocache") == 2
    assert capsys.readouterr().out == ""


def test_output_cp932_strict(env, monkeypatch):
    root, _ds, tmp = env
    (root / "x.csv").write_bytes(b"")
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp932", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    assert doctor.run(cache=tmp / "nocache", log=tmp / "no.log") == 1
    stream.flush()


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                ids.add(id(body[0].value))
    return ids


def test_doctor_literals_encodable_cp932():
    src = (_REPO / "llm_bridge" / "doctor.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    skip = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            node.value.encode("cp932")   # UnicodeEncodeError なら失敗
