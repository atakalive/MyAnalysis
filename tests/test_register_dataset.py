"""Tests for config.register_dataset — pure file-writer registration.

A temporary registry JSON is written to tmp_path and passed via config_path=.
The real repo datasets.local.json is never touched (conftest also redirects the
default path). The in-memory config.DATASETS global must remain unchanged
(memory reflection is the GUI's responsibility, not register_dataset's).
"""

from __future__ import annotations

import json
import sys

import pytest

import config


_SEED = {
    "sample_dataset": {
        "HOST_A": "C:/example-data/sample",
        "HOST_B": "/example-data/sample",
    },
}


def _datasets_from(path):
    """Read the JSON registry back (BOM tolerant, like read_registry)."""
    return json.loads(path.read_text(encoding="utf-8-sig"))


@pytest.fixture()
def cfg(tmp_path):
    p = tmp_path / "datasets.local.json"
    p.write_text(
        json.dumps(_SEED, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    return p


def test_new_dataset_created(cfg):
    result = config.register_dataset(
        "new_dataset", r"G:\new\path", host="host_a", config_path=cfg
    )
    assert result["created"] is True
    assert result["host"] == "HOST_A"
    data = _datasets_from(cfg)
    assert data["new_dataset"] == {"HOST_A": r"G:\new\path"}


def test_merge_host_into_existing(cfg):
    result = config.register_dataset(
        "sample_dataset", r"J:\foo", host="newhost", config_path=cfg
    )
    assert result["created"] is False
    data = _datasets_from(cfg)
    assert data["sample_dataset"]["NEWHOST"] == r"J:\foo"
    assert "HOST_A" in data["sample_dataset"]


def test_overwrite_existing_host_path(cfg):
    result = config.register_dataset(
        "sample_dataset", r"Z:\override", host="HOST_A", config_path=cfg
    )
    assert result["created"] is False
    data = _datasets_from(cfg)
    assert data["sample_dataset"]["HOST_A"] == r"Z:\override"


def test_host_uppercased(cfg):
    result = config.register_dataset(
        "foo", r"G:\foo", host="lowerhost", config_path=cfg
    )
    assert result["host"] == "LOWERHOST"


def test_backslash_path_roundtrips(cfg):
    config.register_dataset("foo", r"G:\a\b\c", host="H1", config_path=cfg)
    # JSON escapes backslashes; the value must survive the roundtrip unchanged.
    assert r'"G:\\a\\b\\c"' in cfg.read_text(encoding="utf-8")
    data = _datasets_from(cfg)
    assert data["foo"]["H1"] == r"G:\a\b\c"


def test_trailing_backslash_path_roundtrips(cfg):
    p = "G:\\a\\b\\"
    config.register_dataset("foo", p, host="H1", config_path=cfg)
    data = _datasets_from(cfg)
    assert data["foo"]["H1"] == p


def test_quote_in_path_roundtrips(cfg):
    # 末尾でない位置に " を含む絶対パス。
    p = '/a/b"c/d'
    config.register_dataset("foo", p, host="H1", config_path=cfg)
    data = _datasets_from(cfg)
    assert data["foo"]["H1"] == p


def test_control_char_path_roundtrips(cfg):
    p = "/a/b\nc"
    config.register_dataset("foo", p, host="H1", config_path=cfg)
    data = _datasets_from(cfg)
    assert data["foo"]["H1"] == p


def test_existing_entries_preserved(cfg):
    config.register_dataset("foo", r"G:\foo", host="H1", config_path=cfg)
    data = _datasets_from(cfg)
    assert data["sample_dataset"] == _SEED["sample_dataset"]
    assert data["foo"] == {"H1": r"G:\foo"}


def test_invalid_dataset_name(cfg):
    # 大文字/ハイフン等は許可されたので、汎用ハイジーン違反 (空白) で拒否を確認。
    with pytest.raises(ValueError):
        config.register_dataset("bad name", r"G:\foo", host="H1", config_path=cfg)


@pytest.mark.parametrize("host", ['ba"d', "ba\\d", "bad\n", "-lead"])
def test_invalid_hostname(cfg, host):
    with pytest.raises(ValueError):
        config.register_dataset("foo", r"G:\foo", host=host, config_path=cfg)


def test_nonascii_host_accepted(cfg):
    # 非ASCII の PC 名 (例: ドイツ語) も .upper() 後に受理される。
    result = config.register_dataset("foo", r"G:\foo", host="müller-pc", config_path=cfg)
    assert result["host"] == "MÜLLER-PC"
    data = _datasets_from(cfg)
    assert data["foo"]["MÜLLER-PC"] == r"G:\foo"


def test_relative_path_rejected(cfg):
    with pytest.raises(ValueError):
        config.register_dataset("foo", "relative/path", host="H1", config_path=cfg)


def test_windows_path_absolute_on_posix(cfg):
    # WSL/POSIX 上でも G:\... は絶対と判定される (OS 非依存チェック)。
    result = config.register_dataset("foo", r"G:\foo", host="H1", config_path=cfg)
    assert result["created"] is True


def test_non_json_config_path_rejected(tmp_path):
    """旧呼び出し（Python ファイル）は I/O 前に拒否され、ソースを書き換えない。"""
    p = tmp_path / "config.py"
    p.write_text("X = 1\n", encoding="utf-8")
    with pytest.raises(config.RegistryError):
        config.register_dataset("foo", r"G:\foo", host="H1", config_path=p)
    assert p.read_text(encoding="utf-8") == "X = 1\n"


def test_reserved_name_accepted_for_dataset(cfg):
    # データセット名は check_reserved=False なので "con" を受理する。
    result = config.register_dataset("con", r"G:\foo", host="H1", config_path=cfg)
    assert result["created"] is True
    data = _datasets_from(cfg)
    assert data["con"] == {"H1": r"G:\foo"}


def test_output_is_lf_utf8_without_bom(cfg):
    config.register_dataset("foo", r"G:\foo", host="H1", config_path=cfg)
    raw = cfg.read_bytes()
    assert b"\r\n" not in raw
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")


def test_does_not_mutate_global_datasets(cfg):
    before = {k: dict(v) for k, v in config.DATASETS.items()}
    config.register_dataset("brand_new_xyz", r"G:\foo", host="H1", config_path=cfg)
    assert config.DATASETS == before
    assert "brand_new_xyz" not in config.DATASETS


def test_reload_datasets_inplace(tmp_path):
    """reload_datasets preserves dict identity (id(DATASETS) unchanged)."""
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps({"test_ds": {"HOST": "/tmp"}}), encoding="utf-8")
    original_id = id(config.DATASETS)
    original_content = dict(config.DATASETS)
    try:
        config.reload_datasets(config_path=reg)
        assert id(config.DATASETS) == original_id
        assert "test_ds" in config.DATASETS
    finally:
        config.DATASETS.clear()
        config.DATASETS.update(original_content)


def test_reload_datasets_rejects_non_dict(tmp_path):
    """reload_datasets raises RegistryError if the registry is not a dict."""
    reg = tmp_path / "reg.json"
    reg.write_text("[1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(config.RegistryError):
        config.reload_datasets(config_path=reg)


def test_reload_failure_keeps_previous_contents(tmp_path):
    """読み込み失敗時は DATASETS の内容を保持する（read 成功前に clear しない）。"""
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"keep_me": {"H": "/p"}}), encoding="utf-8")
    config.reload_datasets(config_path=good)

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(config.RegistryError):
        config.reload_datasets(config_path=bad)
    assert config.DATASETS == {"keep_me": {"H": "/p"}}


def test_register_creates_registry_when_absent(tmp_path):
    """未作成の登録簿でも最初の登録で作られる。"""
    reg = tmp_path / "datasets.local.json"
    assert not reg.exists()
    config.register_dataset("fresh_ds", "/data/fresh", host="H1", config_path=reg)
    assert _datasets_from(reg) == {"fresh_ds": {"H1": "/data/fresh"}}


def test_default_path_registry_used_when_config_path_is_none(tmp_path):
    """config_path=None は既定 registry_path()（conftest で tmp へ差し替え済み）を使う。"""
    import dataset_registry

    config.register_dataset("default_path_ds", "/data/x", host="H1")
    assert _datasets_from(dataset_registry.registry_path()) == {
        "default_path_ds": {"H1": "/data/x"}
    }


# --------------------------------------------------------------------------- #
# 初回 import の挙動 — subprocess（通常テストの config を壊さないため）
# --------------------------------------------------------------------------- #
_CHILD_PREAMBLE = """
import json, sys
from pathlib import Path
sys.path.insert(0, {repo!r})
import dataset_registry
reg = Path({reg!r})
dataset_registry.registry_path = lambda: reg
"""


def _run_child(tmp_path, reg, body):
    import os
    import subprocess

    from common.paths import repo_root

    script = _CHILD_PREAMBLE.format(repo=str(repo_root()), reg=str(reg)) + body
    env = dict(os.environ)
    env.pop("MYANALYSIS_FORCE_FRAGILE", None)
    env["MYANALYSIS_FS_OVERRIDE"] = f"{tmp_path}=local"
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env,
        cwd=str(tmp_path),
    )


def test_initial_import_reads_existing_registry(tmp_path):
    reg = tmp_path / "datasets.local.json"
    reg.write_text(json.dumps({"seed_ds": {"H1": "/p"}}), encoding="utf-8")
    r = _run_child(tmp_path, reg, """
import config
assert config.DATASETS == {"seed_ds": {"H1": "/p"}}, config.DATASETS
print("OK")
""")
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_initial_import_empty_when_absent(tmp_path):
    reg = tmp_path / "datasets.local.json"
    r = _run_child(tmp_path, reg, """
import config
assert config.DATASETS == {}, config.DATASETS
assert not Path({reg!r}).exists()
print("OK")
""".replace("{reg!r}", repr(str(reg))))
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_initial_import_fails_fast_on_corrupt_registry(tmp_path):
    reg = tmp_path / "datasets.local.json"
    reg.write_text("{broken", encoding="utf-8")
    r = _run_child(tmp_path, reg, """
import config
print("SHOULD NOT REACH")
""")
    assert r.returncode != 0
    assert "RegistryError" in r.stderr


def test_module_reload_keeps_dict_identity(tmp_path):
    reg = tmp_path / "datasets.local.json"
    reg.write_text(json.dumps({"a": {"H": "/p"}}), encoding="utf-8")
    r = _run_child(tmp_path, reg, """
import importlib
import config
d = config.DATASETS
reg.write_text(json.dumps({"b": {"H": "/q"}}), encoding="utf-8")
importlib.reload(config)
assert config.DATASETS is d, "dict identity lost across module reload"
assert d == {"b": {"H": "/q"}}, d
print("OK")
""")
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout
