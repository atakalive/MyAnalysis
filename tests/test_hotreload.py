"""Headless tests for devtools.hotreload (Qt-free core).

Throwaway modules are written into tmp_path, imported, edited on disk, then
reloaded via superreload — exercising in-place patching, rollback,
__hot_preserve__, lru_cache, topo order, AST top-level name extraction, and
class/property/stale-name handling.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sys

import pytest

from devtools.hotreload import (
    ModuleRecord,
    ReloadReport,
    _extract_toplevel_names,
    _toposort,
    superreload,
)


def _sha(path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


@pytest.fixture()
def loader(tmp_path):
    """Import a throwaway module from source; auto-cleans sys.modules.

    tmp_path is added to sys.path so importlib.reload (which re-finds the spec
    via the meta path) works — exactly as it does for real on-sys.path repo
    modules. Bytecode writing is disabled so a fast rewrite-then-reload in the
    same second can't read a stale .pyc.
    """
    import pathlib

    created: list[str] = []
    sys.path.insert(0, str(tmp_path))
    prev_dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True

    def _load(name: str, src: str):
        path = tmp_path / f"{name}.py"
        path.write_text(src, encoding="utf-8")
        sys.modules.pop(name, None)
        importlib.invalidate_caches()
        mod = importlib.import_module(name)
        created.append(name)
        rec = ModuleRecord(name, mod, pathlib.Path(mod.__file__).resolve(), _sha(path))
        return mod, path, rec

    yield _load
    sys.dont_write_bytecode = prev_dont_write
    for name in created:
        sys.modules.pop(name, None)
    try:
        sys.path.remove(str(tmp_path))
    except ValueError:
        pass
    importlib.invalidate_caches()


# ---------------------------------------------------------------------------
# _extract_toplevel_names
# ---------------------------------------------------------------------------

def test_extract_basic():
    names = _extract_toplevel_names(
        "import os\nfrom x import y\ndef f():\n  pass\nclass C:\n  pass\nA = 1\n"
    )
    assert {"os", "y", "f", "C", "A"} <= names


def test_extract_tuple_unpacking():
    names = _extract_toplevel_names("_MIN_PT, _MAX_PT = 6.0, 48.0\n")
    assert {"_MIN_PT", "_MAX_PT"} <= names


def test_extract_nested_tuple_and_star():
    names = _extract_toplevel_names("a, (b, c) = 1, (2, 3)\nx, *rest = [1, 2, 3]\n")
    assert {"a", "b", "c", "x", "rest"} <= names


def test_extract_walrus():
    names = _extract_toplevel_names("if (n := 5) > 0:\n  pass\n")
    assert "n" in names


def test_extract_conditional_import():
    names = _extract_toplevel_names("if True:\n  import os\n")
    assert "os" in names


def test_extract_try_import():
    names = _extract_toplevel_names(
        "try:\n  import a\nexcept ImportError:\n  a = None\n"
    )
    assert "a" in names


def test_extract_type_checking_import_included():
    # Over-approximation: TYPE_CHECKING-guarded import is collected, so the old
    # binding is never stale-pruned (harmless — type-only name).
    names = _extract_toplevel_names(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n  from x import Z\n"
    )
    assert "Z" in names and "TYPE_CHECKING" in names


def test_extract_for_with_except_targets():
    names = _extract_toplevel_names(
        "for i in []:\n  pass\n"
        "with open('x') as fh:\n  pass\n"
        "try:\n  pass\nexcept Exception as e:\n  pass\n"
    )
    assert {"i", "fh", "e"} <= names


def test_extract_star_import_returns_none():
    assert _extract_toplevel_names("from os import *\nA = 1\n") is None


# ---------------------------------------------------------------------------
# superreload — functions
# ---------------------------------------------------------------------------

def test_function_patched_in_place(loader):
    mod, path, rec = loader("hr_fn", "def f():\n  return 1\n")
    f = mod.f
    path.write_text("def f():\n  return 2\n", encoding="utf-8")
    rep = ReloadReport()
    assert superreload(rec, rep)
    assert f() == 2          # same object, new code
    assert mod.f is f
    assert "hr_fn" in rep.reloaded


def test_rollback_on_reload_error(loader):
    mod, path, rec = loader("hr_rb", "X = 1\ndef f():\n  return X\n")
    path.write_text("X = 1\nraise RuntimeError('boom')\n", encoding="utf-8")
    rep = ReloadReport()
    assert not superreload(rec, rep)
    assert rep.failed
    assert mod.X == 1 and mod.f() == 1   # fully restored


def test_stale_module_name_removed(loader):
    mod, path, rec = loader("hr_stale", "A = 1\nB = 2\n")
    path.write_text("A = 1\n", encoding="utf-8")
    rep = ReloadReport()
    superreload(rec, rep)
    assert mod.A == 1
    assert not hasattr(mod, "B")
    assert "hr_stale:B" in rep.removed


def test_tuple_unpacking_not_stale_pruned(loader):
    mod, path, rec = loader("hr_tup", "_MIN, _MAX = 1, 2\nX = 3\n")
    path.write_text("_MIN, _MAX = 1, 2\n", encoding="utf-8")  # drop X
    rep = ReloadReport()
    superreload(rec, rep)
    assert mod._MIN == 1 and mod._MAX == 2
    assert not hasattr(mod, "X")


def test_removed_callable_warns(loader):
    mod, path, rec = loader("hr_cb", "def a():\n  return 1\ndef b():\n  return 2\n")
    path.write_text("def a():\n  return 1\n", encoding="utf-8")
    rep = ReloadReport()
    superreload(rec, rep)
    assert not hasattr(mod, "b")
    assert any("scope=app" in w for w in rep.warnings)


def test_hot_preserve_restores_old_value(loader):
    mod, path, rec = loader("hr_hp", '__hot_preserve__ = ["S"]\nS = set()\n')
    mod.S.add("x")
    path.write_text('__hot_preserve__ = ["S"]\nS = set()\n', encoding="utf-8")
    rep = ReloadReport()
    superreload(rec, rep)
    assert "x" in mod.S   # old set object preserved across re-exec


def test_star_import_skips_stale_pruning(loader):
    mod, path, rec = loader("hr_star", "import os\nKEEP = 1\n")
    # Editing to a star import means new_names is None → stale pruning skipped.
    path.write_text("from os import *\n", encoding="utf-8")
    rep = ReloadReport()
    superreload(rec, rep)
    assert mod.KEEP == 1   # not pruned (could not determine new names)


def test_lru_cache_repatched_and_cleared(loader):
    mod, path, rec = loader(
        "hr_lru", "import functools\n@functools.lru_cache\ndef f():\n  return 1\n"
    )
    assert mod.f() == 1
    wrapper = mod.f
    path.write_text(
        "import functools\n@functools.lru_cache\ndef f():\n  return 2\n",
        encoding="utf-8",
    )
    rep = ReloadReport()
    superreload(rec, rep)
    assert mod.f is wrapper      # same wrapper object
    assert mod.f() == 2          # cache cleared, wrapped fn patched


# ---------------------------------------------------------------------------
# superreload — classes
# ---------------------------------------------------------------------------

def test_class_instance_survives_reload(loader):
    mod, path, rec = loader("hr_cls", "class C:\n  pass\n")
    inst = mod.C()
    path.write_text("class C:\n  def hi(self):\n    return 'hi'\n", encoding="utf-8")
    rep = ReloadReport()
    superreload(rec, rep)
    assert isinstance(inst, mod.C)     # identity preserved
    assert inst.hi() == "hi"           # new method visible on old instance


def test_class_method_removed_warns(loader):
    mod, path, rec = loader(
        "hr_clsrm", "class C:\n  def a(self):\n    return 1\n  def b(self):\n    return 2\n"
    )
    path.write_text("class C:\n  def a(self):\n    return 1\n", encoding="utf-8")
    rep = ReloadReport()
    superreload(rec, rep)
    assert not hasattr(mod.C, "b")
    assert mod.C().a() == 1
    assert any("scope=app" in w for w in rep.warnings)


def test_class_dunder_protected(loader):
    mod, path, rec = loader("hr_dun", "class C:\n  def m(self):\n    return 1\n")
    path.write_text("class C:\n  def m(self):\n    return 2\n", encoding="utf-8")
    rep = ReloadReport()
    superreload(rec, rep)
    # Standard dunders remain intact after migration.
    assert mod.C.__module__ == "hr_dun"
    assert mod.C().m() == 2


def test_slots_change_warns(loader):
    mod, path, rec = loader(
        "hr_slots", "class C:\n  __slots__ = ('x',)\n  def __init__(self):\n    self.x = 1\n"
    )
    path.write_text(
        "class C:\n  __slots__ = ('x', 'y')\n  def __init__(self):\n    self.x = 1\n",
        encoding="utf-8",
    )
    rep = ReloadReport()
    superreload(rec, rep)
    assert any("__slots__" in w for w in rep.warnings)


def test_property_shape_change_warns(loader):
    mod, path, rec = loader(
        "hr_prop", "class C:\n  @property\n  def v(self):\n    return 1\n"
    )
    path.write_text(
        "class C:\n  @property\n  def v(self):\n    return 2\n"
        "  @v.setter\n  def v(self, val):\n    pass\n",
        encoding="utf-8",
    )
    rep = ReloadReport()
    superreload(rec, rep)
    assert mod.C().v == 2
    assert any("property shape changed" in w for w in rep.warnings)


# ---------------------------------------------------------------------------
# topo sort
# ---------------------------------------------------------------------------

def test_on_reload_reregisters_window_verbs():
    """llm_bridge.__on_reload__ rebuilds the dispatch-table closures."""
    import llm_bridge
    from unittest.mock import MagicMock

    win = MagicMock()
    ctx = type("Ctx", (), {"window": win})()
    llm_bridge.__on_reload__(ctx)
    verbs = {c.args[0] for c in win.register_command.call_args_list}
    assert {
        "add-tab", "close-tab", "list-tabs", "set-active-tab",
        "toggle-chat-float", "show", "open-dataset",
    } <= verbs
    win.set_session_saver.assert_called_once()


# ---------------------------------------------------------------------------
# Analysis baseline: (dataset, name) keying + tab-open baseline (reviewer P1 R3)
# ---------------------------------------------------------------------------


def test_changed_analyses_baseline_on_open(monkeypatch, tmp_path):
    from devtools.hotreload import HotReloader

    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)
    ds, name = "dsx", "an1"
    d = tmp_path / ds / "analyses" / name
    d.mkdir(parents=True)
    af = d / "analysis.py"
    af.write_text("V = 1\n", encoding="utf-8")

    hr = HotReloader()
    pairs = {(ds, name)}

    # Before any baseline is registered, an open analysis is never reported
    # (no false "changed" at startup; reviewer P1 / reviewer P2-1).
    assert hr.changed_analyses(pairs) == []

    # Tab-open registers a clean baseline.
    hr.mark_analysis_clean(ds, name)
    assert hr.changed_analyses(pairs) == []

    # Edit after open → reported on the first reload (reviewer P1 R3).
    af.write_text("V = 2\n", encoding="utf-8")
    assert hr.changed_analyses(pairs) == [(ds, name)]

    # Tier-2 re-baseline clears it.
    hr.mark_analysis_clean(ds, name)
    assert hr.changed_analyses(pairs) == []


def test_changed_analyses_no_setdefault(monkeypatch, tmp_path):
    """A scanned-but-unregistered analysis must not seed a baseline implicitly."""
    from devtools.hotreload import HotReloader

    monkeypatch.setattr("config.get_dataset_dir", lambda ds: tmp_path / ds)
    ds, name = "dsy", "an2"
    d = tmp_path / ds / "analyses" / name
    d.mkdir(parents=True)
    (d / "analysis.py").write_text("V = 1\n", encoding="utf-8")

    hr = HotReloader()
    pairs = {(ds, name)}
    hr.changed_analyses(pairs)  # scan without registering
    assert (ds, name) not in hr.analyses


def test_toposort_dependency_first(tmp_path):
    (tmp_path / "a.py").write_text("import b\nx = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 1\n", encoding="utf-8")
    recs = [
        ModuleRecord("a", None, tmp_path / "a.py", ""),
        ModuleRecord("b", None, tmp_path / "b.py", ""),
    ]
    rep = ReloadReport()
    order = [r.name for r in _toposort(recs, rep)]
    assert order.index("b") < order.index("a")


# ---------------------------------------------------------------------------
# requires_app: Signal / __bases__ changes escalate to Tier 3 (Issue #83)
# ---------------------------------------------------------------------------


def test_signal_set_change_goes_to_requires_app(loader):
    mod, path, rec = loader(
        "hr_sig",
        "from PySide6.QtCore import Signal\nclass C:\n  a = Signal()\n",
    )
    path.write_text(
        "from PySide6.QtCore import Signal\nclass C:\n  a = Signal()\n  b = Signal()\n",
        encoding="utf-8",
    )
    rep = ReloadReport()
    superreload(rec, rep)
    assert any("Signal set changed" in w for w in rep.requires_app)
    assert not any("Signal set changed" in w for w in rep.warnings)


def test_init_body_change_stays_in_warnings(loader):
    # An __init__ body change is a *soft* warning, never escalated to requires_app
    # (only Signal-set / __bases__ changes escalate). A statement is added rather
    # than editing a constant on purpose: __init__ detection compares co_code only,
    # and a constant-only edit (self.x = 1 → 2) leaves co_code identical (LOAD_CONST
    # carries a co_consts *index*), so no warning fires — see the blind-spot test
    # below and issue #84.
    mod, path, rec = loader(
        "hr_init", "class C:\n  def __init__(self):\n    self.x = 1\n"
    )
    path.write_text(
        "class C:\n  def __init__(self):\n    self.x = 1\n    self.y = 2\n",
        encoding="utf-8",
    )
    rep = ReloadReport()
    superreload(rec, rep)
    assert any("__init__ changed" in w for w in rep.warnings)
    assert rep.requires_app == []


def test_init_constant_only_change_not_flagged(loader):
    # Documents the co_code-based detector's blind spot (issue #84): editing only a
    # constant default in __init__ leaves co_code identical (co_consts differs), so
    # the change is classified as neither a soft warning nor requires_app.
    # update_function still patches the constant into the live __init__ (new
    # instances see the new default; existing instances keep the old one). Left
    # unflagged deliberately — constant tweaks are the most common hot-reload edit
    # and warning on each would reintroduce the alarm fatigue #83 removes.
    mod, path, rec = loader(
        "hr_init_const", "class C:\n  def __init__(self):\n    self.x = 1\n"
    )
    path.write_text(
        "class C:\n  def __init__(self):\n    self.x = 2\n",
        encoding="utf-8",
    )
    rep = ReloadReport()
    superreload(rec, rep)
    assert not any("__init__ changed" in w for w in rep.warnings)
    assert rep.requires_app == []


def test_needs_app_reload_true_for_requires_app_only():
    rep = ReloadReport(requires_app=["C: Signal set changed — requires scope=app"])
    assert rep.warnings == []
    assert rep.needs_app_reload() is True
