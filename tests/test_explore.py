"""Hermetic tests for common.explore and the new llm_bridge CLI subcommands.

All filesystem-touching tests redirect common.paths.repo_root to tmp_path via
monkeypatch (the `fake_roots` fixture), mirroring tests/test_newanalysis.py, so
the real repo's data/ directory is never touched. Dataset-loading tests
monkeypatch config.get_dataset_dir / common.loaders.load_csv_per_subdir at the
module paths common.explore looks them up from at call time.
"""

from __future__ import annotations

import pytest

import common.explore as explore
import llm_bridge.__main__ as bridge_main
from common.paths import validate_name


@pytest.fixture()
def fake_roots(monkeypatch, tmp_path):
    """common.paths.repo_root を tmp_path にリダイレクト。"""
    monkeypatch.setattr("common.paths.repo_root", lambda: tmp_path)
    return tmp_path


# ---- load_dataset ----

def test_load_dataset_delegates(monkeypatch, tmp_path):
    captured = {}

    def fake_get_dataset_dir(name):
        captured["name"] = name
        return tmp_path / "ds_root"

    def fake_load(root, subdir_pattern, csv_name, encoding):
        captured["root"] = root
        captured["subdir_pattern"] = subdir_pattern
        captured["csv_name"] = csv_name
        captured["encoding"] = encoding
        return [{"name": "s0", "dir": root / "s0", "df": object()}]

    monkeypatch.setattr("config.get_dataset_dir", fake_get_dataset_dir)
    monkeypatch.setattr("common.loaders.load_csv_per_subdir", fake_load)

    result = explore.load_dataset("test", subdir_pattern="sess_*", csv_name="rec.csv")

    assert captured["name"] == "test"
    assert captured["root"] == tmp_path / "ds_root"
    assert captured["subdir_pattern"] == "sess_*"
    assert captured["csv_name"] == "rec.csv"
    assert captured["encoding"] is None
    assert result == [{"name": "s0", "dir": tmp_path / "ds_root" / "s0", "df": result[0]["df"]}]


def test_load_dataset_custom_pattern(monkeypatch, tmp_path):
    captured = {}

    def fake_load(root, subdir_pattern, csv_name, encoding):
        captured["subdir_pattern"] = subdir_pattern
        captured["csv_name"] = csv_name
        captured["encoding"] = encoding
        return [{"name": "x", "dir": root, "df": object()}]

    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    monkeypatch.setattr("common.loaders.load_csv_per_subdir", fake_load)

    explore.load_dataset(
        "test", subdir_pattern="run_*", csv_name="data.csv", encoding="cp932"
    )

    assert captured["subdir_pattern"] == "run_*"
    assert captured["csv_name"] == "data.csv"
    assert captured["encoding"] == "cp932"


def test_load_dataset_unknown_raises(monkeypatch):
    monkeypatch.setattr("config.DATASETS", {})
    with pytest.raises(KeyError):
        explore.load_dataset("nope")


def test_load_dataset_empty_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    monkeypatch.setattr(
        "dataset_config.load_config",
        lambda name: {"work_dir": "_work", "format": "csv_per_subdir"},
    )
    monkeypatch.setattr("common.loaders.load_csv_per_subdir",
                        lambda **kw: [])
    with pytest.raises(FileNotFoundError):
        explore.load_dataset("test", subdir_pattern="s_*", csv_name="d.csv")


def test_load_dataset_missing_pattern_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    monkeypatch.setattr(
        "dataset_config.load_config",
        lambda name: {"work_dir": "_work", "format": "csv_per_subdir"},
    )
    with pytest.raises(ValueError) as ei:
        explore.load_dataset("test")
    msg = str(ei.value)
    assert "no default load pattern" in msg
    assert "ac_session" not in msg
    assert "samples.csv" not in msg


# ---- save_fig ----

def test_save_fig_creates_file(monkeypatch, tmp_path):
    from matplotlib.figure import Figure
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    fig = Figure()
    fig.add_subplot(111).plot([0, 1], [0, 1])

    path = explore.save_fig("ds", fig, "test_out")

    expected = tmp_path / "_work" / "figures" / "test_out.png"
    assert path == expected
    assert expected.is_file()
    assert expected.stat().st_size > 0


@pytest.mark.parametrize("label", ["", "a/b", "a\\b", ".", "..", "has\0nul"])
def test_save_fig_invalid_labels(monkeypatch, tmp_path, label):
    from matplotlib.figure import Figure
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    fig = Figure()
    with pytest.raises(ValueError):
        explore.save_fig("ds", fig, label)


def test_save_fig_overwrites(monkeypatch, tmp_path):
    from matplotlib.figure import Figure
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)

    fig1 = Figure(figsize=(2, 2))
    fig1.add_subplot(111).plot([0, 1], [0, 1])
    explore.save_fig("ds", fig1, "dup")

    expected = tmp_path / "_work" / "figures" / "dup.png"
    first_size = expected.stat().st_size

    fig2 = Figure(figsize=(8, 8))
    fig2.add_subplot(111).plot([0, 1], [1, 0])
    path = explore.save_fig("ds", fig2, "dup")

    assert path == expected
    assert expected.is_file()
    assert expected.stat().st_size > first_size


# ---- save_code ----

def test_save_code_creates_file(monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    path = explore.save_code("ds", "snippet", "print('hello')")
    expected = tmp_path / "_work" / "code" / "snippet.py"
    assert path == expected
    assert expected.is_file()
    assert expected.read_text(encoding="utf-8") == "print('hello')"


@pytest.mark.parametrize("label", ["", "a/b", "a\\b", ".", "..", "has\0nul"])
def test_save_code_invalid_labels(monkeypatch, tmp_path, label):
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    with pytest.raises(ValueError):
        explore.save_code("ds", label, "x")


def test_save_code_dotted_label_points_at_save_text(monkeypatch, tmp_path):
    """報告された欠陥そのもの: 以前は黙って code/notes.md.py が出来ていた。"""
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    with pytest.raises(ValueError, match="save_text"):
        explore.save_code("ds", "notes.md", "# report")
    assert not (tmp_path / "_work" / "code" / "notes.md.py").exists()


def test_save_code_accepts_explicit_py_suffix(monkeypatch, tmp_path):
    """明示的な .py は冪等に扱う（snippet.py.py にしない）。"""
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    path = explore.save_code("ds", "snippet.py", "x = 1")
    assert path == tmp_path / "_work" / "code" / "snippet.py"
    assert path.read_text(encoding="utf-8") == "x = 1"


def test_save_fig_dotted_label_rejected(monkeypatch, tmp_path):
    """save_fig も同じ欠陥を持っていた（chart.png → chart.png.png）。"""
    from matplotlib.figure import Figure
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    with pytest.raises(ValueError):
        explore.save_fig("ds", Figure(), "chart.v2")
    assert not (tmp_path / "_work" / "figures" / "chart.v2.png").exists()


def test_save_fig_accepts_explicit_png_suffix(monkeypatch, tmp_path):
    from matplotlib.figure import Figure
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    fig = Figure()
    fig.add_subplot(111).plot([0, 1], [0, 1])
    path = explore.save_fig("ds", fig, "chart.png")
    assert path == tmp_path / "_work" / "figures" / "chart.png"


# ---- save_text ----

def test_save_text_creates_file(monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    path = explore.save_text("ds", "notes.md", "# hello")
    expected = tmp_path / "_work" / "notes.md"
    assert path == expected
    assert expected.read_text(encoding="utf-8") == "# hello"


def test_save_text_creates_subdirs(monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    path = explore.save_text("ds", "reports/2026-07/summary.md", "body")
    expected = tmp_path / "_work" / "reports" / "2026-07" / "summary.md"
    assert path == expected
    assert expected.read_text(encoding="utf-8") == "body"


def test_save_text_accepts_backslash_separator(monkeypatch, tmp_path):
    """POSIX CI でも Windows でも同じ場所に落ちる（PureWindowsPath で解析するため）。"""
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    path = explore.save_text("ds", "reports\\summary.csv", "a,b\n1,2\n")
    assert path == tmp_path / "_work" / "reports" / "summary.csv"


def test_save_text_overwrites(monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    explore.save_text("ds", "notes.md", "first")
    path = explore.save_text("ds", "notes.md", "second")
    assert path.read_text(encoding="utf-8") == "second"


def test_save_text_respects_configured_work_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    (tmp_path / "myanalysis.toml").write_text(
        'work_dir = "results"\nformat = "csv_per_subdir"\n', encoding="utf-8"
    )
    path = explore.save_text("ds", "notes.md", "x")
    assert path == tmp_path / "results" / "notes.md"


@pytest.mark.parametrize("relpath", [
    "../../evil.md", "a/../../b.md", "C:/evil.md", "C:evil.md",
    "/etc/evil", "\\\\srv\\share\\x.md", "nul.txt", ".hidden.md", "",
])
def test_save_text_rejects_escapes(monkeypatch, tmp_path, relpath):
    """拒否時は tmp_path 配下に何も作られていないこと。

    validate より先に mkdir する順序へ退行すると、拒否したパスの途中ディレクトリだけが
    同期ドライブ上に残る。それを検出する。
    """
    dataset = tmp_path / "ds"
    dataset.mkdir()
    monkeypatch.setattr("config.get_dataset_dir", lambda name: dataset)
    with pytest.raises(ValueError):
        explore.save_text("ds", relpath, "payload")
    strays = [p for p in tmp_path.rglob("*")
              if p.is_file() and p.name != "myanalysis.toml"]
    assert strays == []


@pytest.mark.parametrize("relpath", [
    "session.json", "session.json.bak",
    "chat_sessions/abc.json", "chat_sessions/abc.json.bak",
    "figures/plot.png.bak",
])
def test_save_text_rejects_reserved_destinations(monkeypatch, tmp_path, relpath):
    """work_dir 直下の live 状態（タブ構成・チャット履歴）を上書きさせない。"""
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    work = tmp_path / "_work"
    work.mkdir()
    victim = work / "session.json"
    victim.write_text('{"tabs": ["keep me"]}', encoding="utf-8")
    with pytest.raises(ValueError, match="live GUI state"):
        explore.save_text("ds", relpath, "{}")
    assert victim.read_text(encoding="utf-8") == '{"tabs": ["keep me"]}'


def test_save_text_on_fragile_fs_never_renames(monkeypatch, tmp_path):
    """新しい書込経路がマウント安全戦略に乗っている証明（Issue #96 の中核）。

    fragile FS では os.replace を一度も呼ばず、.tmp も残さない。
    """
    import os
    import common.fs_kind as fk
    import common.paths as cp
    monkeypatch.setattr(fk, "is_fragile", lambda _p: True)
    monkeypatch.setattr(cp.time, "sleep", lambda _s: None)
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    calls = {"n": 0}
    real_replace = os.replace
    monkeypatch.setattr(os, "replace",
                        lambda s, d: (calls.__setitem__("n", calls["n"] + 1),
                                      real_replace(s, d))[1])
    path = explore.save_text("ds", "reports/summary.md", "payload")
    assert calls["n"] == 0
    assert list(path.parent.glob("*.tmp")) == []
    assert path.read_text(encoding="utf-8") == "payload"


# ---- dataset_summary ----

def _subdir_of(summary, name):
    for s in summary["subdirs"]:
        if s["name"] == name:
            return s
    raise AssertionError(f"subdir {name!r} not in summary")


def test_dataset_summary_walks_layout(monkeypatch, tmp_path):
    root = tmp_path / "root"
    (root / "sess_a").mkdir(parents=True)
    (root / "sess_a" / "samples.csv").write_text("i,j\n1,2\n3,4\n5,6\n", encoding="utf-8")
    (root / "sess_b").mkdir()
    (root / "sess_b" / "data.csv").write_text("x\n1\n", encoding="utf-8")
    (root / "notes.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (root / "_work").mkdir()
    (root / "analyses").mkdir()
    (root / ".hidden").mkdir()
    (root / "myanalysis.toml").write_text("work_dir='_work'\n", encoding="utf-8")

    monkeypatch.setattr("config.get_dataset_dir", lambda name: root)
    monkeypatch.setattr(
        "dataset_config.get_work_dir", lambda name, *, create=True: root / "_work"
    )

    summary = explore.dataset_summary("test")

    assert summary["name"] == "test"
    assert summary["path"] == str(root)
    assert [s["name"] for s in summary["subdirs"]] == ["sess_a", "sess_b"]
    sa = _subdir_of(summary, "sess_a")
    assert set(sa) == {"name", "files", "subdirs"}
    assert "samples.csv" in sa["files"]
    assert sa["subdirs"] == []

    paths = [c["path"] for c in summary["csv_samples"]]
    assert paths == ["sess_a/samples.csv", "sess_b/data.csv", "notes.csv"]
    sa_csv = next(c for c in summary["csv_samples"] if c["path"] == "sess_a/samples.csv")
    assert sa_csv["columns"] == ["i", "j"]
    assert sa_csv["rows"] == 3

    assert "sessions" not in summary
    assert "format" not in summary
    assert "csv_samples" in summary
    all_files = [f for s in summary["subdirs"] for f in s["files"]]
    assert "myanalysis.toml" not in all_files
    assert "myanalysis.toml" not in paths


def test_dataset_summary_excludes_configured_work_dir(monkeypatch, tmp_path):
    root = tmp_path / "root"
    (root / "results").mkdir(parents=True)
    (root / "results" / "out.csv").write_text("a\n1\n", encoding="utf-8")
    (root / "real_sess").mkdir()
    (root / "real_sess" / "m.csv").write_text("b\n1\n", encoding="utf-8")

    monkeypatch.setattr("config.get_dataset_dir", lambda name: root)
    monkeypatch.setattr(
        "dataset_config.get_work_dir", lambda name, *, create=True: root / "results"
    )

    summary = explore.dataset_summary("test")
    names = [s["name"] for s in summary["subdirs"]]
    assert "results" not in names
    assert "real_sess" in names
    assert "results/out.csv" not in [c["path"] for c in summary["csv_samples"]]


def test_dataset_summary_workdir_resolve_failure_falls_back(monkeypatch, tmp_path, capsys):
    root = tmp_path / "root"
    (root / "_work").mkdir(parents=True)
    (root / "_work" / "junk.csv").write_text("a\n1\n", encoding="utf-8")
    (root / "sess").mkdir()
    (root / "sess" / "m.csv").write_text("b\n1\n", encoding="utf-8")

    def boom(name, *, create=True):
        raise ValueError("bad toml")

    monkeypatch.setattr("config.get_dataset_dir", lambda name: root)
    monkeypatch.setattr("dataset_config.get_work_dir", boom)

    summary = explore.dataset_summary("test")
    names = [s["name"] for s in summary["subdirs"]]
    assert "_work" not in names
    assert "sess" in names
    assert "could not resolve work_dir" in capsys.readouterr().err


def test_dataset_summary_surfaces_nested_subdirs(monkeypatch, tmp_path):
    root = tmp_path / "root"
    (root / "cond_A" / "rep_1").mkdir(parents=True)
    (root / "cond_A" / "rep_1" / "data.csv").write_text("a\n1\n", encoding="utf-8")
    (root / "cond_A" / "rep_2").mkdir(parents=True)
    (root / "cond_A" / "rep_2" / "data.csv").write_text("a\n1\n", encoding="utf-8")

    monkeypatch.setattr("config.get_dataset_dir", lambda name: root)
    monkeypatch.setattr(
        "dataset_config.get_work_dir", lambda name, *, create=True: root / "_work"
    )

    summary = explore.dataset_summary("test")
    ca = _subdir_of(summary, "cond_A")
    assert ca["subdirs"] == ["rep_1", "rep_2"]
    assert ca["files"] == []
    assert "cond_A/rep_1/data.csv" not in [c["path"] for c in summary["csv_samples"]]
    assert summary["csv_samples"] == []


def test_dataset_summary_skips_dotfiles_in_subdirs(monkeypatch, tmp_path):
    root = tmp_path / "root"
    (root / "sess").mkdir(parents=True)
    (root / "sess" / ".hidden.csv").write_text("a\n1\n", encoding="utf-8")
    (root / "sess" / "real.csv").write_text("b\n1\n", encoding="utf-8")
    (root / ".dotdir").mkdir()

    monkeypatch.setattr("config.get_dataset_dir", lambda name: root)
    monkeypatch.setattr(
        "dataset_config.get_work_dir", lambda name, *, create=True: root / "_work"
    )

    summary = explore.dataset_summary("test")
    sess = _subdir_of(summary, "sess")
    assert ".hidden.csv" not in sess["files"]
    assert "real.csv" in sess["files"]
    paths = [c["path"] for c in summary["csv_samples"]]
    assert all(not p.endswith(".hidden.csv") for p in paths)
    assert ".dotdir" not in [s["name"] for s in summary["subdirs"]]


def test_dataset_summary_subdir_read_error_continues(monkeypatch, tmp_path, capsys):
    from pathlib import Path

    root = tmp_path / "root"
    (root / "locked").mkdir(parents=True)
    (root / "locked" / "x.csv").write_text("a\n1\n", encoding="utf-8")
    (root / "ok").mkdir()
    (root / "ok" / "y.csv").write_text("b\n1\n", encoding="utf-8")

    real_iterdir = Path.iterdir

    def fake_iterdir(self):
        if self.name == "locked":
            raise PermissionError("nope")
        return real_iterdir(self)

    monkeypatch.setattr("config.get_dataset_dir", lambda name: root)
    monkeypatch.setattr(
        "dataset_config.get_work_dir", lambda name, *, create=True: root / "_work"
    )
    monkeypatch.setattr(Path, "iterdir", fake_iterdir)

    summary = explore.dataset_summary("test")
    locked = _subdir_of(summary, "locked")
    assert locked["files"] == []
    assert locked["subdirs"] == []
    ok = _subdir_of(summary, "ok")
    assert "y.csv" in ok["files"]
    assert "cannot list" in capsys.readouterr().err


def test_dataset_summary_big_csv_skips_rows(monkeypatch, tmp_path):
    root = tmp_path / "root"
    (root / "sess").mkdir(parents=True)
    (root / "sess" / "big.csv").write_text("aa,bb\n1,2\n3,4\n", encoding="utf-8")

    monkeypatch.setattr("config.get_dataset_dir", lambda name: root)
    monkeypatch.setattr(
        "dataset_config.get_work_dir", lambda name, *, create=True: root / "_work"
    )
    monkeypatch.setattr(explore, "_MAX_CSV_BYTES_FOR_ROWS", 5)

    summary = explore.dataset_summary("test")
    entry = next(c for c in summary["csv_samples"] if c["path"] == "sess/big.csv")
    assert "columns" in entry
    assert "rows" not in entry


def test_dataset_summary_rows_no_trailing_newline(monkeypatch, tmp_path):
    root = tmp_path / "root"
    (root / "no_nl").mkdir(parents=True)
    (root / "no_nl" / "rec.csv").write_bytes(b"i,j\n1,2\n3,4")
    (root / "with_nl").mkdir()
    (root / "with_nl" / "rec.csv").write_bytes(b"i,j\n1,2\n3,4\n")

    monkeypatch.setattr("config.get_dataset_dir", lambda name: root)
    monkeypatch.setattr(
        "dataset_config.get_work_dir", lambda name, *, create=True: root / "_work"
    )

    summary = explore.dataset_summary("test")
    by_path = {c["path"]: c for c in summary["csv_samples"]}
    assert by_path["no_nl/rec.csv"]["rows"] == 2
    assert by_path["with_nl/rec.csv"]["rows"] == 2


def test_dataset_summary_warns_on_subdir_truncation(monkeypatch, tmp_path, capsys):
    root = tmp_path / "root"
    (root / "sess" / "c1").mkdir(parents=True)
    (root / "sess" / "c2").mkdir()
    (root / "sess" / "c3").mkdir()

    monkeypatch.setattr("config.get_dataset_dir", lambda name: root)
    monkeypatch.setattr(
        "dataset_config.get_work_dir", lambda name, *, create=True: root / "_work"
    )

    summary = explore.dataset_summary("test", max_files=2)
    sess = _subdir_of(summary, "sess")
    assert sess["subdirs"] == ["c1", "c2"]
    err = capsys.readouterr().err
    assert "3 subdirs" in err
    assert "first 2" in err


def test_dataset_summary_bad_csv_skipped_continues(monkeypatch, tmp_path, capsys):
    """壊れた CSV（0 バイト → EmptyDataError=ValueError）はヘッダ読取り失敗で
    csv_samples から除外され警告を出しつつ、同 subdir の正当 CSV は残り探索は継続する。"""
    root = tmp_path / "root"
    (root / "sess").mkdir(parents=True)
    (root / "sess" / "empty.csv").write_bytes(b"")
    (root / "sess" / "good.csv").write_bytes(b"i,j\n1,2\n")

    monkeypatch.setattr("config.get_dataset_dir", lambda name: root)
    monkeypatch.setattr(
        "dataset_config.get_work_dir", lambda name, *, create=True: root / "_work"
    )

    summary = explore.dataset_summary("test")
    paths = [c["path"] for c in summary["csv_samples"]]
    assert "sess/empty.csv" not in paths
    good = next(c for c in summary["csv_samples"] if c["path"] == "sess/good.csv")
    assert good["columns"] == ["i", "j"]
    assert good["rows"] == 1
    assert "cannot read header" in capsys.readouterr().err


def test_dataset_summary_missing_dir_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path / "nope")
    with pytest.raises(FileNotFoundError):
        explore.dataset_summary("test")


# ---- validate_name ----

@pytest.mark.parametrize("name", ["", "a/b", "a\\b", ".", "..", "x\0y"])
def test_validate_name_rejects(name):
    with pytest.raises(ValueError):
        validate_name(name)


@pytest.mark.parametrize("name", ["hello", "test_123", "a"])
def test_validate_name_accepts(name):
    validate_name(name)  # 例外が出ないこと。


# ---- CLI: list-datasets ----

def test_list_datasets_cli(monkeypatch, capsys):
    monkeypatch.setattr(
        "config.DATASETS",
        {"zeta": {}, "alpha": {}, "my_dataset": {}},
    )
    rc = bridge_main.main(["list-datasets"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.splitlines() == ["alpha", "my_dataset", "zeta"]


# ---- CLI: list-analyses excludes underscore-prefixed dirs ----

def test_list_analyses_excludes_underscore(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path / name)
    ds = "ds_test"
    analyses = tmp_path / ds / "analyses"
    for name in ("_viewer", "real_one"):
        d = analyses / name
        d.mkdir(parents=True)
        (d / "analysis.py").write_text("# stub\n", encoding="utf-8")

    rc = bridge_main.main(["list-analyses", "--dataset", ds])
    assert rc == 0
    names = capsys.readouterr().out.splitlines()
    assert "_viewer" not in names
    assert "real_one" in names


# ---- load_dataset / dataset_summary: format dispatch (#27) ----

def test_load_dataset_custom_format_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    monkeypatch.setattr(
        "dataset_config.load_config", lambda name: {"work_dir": "_work", "format": "custom"}
    )
    with pytest.raises(NotImplementedError) as ei:
        explore.load_dataset("test")
    assert "list-analyses" in str(ei.value)


def test_load_dataset_unknown_format_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("config.get_dataset_dir", lambda name: tmp_path)
    monkeypatch.setattr(
        "dataset_config.load_config", lambda name: {"work_dir": "_work", "format": "weird"}
    )
    with pytest.raises(ValueError):
        explore.load_dataset("test")


# ---- CLI: list-datasets --json (#27) ----

def test_list_datasets_json_structure(monkeypatch, capsys):
    monkeypatch.setattr("config.DATASETS", {"alpha": {}, "beta": {}})
    monkeypatch.setattr(
        "dataset_config.load_config",
        lambda name: {"work_dir": "_work", "format": "csv_per_subdir"},
    )
    rc = bridge_main.main(["list-datasets", "--json"])
    assert rc == 0
    import json
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    for entry in data:
        # #50: description added as a non-breaking superset (name/path/format kept).
        assert {"name", "path", "format", "description"} <= set(entry)


def test_list_datasets_json_format_null_on_error(monkeypatch, capsys):
    monkeypatch.setattr("config.DATASETS", {"good": {}, "broken": {}})

    def fake_load_config(name):
        if name == "broken":
            raise ValueError("corrupt toml")
        return {"work_dir": "_work", "format": "csv_per_subdir"}

    monkeypatch.setattr("dataset_config.load_config", fake_load_config)
    rc = bridge_main.main(["list-datasets", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    import json
    data = json.loads(captured.out)
    by_name = {e["name"]: e for e in data}
    assert by_name["good"]["format"] == "csv_per_subdir"
    assert by_name["broken"]["format"] is None
    assert "warning" in captured.err


def test_list_datasets_plain_unchanged(monkeypatch, capsys):
    monkeypatch.setattr("config.DATASETS", {"zeta": {}, "alpha": {}})
    rc = bridge_main.main(["list-datasets"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.splitlines() == ["alpha", "zeta"]


def test_register_dataset_format_custom_new(monkeypatch, capsys):
    calls = []

    def fake_register_dataset(name, path, host):
        calls.append(("register", name))
        return {"created": True, "name": name, "host": "HOST", "path": path}

    def fake_reload():
        calls.append(("reload", None))

    def fake_set_format(name, fmt):
        calls.append(("set_format", name, fmt))

    monkeypatch.setattr("config.register_dataset", fake_register_dataset)
    monkeypatch.setattr("config.reload_datasets", fake_reload)
    monkeypatch.setattr("dataset_config.set_format", fake_set_format)

    import os
    existing_path = os.getcwd()  # an existing path on this host
    rc = bridge_main.main([
        "register-dataset", "newds", existing_path, "--format", "custom", "--no-open",
    ])
    assert rc == 0
    # order: register → reload → set_format
    kinds = [c[0] for c in calls]
    assert kinds.index("register") < kinds.index("reload") < kinds.index("set_format")
    sf = [c for c in calls if c[0] == "set_format"][0]
    assert sf[1] == "newds" and sf[2] == "custom"


def test_register_dataset_format_omitted_keeps_existing(monkeypatch, capsys):
    """--format 省略時は set_format を呼ばず ensure_config だけ（既存の format を
    上書きしない。Issue #100 D-7）。"""
    calls = []

    def fake_register_dataset(name, path, host):
        return {"created": False, "name": name, "host": "HOST", "path": path}

    monkeypatch.setattr("config.register_dataset", fake_register_dataset)
    monkeypatch.setattr("config.reload_datasets", lambda: None)
    monkeypatch.setattr(
        "dataset_config.set_format", lambda name, fmt: calls.append(("set_format", name))
    )
    monkeypatch.setattr(
        "dataset_config.ensure_config", lambda name: calls.append(("ensure_config", name))
    )

    import os
    existing_path = os.getcwd()  # an existing path on this host
    rc = bridge_main.main(["register-dataset", "newds", existing_path, "--no-open"])
    assert rc == 0
    assert [c for c in calls if c[0] == "set_format"] == []
    assert [c for c in calls if c[0] == "ensure_config"] == [("ensure_config", "newds")]
