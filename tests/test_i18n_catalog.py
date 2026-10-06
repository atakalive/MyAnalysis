"""Catalog consistency guards — true pure Python (no Qt, no config at import).

Does NOT import gui.chat (pulls in PySide6). Imports dataset_config only for the
allowlist (it defers `from config import ...` into functions, so import-time is
Qt/config-free). All source analysis is AST-based (robust to quote/whitespace).
"""
import ast
import re
import string
import tomllib
from pathlib import Path

import pytest

from common.paths import i18n_dir

_SCAN_DIRS = (
    "common", "core", "devtools", "export", "gui",
    "llm_backend", "llm_bridge", "meeting", "newanalysis",
)

# tr(変数) を許すファイルと、その呼び出し数（増減したらテストが落ちる）。
# キーの出どころ: ENGINES.label_key → test_engines、preflight の note → test_preflight、
# model_catalog の note → test_model_catalog、
# それ以外は同じファイル内のリテラル（下のキー形リテラル検査で存在を確認する）。
DYNAMIC_TR_CALLS = {
    "gui/backend_selector_dialog.py": 3,
    "gui/backend_status_window.py": 3,
    "gui/meeting_share.py": 2,
    "llm_backend/engines.py": 1,
    "llm_bridge/__main__.py": 2,
}

# ファイル単位で許す CJK リテラル。ペルソナ名の予約語で、表示用の文字列ではない。
CJK_LITERAL_ALLOW = {"gui/persona_dialog.py": {"無し（お客様対応窓口）", "（なし）"}}

# CJK リテラル検査から外すファイル。CLI の argparse の help とエージェント向けの出力で、
# GUI の言語設定の対象外（CLI プロセスは init_language() を呼ばない）。キーの存在検査と
# 動的キーの件数検査、FORBIDDEN_EN_SUBSTR の検査の対象には残す。
CJK_EXEMPT_FILES = {"llm_bridge/__main__.py"}

KEY_SHAPE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _calls_tr(tree: ast.AST) -> bool:
    # tr(...) と、i18n.tr(...) / _m("common.i18n").tr(...) のような属性呼び出しの両方。
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        if isinstance(n.func, ast.Name) and n.func.id == "tr":
            return True
        if isinstance(n.func, ast.Attribute) and n.func.attr == "tr":
            return True
    return False


def _discover_in_files() -> list[str]:
    root = _repo_root()
    paths = list(root.glob("*.py"))
    for d in _SCAN_DIRS:
        paths += (root / d).rglob("*.py")
    out = []
    for p in paths:
        if "__pycache__" in p.parts:
            continue
        if _calls_tr(ast.parse(p.read_text(encoding="utf-8"))):
            out.append(p.relative_to(root).as_posix())
    return sorted(out)


IN_FILES = _discover_in_files()


@pytest.fixture(autouse=True)
def _restore_i18n():
    import common.i18n as i18n
    saved = (i18n._active, dict(i18n._catalogs))
    yield
    i18n._active, i18n._catalogs = saved[0], saved[1]


def _load_all_catalogs() -> dict[str, dict]:
    cats = {}
    for p in sorted(i18n_dir().glob("*.toml")):
        with open(p, "rb") as f:
            cats[p.stem] = tomllib.load(f)
    return cats


def _parse(rel: str) -> ast.AST:
    return ast.parse((_repo_root() / rel).read_text(encoding="utf-8"))


def _tr_arg0(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
           and n.func.id == "tr" and n.args:
            yield n.args[0]


def _fields(t: str):
    return frozenset(
        (fname, conv, spec)
        for _, fname, spec, conv in string.Formatter().parse(t)
        if fname is not None
    )


def test_catalogs_exist():
    cats = _load_all_catalogs()
    assert "en" in cats and "ja" in cats


def test_key_sets_match_across_catalogs():
    cats = _load_all_catalogs()
    keysets = {lang: set(c.keys()) for lang, c in cats.items()}
    ref = keysets["en"]
    for lang, ks in keysets.items():
        assert ks == ref, f"key set mismatch in {lang}: {ks ^ ref}"


def _docstring_ids(tree) -> set[int]:
    ids = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
           and n.body and isinstance(n.body[0], ast.Expr) \
           and isinstance(n.body[0].value, ast.Constant) \
           and isinstance(n.body[0].value.value, str):
            ids.add(id(n.body[0].value))
    return ids


def test_in_files_discovery():
    expected = {
        # 旧 IN_FILES
        "gui/window.py", "gui/chat.py", "common/paths.py", "dataset_config.py",
        "tool.py", "devtools/qt_integration.py",
        # tr() を呼ぶファイル
        "gui/backend_selector_dialog.py", "gui/backend_status_window.py",
        "gui/meeting_share.py", "gui/open_dataset_dialog.py", "gui/panels.py",
        "gui/persona_dialog.py", "llm_backend/engines.py", "llm_bridge/__main__.py",
        "meeting/relay.py", "gui/imageviewer.py",
    }
    missing = expected - set(IN_FILES)
    assert not missing, f"not discovered: {sorted(missing)}"


def test_scan_dirs_cover_all_packages():
    root = _repo_root()
    pkgs = {
        d.name for d in root.iterdir()
        if d.is_dir() and d.name != "tests" and (d / "__init__.py").is_file()
    }
    missing = pkgs - set(_SCAN_DIRS)
    assert not missing, f"packages not in _SCAN_DIRS: {sorted(missing)}"


def test_used_keys_subset_and_no_dynamic_keys():
    cats = _load_all_catalogs()
    keys = set()
    for rel in IN_FILES:
        tree = _parse(rel)
        dynamic = []
        for arg0 in _tr_arg0(tree):
            if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                keys.add(arg0.value)
            else:
                dynamic.append(getattr(arg0, "lineno", "?"))
        assert len(dynamic) == DYNAMIC_TR_CALLS.get(rel, 0), \
            f"dynamic tr() key count changed in {rel} (lines {dynamic}); " \
            f"update DYNAMIC_TR_CALLS only if the key source is checked elsewhere"
    for lang, c in cats.items():
        missing = keys - set(c.keys())
        assert not missing, f"keys used in source missing from {lang}: {missing}"


def test_placeholders_consistent_across_catalogs():
    cats = _load_all_catalogs()
    ref_lang, ref = next(iter(cats.items()))
    for key in ref:
        fieldsets = {lang: _fields(c[key]) for lang, c in cats.items()}
        base = fieldsets[ref_lang]
        for lang, fs in fieldsets.items():
            assert fs == base, f"placeholder mismatch for {key!r} in {lang}: {fs ^ base}"


def test_key_shaped_literals_resolve():
    cats = _load_all_catalogs()
    prefixes = {k.split(".", 1)[0] for k in cats["en"]}
    for rel in IN_FILES:
        tree = _parse(rel)
        docs = _docstring_ids(tree)
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Constant) and isinstance(n.value, str)) \
               or id(n) in docs:
                continue
            s = n.value
            if not KEY_SHAPE.match(s) or s.split(".", 1)[0] not in prefixes:
                continue
            for lang, c in cats.items():
                assert s in c, \
                    f"key-shaped literal {s!r} in {rel} (line {n.lineno}) missing from {lang}"


def test_no_unwrapped_ui_literals():
    import dataset_config
    CJK = re.compile(r'[぀-ヿ㐀-鿿ｦ-ﾟ]')
    ALLOWLIST = {dataset_config._TEMPLATE}
    FORBIDDEN_EN_SUBSTR = [
        "Ctrl+Enter to send", "stopping…", "waiting…",
        "not wired yet", "No analyses defined yet", "[stopped]", "start failed",
    ]

    def _tr_arg0_ids(tree):
        ids = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
               and n.func.id == "tr" and n.args and isinstance(n.args[0], ast.Constant):
                ids.add(id(n.args[0]))
        return ids

    def _ui_strings(tree):
        bare = {id(n.value) for n in ast.walk(tree)
                if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)}
        tr_keys = _tr_arg0_ids(tree)
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) \
               and id(n) not in bare and id(n) not in tr_keys:
                yield n.value

    for rel in IN_FILES:
        tree = _parse(rel)
        allow = ALLOWLIST | CJK_LITERAL_ALLOW.get(rel, set())
        for s in _ui_strings(tree):
            if rel not in CJK_EXEMPT_FILES:
                assert not (CJK.search(s) and s not in allow), \
                    f"unwrapped CJK literal in {rel}: {s!r}"
            assert not any(sub in s for sub in FORBIDDEN_EN_SUBSTR), \
                f"unwrapped EN UI literal in {rel}: {s!r}"
