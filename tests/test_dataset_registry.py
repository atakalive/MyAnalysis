"""Contract tests for dataset_registry (JSON registry storage, Issue #95).

すべて一時領域に対して行う。既定パスは conftest が tmp へ差し替えているので、
実 `datasets.local.json` には触れない。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading

import pytest

import dataset_registry as dr
from common.paths import repo_root


@pytest.fixture()
def reg(tmp_path):
    return tmp_path / "datasets.local.json"


# --------------------------------------------------------------------------- #
# read / 既定パス
# --------------------------------------------------------------------------- #
def test_import_and_read_write_nothing(tmp_path, reg):
    before = set(os.listdir(tmp_path))
    assert dr.read_registry(reg) == {}
    assert set(os.listdir(tmp_path)) == before
    assert not reg.exists()


def test_missing_returns_fresh_empty_dict(reg):
    a = dr.read_registry(reg)
    b = dr.read_registry(reg)
    assert a == b == {}
    assert a is not b          # 共有された辞書を返さない


def test_default_path_is_repo_root_and_cwd_independent(tmp_path, monkeypatch):
    """既定パスの計算そのものを検証する（conftest の差し替えを受けない私的コピーで）。

    実ファイルの I/O は行わない。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_dataset_registry_pristine", repo_root() / "dataset_registry.py"
    )
    pristine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pristine)

    expected = repo_root() / "datasets.local.json"
    monkeypatch.chdir(tmp_path)
    assert pristine.registry_path() == expected
    monkeypatch.chdir(repo_root())
    assert pristine.registry_path() == expected


def test_explicit_json_path(reg):
    reg.write_text(json.dumps({"a": {"H": "/p"}}), encoding="utf-8")
    assert dr.read_registry(reg) == {"a": {"H": "/p"}}


@pytest.mark.parametrize("name", ["config.py", "legacy.PY", "registry.txt", "noext"])
def test_non_json_path_rejected_before_io(tmp_path, name):
    p = tmp_path / name
    p.write_text("X = 1\n", encoding="utf-8")
    with pytest.raises(dr.RegistryError):
        dr.read_registry(p)
    with pytest.raises(dr.RegistryError):
        dr.write_registry({}, config_path=p)
    with pytest.raises(dr.RegistryError):
        with dr.registry_transaction(config_path=p):
            pass
    # I/O 前に落ちるので中身は不変、ロックファイルも作らない。
    assert p.read_text(encoding="utf-8") == "X = 1\n"
    assert not (tmp_path / f"{name}.lock").exists()


def test_bom_is_accepted(reg):
    reg.write_text(json.dumps({"a": {"H": "/p"}}), encoding="utf-8-sig")
    assert reg.read_bytes().startswith(b"\xef\xbb\xbf")
    assert dr.read_registry(reg) == {"a": {"H": "/p"}}


@pytest.mark.parametrize("raw", ["", "   \n\t ", "{not json", "[1, 2, 3]",
                                 '{"a": "notdict"}', '{"a": {"H": 1}}',
                                 '{"a": {"H": {"x": 1}}}'])
def test_unreadable_or_invalid_is_error_not_empty(reg, raw):
    reg.write_text(raw, encoding="utf-8")
    with pytest.raises(dr.RegistryError):
        dr.read_registry(reg)


def test_invalid_utf8_is_error(reg):
    reg.write_bytes(b'{"a": {"H": "\xff\xfe"}}')
    with pytest.raises(dr.RegistryError):
        dr.read_registry(reg)


def test_read_oserror_becomes_registry_error(reg, monkeypatch):
    reg.write_text("{}", encoding="utf-8")

    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(type(reg), "read_text", boom)
    with pytest.raises(dr.RegistryError):
        dr.read_registry(reg)


def test_error_message_names_the_file_not_the_contents(reg):
    reg.write_text('{"secret_dataset": {"SECRET_HOST": "/secret/path"}', encoding="utf-8")
    with pytest.raises(dr.RegistryError) as ei:
        dr.read_registry(reg)
    msg = str(ei.value)
    assert "datasets.local.json" in msg
    assert "secret_dataset" not in msg and "/secret/path" not in msg


# --------------------------------------------------------------------------- #
# validate_registry
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [
    [1, 2, 3],
    "string",
    {1: {}},                        # 外側 dataset キーが非 str
    {"a": ["not", "dict"]},
    {"a": {1: "/p"}},               # 内側 host キーが非 str
    {"a": {"H": 2}},                # path が非 str
])
def test_validate_registry_rejects(bad):
    with pytest.raises(dr.RegistryError):
        dr.validate_registry(bad)
    with pytest.raises(dr.RegistryError):
        dr.write_registry(bad, config_path=None)


def test_validate_registry_accepts_empty_shapes():
    v = {"a": {}, "b": {"H": "/p"}}
    assert dr.validate_registry(v) is v
    assert dr.validate_registry({}) == {}


# --------------------------------------------------------------------------- #
# write / 形式
# --------------------------------------------------------------------------- #
def test_first_save_creates_file(reg):
    dr.write_registry({"a": {"H": "/p"}}, config_path=reg)
    assert json.loads(reg.read_text(encoding="utf-8")) == {"a": {"H": "/p"}}


def test_output_format_lf_single_trailing_newline_no_bom(reg):
    dr.write_registry({"日本語": {"H": "/p"}}, config_path=reg)
    raw = reg.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    assert "日本語".encode("utf-8") in raw        # ensure_ascii=False


@pytest.mark.parametrize("path_value", [
    r"D:\サンプル\データ\sub",
    "/example-data/sample",
    "C:/example-data/sample",
    "G:\\a\\b\\",                                   # 末尾バックスラッシュ
    '/a/b"c/d',                                     # 引用符
    "/a/b\nc\td",                                   # エスケープ文字
    "\\\\server\\share\\dir",
])
def test_path_values_roundtrip(reg, path_value):
    dr.write_registry({"ds": {"HOST": path_value}}, config_path=reg)
    assert dr.read_registry(reg) == {"ds": {"HOST": path_value}}


def test_backslashes_are_json_escaped(reg):
    dr.write_registry({"ds": {"H": r"G:\a\b"}}, config_path=reg)
    assert r'"G:\\a\\b"' in reg.read_text(encoding="utf-8")


def test_transaction_without_writer_leaves_file_untouched(reg):
    reg.write_text(json.dumps({"a": {"H": "/p"}}) + "\n", encoding="utf-8")
    before = reg.read_bytes()
    with dr.registry_transaction(config_path=reg) as (fresh, _writer):
        assert fresh == {"a": {"H": "/p"}}
    assert reg.read_bytes() == before


def test_transaction_refuses_to_reach_writer_on_corrupt_file(reg):
    reg.write_text("{broken", encoding="utf-8")
    with pytest.raises(dr.RegistryError):
        with dr.registry_transaction(config_path=reg) as (_fresh, _writer):
            pytest.fail("writer must not be reached")
    assert reg.read_text(encoding="utf-8") == "{broken"


def test_write_failure_propagates_and_releases_lock(reg, monkeypatch):
    import dataset_registry

    def boom(*a, **k):
        raise OSError("write failed")

    # 局所パッチは context() で解除する。monkeypatch.undo() は conftest の autouse 隔離
    # （FS_OVERRIDE・既定 registry 差し替え等）まで巻き戻してしまうため使わない。
    with monkeypatch.context() as m:
        m.setattr(dataset_registry, "atomic_write_text", boom)
        with pytest.raises(OSError):
            dr.write_registry({"a": {"H": "/p"}}, config_path=reg)
    # ロックが解放されていれば次の書き込みが（ブロックせず）成功する。
    dr.write_registry({"a": {"H": "/p"}}, config_path=reg)
    assert dr.read_registry(reg) == {"a": {"H": "/p"}}


def test_rewriting_same_content_is_stable(reg):
    dr.write_registry({"a": {"H": "/p"}}, config_path=reg)
    first = reg.read_bytes()
    dr.write_registry({"a": {"H": "/p"}}, config_path=reg)
    assert reg.read_bytes() == first


def test_write_registry_replaces_whole_registry(reg):
    dr.write_registry({"a": {"H": "/p"}, "b": {"H": "/q"}}, config_path=reg)
    dr.write_registry({"a": {"H": "/p"}}, config_path=reg)
    assert dr.read_registry(reg) == {"a": {"H": "/p"}}


def test_storage_does_not_touch_config_datasets(reg):
    import config

    before = dict(config.DATASETS)
    dr.write_registry({"storage_only": {"H": "/p"}}, config_path=reg)
    assert config.DATASETS == before


# --------------------------------------------------------------------------- #
# UTF-8 エンコード不能値 — atomic_write_text に到達させない（原本保護）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [
    {"ds": {"H": "\ud800"}},         # path 値
    {"ds": {"\ud800": "/p"}},        # host キー
    {"\ud800": {"H": "/p"}},         # dataset キー
])
def test_lone_surrogate_rejected_before_write(reg, bad, monkeypatch):
    import dataset_registry

    def boom(*a, **k):
        pytest.fail("atomic_write_text must not be reached")

    monkeypatch.setattr(dataset_registry, "atomic_write_text", boom)
    with pytest.raises(dr.RegistryError):
        dr.serialize_registry(bad)
    with pytest.raises(dr.RegistryError):
        dr.write_registry(bad, config_path=reg)


def test_write_error_names_the_target_file(reg, monkeypatch):
    """writer 境界の RegistryError は対象登録簿ファイル名を含む（§2.2 契約）。

    純粋関数 serialize_registry 自体はファイル名を知らないので、writer が target.name を
    付けて包む。ヘルパー（atomic_write_text）未到達も併せて確認する。
    """
    import dataset_registry

    def boom(*a, **k):
        pytest.fail("atomic_write_text must not be reached")

    monkeypatch.setattr(dataset_registry, "atomic_write_text", boom)
    with pytest.raises(dr.RegistryError) as ei:
        dr.write_registry({1: {}}, config_path=reg)      # 非 str の外側キー
    assert "datasets.local.json" in str(ei.value)


def test_encode_failure_leaves_existing_bytes_intact_inplace(reg, monkeypatch):
    """in-place 経路（truncate 先行）でも原本が空にならないこと。

    `MYANALYSIS_WRITE_STRATEGY=inplace` だけを設定する（`MYANALYSIS_FORCE_FRAGILE` は
    `_classify` で FS_OVERRIDE より先に評価され、ロック実体を実作業コピーの
    data/locks/ へ退避させてしまうため使わない）。
    """
    monkeypatch.setenv("MYANALYSIS_WRITE_STRATEGY", "inplace")
    dr.write_registry({"keep": {"H": "/p"}}, config_path=reg)
    before = reg.read_bytes()
    with pytest.raises(dr.RegistryError):
        dr.write_registry({"ds": {"H": "\ud800"}}, config_path=reg)
    assert reg.read_bytes() == before


def test_non_ascii_roundtrips_in_inplace_mode(reg, monkeypatch):
    monkeypatch.setenv("MYANALYSIS_WRITE_STRATEGY", "inplace")
    dr.write_registry({"日本語データ": {"ホスト名": r"D:\サンプル\データ"}}, config_path=reg)
    assert dr.read_registry(reg) == {"日本語データ": {"ホスト名": r"D:\サンプル\データ"}}


def test_serialize_registry_has_no_side_effects(reg):
    text = dr.serialize_registry({"a": {"H": "/p"}})
    assert json.loads(text) == {"a": {"H": "/p"}}
    assert text.endswith("\n")
    assert not reg.exists()


# --------------------------------------------------------------------------- #
# ロック隔離（force-fragile 専用。repo_root をここでだけ差し替える）
# --------------------------------------------------------------------------- #
def test_force_fragile_lock_stays_in_tmp(tmp_path, reg, monkeypatch):
    from common import fs_kind
    from common import paths as common_paths

    # repo_root を tmp に固定するので、force-fragile でも lock_file_for の退避先は
    # tmp/data/locks に構造的に限定される（実作業コピーの data/locks は触れない）。
    # 局所パッチは context() で解除し、conftest の autouse 隔離は巻き戻さない。
    # 実 repo ディレクトリは走査しない（他プロセスのロック生成と競合し env 依存になるため）。
    with monkeypatch.context() as m:
        m.setattr(common_paths, "repo_root", lambda: tmp_path)
        m.setenv("MYANALYSIS_FORCE_FRAGILE", "1")
        fs_kind.cache_clear()
        try:
            dr.write_registry({"a": {"H": "/p"}}, config_path=reg)
        finally:
            fs_kind.cache_clear()

    locks_dir = tmp_path / "data" / "locks"
    assert locks_dir.is_dir()
    assert any(locks_dir.iterdir())        # ロック実体が tmp 内に作られた（退避先が tmp に限定された証拠）
    assert dr.read_registry(reg) == {"a": {"H": "/p"}}


# --------------------------------------------------------------------------- #
# 排他（2 プロセス）
# --------------------------------------------------------------------------- #
_CHILD = textwrap.dedent("""
    import sys
    sys.path.insert(0, {repo!r})
    from pathlib import Path
    import dataset_registry as dr

    reg = Path({reg!r})
    name = sys.argv[1]
    hold = sys.argv[2] == "hold"
    print("TRYING", flush=True)            # ロック取得を試みる直前の合図
    with dr.registry_transaction(config_path=reg) as (fresh, writer):
        if hold:
            print("ACQUIRED", flush=True)
            sys.stdin.readline()          # 親の合図まで保持
        fresh.setdefault(name, {{}})["H"] = "/p/" + name
        writer(fresh)
    print("DONE", flush=True)
""")


def _readline(proc, timeout):
    """proc.stdout から 1 行を timeout 秒以内に読む（超過は AssertionError）。

    holder が合図前に死んでもスイートが無期限にブロックしないための期限付き読み取り。
    """
    out = []
    t = threading.Thread(target=lambda: out.append(proc.stdout.readline()))
    t.daemon = True
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise AssertionError(f"no line from child within {timeout}s")
    return out[0].strip()


def _spawn(tmp_path, reg, name, mode):
    env = dict(os.environ)
    env.pop("MYANALYSIS_FORCE_FRAGILE", None)
    env["MYANALYSIS_FS_OVERRIDE"] = f"{tmp_path}=local"
    script = _CHILD.format(repo=str(repo_root()), reg=str(reg))
    return subprocess.Popen(
        [sys.executable, "-c", script, name, mode],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, cwd=str(tmp_path),
    )


def test_concurrent_transactions_do_not_lose_entries(tmp_path, reg):
    reg.write_text("{}\n", encoding="utf-8")
    holder = _spawn(tmp_path, reg, "first", "hold")
    waiter = None
    try:
        assert _readline(holder, 10) == "TRYING"
        assert _readline(holder, 10) == "ACQUIRED"      # holder がロックを保持した
        waiter = _spawn(tmp_path, reg, "second", "now")
        assert _readline(waiter, 10) == "TRYING"         # waiter が取得を試みる地点に到達
        # holder 保持中は waiter がロック取得で進めない（起動の遅さではなく実際の排他の証拠）。
        with pytest.raises(subprocess.TimeoutExpired):
            waiter.wait(timeout=1.5)
        holder.stdin.write("go\n")
        holder.stdin.flush()
        assert holder.wait(timeout=30) == 0
        assert waiter.wait(timeout=30) == 0
    finally:
        for p in (waiter, holder):
            if p is None:
                continue
            p.kill()
            try:
                p.wait(timeout=10)            # kill 後に回収して子プロセスを残さない
            except subprocess.TimeoutExpired:
                pass

    data = dr.read_registry(reg)
    assert data == {"first": {"H": "/p/first"}, "second": {"H": "/p/second"}}


# --------------------------------------------------------------------------- #
# Git 除外
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", [
    "datasets.local.json",
    "datasets.local.json.lock",
    "datasets.local.json.a1b2c3.tmp",
    "datasets.local.json.bak",
    "datasets.local.json.corrupt",
])
def test_registry_artifacts_are_git_ignored(tmp_path, name):
    import shutil

    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = tmp_path / "gitrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    shutil.copy(repo_root() / ".gitignore", repo / ".gitignore")
    (repo / name).write_text("x", encoding="utf-8")
    r = subprocess.run(["git", "check-ignore", name], cwd=repo,
                       capture_output=True, text=True)
    assert r.returncode == 0, f"{name} is not git-ignored"
