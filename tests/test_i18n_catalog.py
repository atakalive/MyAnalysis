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

IN_FILES = [
    "gui/window.py",
    "gui/chat.py",
    "common/paths.py",
    "dataset_config.py",
    "tool.py",
]


@pytest.fixture(autouse=True)
def _restore_i18n():
    import common.i18n as i18n
    saved = (i18n._active, dict(i18n._catalogs))
    yield
    i18n._active, i18n._catalogs = saved[0], saved[1]


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


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


def test_used_keys_subset_and_no_dynamic_keys():
    cats = _load_all_catalogs()
    keys = set()
    for rel in IN_FILES:
        tree = _parse(rel)
        for arg0 in _tr_arg0(tree):
            assert isinstance(arg0, ast.Constant) and isinstance(arg0.value, str), \
                f"dynamic tr() key in {rel} (line {getattr(arg0, 'lineno', '?')})"
            keys.add(arg0.value)
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


def test_no_unwrapped_ui_literals():
    import dataset_config
    CJK = re.compile(r'[぀-ヿ㐀-鿿ｦ-ﾟ]')
    ALLOWLIST = {dataset_config._TEMPLATE}
    FORBIDDEN_EN_SUBSTR = ["Ctrl+Enter to send", "stopping…", "waiting…"]

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
        for s in _ui_strings(tree):
            assert not (CJK.search(s) and s not in ALLOWLIST), \
                f"unwrapped CJK literal in {rel}: {s!r}"
            assert not any(sub in s for sub in FORBIDDEN_EN_SUBSTR), \
                f"unwrapped EN UI literal in {rel}: {s!r}"
