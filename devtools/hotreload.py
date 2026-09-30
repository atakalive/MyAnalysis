"""In-process hot reload core (Qt-independent, unit-testable).

The reloader patches already-imported repo modules *in place* (superreload
style): functions get their `__code__` transplanted onto the live function
object, classes get their methods migrated onto the live class object. Because
the live objects keep their identity, existing instances, `isinstance` checks,
Qt signal connections, and closures captured by long-lived callbacks all keep
working against the new code.

This module knows nothing about Qt. `devtools/qt_integration.py` drives it.

Key public surface:
    HotReloader(root)            — tracks repo modules + analyses, performs reloads
    HotReloader.reload(ctx)      — Tier 1: patch all changed modules, return report
    HotReloader.changed_analyses() — Tier 2 candidate detection
    purge_project_modules(root)  — Tier 3: drop repo modules from sys.modules
    is_project_module(name)      — Tier 3: snapshot predicate
    ReloadReport                 — outcome record (reloaded/removed/warnings/...)

Limits (documented in CLAUDE.md / the issue):
    - Stale-name removal only deletes from a module/class `__dict__`; external
      references (Qt signal connections, closures) keep the OLD callable. Such
      removals are reported in `warnings` with a scope=app recommendation.
    - Nested closures already created by a long-lived call (e.g. the command
      watcher's `_drain`) keep old code even after the enclosing function is
      patched — flagged as a structural warning.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import FunctionType, ModuleType

from common.paths import repo_root
import dataset_config

# ---------------------------------------------------------------------------
# Skip sets
# ---------------------------------------------------------------------------

# Names never removed from a module __dict__ during stale-name pruning.
_MODULE_DUNDERS = frozenset({
    "__name__", "__file__", "__loader__", "__spec__", "__package__",
    "__path__", "__builtins__", "__doc__", "__cached__",
})

# Class attributes never touched when migrating methods.
_CLASS_SKIP = frozenset({"__dict__", "__weakref__", "staticMetaObject"})

# Class attributes never removed during class-level stale pruning.
_CLASS_DUNDER_SKIP = frozenset({
    "__module__", "__qualname__", "__doc__", "__annotations__", "__slots__",
    "__dict__", "__weakref__", "staticMetaObject",
    "__dataclass_fields__", "__dataclass_params__",
})

# Directory names (compared case-insensitively) marking a third-party install
# tree. A module below one of these under the repo root (e.g. an in-repo
# `.venv/Lib/site-packages/`) is not a repo module. Issue #101 E-1.
_THIRD_PARTY_DIRS = frozenset({"site-packages", "dist-packages"})


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class ReloadReport:
    """Outcome of a Tier 1 reload pass."""
    reloaded: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)        # "module:name"
    skipped_syntax: list[str] = field(default_factory=list)  # "path:line: msg"
    failed: list[str] = field(default_factory=list)          # "module: err" (rolled back)
    warnings: list[str] = field(default_factory=list)
    requires_app: list[str] = field(default_factory=list)  # cannot-patch structural changes (Signal/__bases__)
    analyses_changed: list[tuple[str, str]] = field(default_factory=list)  # (dataset, name)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.skipped_syntax

    def needs_app_reload(self) -> bool:
        """True if any structural change suggests escalating to Tier 3 (app)."""
        return bool(self.warnings or self.requires_app)

    def summary(self) -> str:
        parts: list[str] = []
        if self.reloaded:
            parts.append(f"reloaded {len(self.reloaded)}: {', '.join(self.reloaded)}")
        if self.removed:
            parts.append(f"removed {len(self.removed)}: {', '.join(self.removed)}")
        if self.analyses_changed:
            parts.append(
                "analyses changed (use scope=tab): "
                + ", ".join(f"{ds}/{nm}" for ds, nm in self.analyses_changed)
            )
        if self.skipped_syntax:
            parts.append("SYNTAX ERROR — nothing reloaded:\n  " + "\n  ".join(self.skipped_syntax))
        if self.failed:
            parts.append("FAILED (rolled back): " + "; ".join(self.failed))
        if self.requires_app:
            parts.append(
                "‼ requires scope=app (cannot patch):\n  "
                + "\n  ".join(self.requires_app)
            )
        if self.warnings:
            parts.append(
                "⚠ structural changes — scope=app recommended:\n  "
                + "\n  ".join(self.warnings)
            )
        if not parts:
            return "no changes"
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Module record + discovery
# ---------------------------------------------------------------------------

@dataclass
class ModuleRecord:
    name: str
    module: ModuleType
    path: Path
    sha1: str


def _file_sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _interpreter_prefixes_inside(root: Path) -> list[Path]:
    """Interpreter prefixes strictly inside *root* (an in-repo venv / embedded
    Python). A prefix equal to or above *root* is ignored — it would otherwise
    exclude every repo module."""
    out: list[Path] = []
    for raw in (sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix):
        if not raw:
            continue
        try:
            pre = Path(raw).resolve()
        except (OSError, ValueError):
            continue
        if pre != root and pre.is_relative_to(root):
            out.append(pre)
    return out


def is_project_module(name: str, root: Path | None = None) -> bool:
    """True if sys.modules[name] is a repo module eligible for reload/purge.

    Excludes `__main__` (entry script — Tier 1 unreachable), `devtools.*` (the
    reloader itself), and third-party code that merely lives under the repo:
    anything below a `site-packages` / `dist-packages` directory (an in-repo
    `.venv`) or below an interpreter prefix strictly inside the repo (an
    in-repo embedded Python). Issue #101 E-1.
    """
    if name == "__main__" or name == "devtools" or name.startswith("devtools."):
        return False
    mod = sys.modules.get(name)
    if mod is None:
        return False
    f = getattr(mod, "__file__", None)
    if not f:
        return False
    root = (root or repo_root()).resolve()
    try:
        p = Path(f).resolve()
        if not p.is_relative_to(root):
            return False
        rel = p.relative_to(root)
    except (ValueError, OSError):
        return False
    if any(part.lower() in _THIRD_PARTY_DIRS for part in rel.parts):
        return False
    return not any(p.is_relative_to(pre) for pre in _interpreter_prefixes_inside(root))


def purge_project_modules(root: Path | None = None) -> list[str]:
    """Drop every repo module from sys.modules (Tier 3). Returns purged names.

    Running frames keep their old globals via the frame's reference, so they run
    to completion safely; only the *name → module* mapping is removed so the next
    import re-executes fresh source.
    """
    root = (root or repo_root()).resolve()
    purged = []
    for name in list(sys.modules):
        if is_project_module(name, root):
            del sys.modules[name]
            purged.append(name)
    return purged


# ---------------------------------------------------------------------------
# AST top-level name extraction
# ---------------------------------------------------------------------------

def _extract_toplevel_names(source: str) -> set[str] | None:
    """Return the set of names bound in a module's top-level scope.

    Static (no exec). Over-approximates on purpose: collecting too many names is
    safe (a still-wanted name is kept), collecting too few is dangerous (a wanted
    name gets pruned). Returns None when `from x import *` appears — the imported
    name set is not statically knowable, so the caller skips stale pruning.

    Recurses into compound statement bodies (if/try/with/for/while) so
    conditional and try-guarded imports are captured, but NOT into nested
    function/class bodies (those open new scopes).
    """
    tree = ast.parse(source)
    names: set[str] = set()
    star = False

    def add_target(t: ast.AST) -> None:
        if isinstance(t, ast.Name):
            names.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                add_target(e)
        elif isinstance(t, ast.Starred):
            add_target(t.value)
        # ast.Attribute / ast.Subscript targets bind no new top-level name.

    def add_walrus(node: ast.AST | None) -> None:
        if node is None:
            return
        for n in ast.walk(node):
            if isinstance(n, ast.NamedExpr) and isinstance(n.target, ast.Name):
                names.add(n.target.id)

    def visit(body: list[ast.stmt]) -> None:
        nonlocal star
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)  # do not descend — new scope
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    add_target(t)
                add_walrus(node.value)
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name):
                    names.add(node.target.id)
                add_walrus(node.value)
            elif isinstance(node, ast.AugAssign):
                add_target(node.target)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    names.add(a.asname or a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if a.name == "*":
                        star = True
                    else:
                        names.add(a.asname or a.name)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                add_target(node.target)
                visit(node.body)
                visit(node.orelse)
            elif isinstance(node, ast.While):
                add_walrus(node.test)
                visit(node.body)
                visit(node.orelse)
            elif isinstance(node, ast.If):
                add_walrus(node.test)
                visit(node.body)
                visit(node.orelse)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        add_target(item.optional_vars)
                visit(node.body)
            elif isinstance(node, ast.Try):
                visit(node.body)
                visit(node.orelse)
                visit(node.finalbody)
                for h in node.handlers:
                    if h.name:
                        names.add(h.name)
                    visit(h.body)

    visit(tree.body)
    if star:
        return None
    return names


# ---------------------------------------------------------------------------
# Object reconciliation
# ---------------------------------------------------------------------------

def _is_lru_cache(obj: object) -> bool:
    return hasattr(obj, "__wrapped__") and hasattr(obj, "cache_clear")


def _is_signal(obj: object) -> bool:
    """Duck-detect a PySide6 Signal / SignalInstance without importing Qt."""
    t = type(obj)
    return t.__name__ in ("Signal", "SignalInstance") and \
        "PySide" in (getattr(t, "__module__", "") or "")


def update_function(old: FunctionType, new: FunctionType) -> bool:
    """Transplant new code onto the live `old` function object.

    Returns True if patched in place; False if the closure free-variable shape
    differs (the `__code__` assignment raises ValueError) — the caller then
    replaces the binding with `new` and warns.
    """
    try:
        old.__code__ = new.__code__
    except ValueError:
        return False
    old.__defaults__ = new.__defaults__
    old.__kwdefaults__ = new.__kwdefaults__
    old.__doc__ = new.__doc__
    try:
        old.__annotations__ = new.__annotations__
    except (AttributeError, TypeError):
        pass
    try:
        old.__dict__.update(new.__dict__)
    except (AttributeError, TypeError):
        pass
    return True


def _nested_code_changes(old_code, new_code) -> list[str]:
    """Names of nested functions whose code object differs between old/new.

    A long-lived call (e.g. the command watcher's `_drain`) creates nested
    closures that survive a patch of the enclosing function — they keep old
    code. We can't tell which nested closures are externally held, so any change
    is reported (over-approximate, advisory).
    """
    def nested(code) -> dict[str, object]:
        return {
            c.co_name: c for c in code.co_consts
            if hasattr(c, "co_code")
        }

    old_nested = nested(old_code)
    new_nested = nested(new_code)
    changed: list[str] = []
    for name, oc in old_nested.items():
        nc = new_nested.get(name)
        if nc is None:
            changed.append(name)
            continue
        if (oc.co_code != nc.co_code
                or oc.co_consts != nc.co_consts
                or oc.co_argcount != nc.co_argcount
                or oc.co_freevars != nc.co_freevars):
            changed.append(name)
    return changed


def _property_shape(p: property) -> tuple[bool, bool, bool]:
    return (p.fget is not None, p.fset is not None, p.fdel is not None)


def update_class(old: type, new: type, report: ReloadReport) -> None:
    """Migrate methods/attributes from `new` onto the live `old` class object.

    Existing instances, isinstance checks, and signal connections survive
    because `old` keeps its identity.
    """
    qn = getattr(old, "__qualname__", old.__name__)

    # --- structural change detection (→ warnings / Tier 3 escalation) ---
    if old.__bases__ != new.__bases__:
        report.requires_app.append(f"{qn}: __bases__ changed — requires scope=app")
    if getattr(old, "__slots__", None) != getattr(new, "__slots__", None):
        report.warnings.append(f"{qn}: __slots__ changed — scope=app recommended")
    old_sigs = {k for k, v in old.__dict__.items() if _is_signal(v)}
    new_sigs = {k for k, v in new.__dict__.items() if _is_signal(v)}
    if old_sigs != new_sigs:
        report.requires_app.append(
            f"{qn}: Signal set changed ({old_sigs ^ new_sigs}) — requires scope=app"
        )
    oi, ni = old.__dict__.get("__init__"), new.__dict__.get("__init__")
    if isinstance(oi, FunctionType) and isinstance(ni, FunctionType):
        if oi.__code__.co_code != ni.__code__.co_code:
            report.warnings.append(
                f"{qn}.__init__ changed — existing instances keep old layout; "
                f"scope=app recommended"
            )

    # --- migrate new members onto old class ---
    for name, new_val in list(new.__dict__.items()):
        if name in _CLASS_SKIP or _is_signal(new_val):
            continue
        old_val = old.__dict__.get(name)
        if isinstance(new_val, (staticmethod, classmethod)) and \
                isinstance(old_val, type(new_val)):
            if not update_function(old_val.__func__, new_val.__func__):
                _set_class_attr(old, name, new_val, report)
        elif isinstance(new_val, property) and isinstance(old_val, property):
            if _property_shape(old_val) == _property_shape(new_val):
                for getter in ("fget", "fset", "fdel"):
                    ofn = getattr(old_val, getter)
                    nfn = getattr(new_val, getter)
                    if isinstance(ofn, FunctionType) and isinstance(nfn, FunctionType):
                        update_function(ofn, nfn)
            else:
                _set_class_attr(old, name, new_val, report)
                report.warnings.append(
                    f"{qn}.{name}: property shape changed — existing bound refs "
                    f"keep the old descriptor; scope=app recommended"
                )
        elif isinstance(new_val, FunctionType) and isinstance(old_val, FunctionType):
            changed = _nested_code_changes(old_val.__code__, new_val.__code__)
            if not update_function(old_val, new_val):
                _set_class_attr(old, name, new_val, report)
                report.warnings.append(
                    f"{qn}.{name}: closure shape changed — replaced; "
                    f"external refs keep old code; scope=app recommended"
                )
            elif changed:
                report.warnings.append(
                    f"{qn}.{name}: nested closure(s) {changed} changed — "
                    f"externally-held instances keep old code; scope=app recommended"
                )
        else:
            _set_class_attr(old, name, new_val, report)

    # --- class-level stale name removal ---
    for name in list(old.__dict__.keys()):
        if name in new.__dict__ or name in _CLASS_DUNDER_SKIP:
            continue
        val = old.__dict__.get(name)
        if _is_signal(val):
            continue
        try:
            delattr(old, name)
        except (AttributeError, TypeError):
            continue
        report.removed.append(f"{qn}:{name}")
        if callable(val) or isinstance(val, (property, staticmethod, classmethod)):
            report.warnings.append(
                f"{qn}.{name} removed — existing signal connections / bound-method "
                f"refs keep the old code; scope=app recommended"
            )

    # --- dataclass field drift ---
    if hasattr(old, "__dataclass_fields__") and hasattr(new, "__dataclass_fields__"):
        if set(old.__dataclass_fields__) != set(new.__dataclass_fields__):
            old.__dataclass_fields__ = dict(new.__dataclass_fields__)
            report.warnings.append(
                f"{qn}: dataclass fields changed — existing instances do not gain "
                f"new fields; scope=app recommended"
            )


def _set_class_attr(old: type, name: str, val: object, report: ReloadReport) -> None:
    try:
        setattr(old, name, val)
    except (AttributeError, TypeError):
        report.warnings.append(f"{old.__name__}.{name}: could not set attribute")


def update_lru_cache(old: object, new: object, report: ReloadReport) -> None:
    """Patch the wrapped function of an lru_cache wrapper and clear its cache."""
    ow, nw = getattr(old, "__wrapped__", None), getattr(new, "__wrapped__", None)
    if isinstance(ow, FunctionType) and isinstance(nw, FunctionType):
        update_function(ow, nw)
    try:
        old.cache_clear()
    except Exception:  # noqa: BLE001 — best effort
        pass


# ---------------------------------------------------------------------------
# superreload
# ---------------------------------------------------------------------------

def _remove_stale_module_names(
    module: ModuleType, old_dict: dict, new_names: set[str] | None,
    report: ReloadReport,
) -> None:
    if new_names is None:  # `from x import *` — cannot determine; skip entirely
        return
    preserve = set(module.__dict__.get("__hot_preserve__", []) or [])
    for name in list(old_dict.keys()):
        if name in new_names or name in _MODULE_DUNDERS or name in preserve:
            continue
        if name.startswith("__") and name.endswith("__"):
            continue  # keep all dunders (e.g. __on_reload__, __hot_preserve__)
        if name not in module.__dict__:
            continue
        val = module.__dict__.get(name)
        try:
            del module.__dict__[name]
        except (KeyError, TypeError):
            continue
        report.removed.append(f"{module.__name__}:{name}")
        if callable(val) or isinstance(val, (property, staticmethod, classmethod)):
            report.warnings.append(
                f"{module.__name__}.{name} removed (callable) — external refs "
                f"(Qt signal connection / closure capture) keep the old code; "
                f"scope=app recommended"
            )


def superreload(record: ModuleRecord, report: ReloadReport, ctx: object = None) -> bool:
    """Reload one module in place. Returns True on success, False if rolled back.

    Steps (mirrors the issue design):
      1. snapshot old __dict__ (rollback + reconcile source)
      2. statically extract the new top-level name set (AST, no exec)
      3. importlib.reload — on exception, full rollback
      4. reconcile same-named functions / classes / lru_cache wrappers in place
      5. prune module-level stale names (using the AST name set, not the dict)
      6. restore __hot_preserve__ values, run __on_reload__(ctx)
    """
    module = record.module
    source = record.path.read_text(encoding="utf-8")
    new_names = _extract_toplevel_names(source)
    old_dict = dict(module.__dict__)

    try:
        importlib.reload(module)
    except Exception as e:  # noqa: BLE001 — any failure must roll back cleanly
        module.__dict__.clear()
        module.__dict__.update(old_dict)
        report.failed.append(f"{record.name}: {e!r}")
        return False

    for name, old_obj in old_dict.items():
        if name not in module.__dict__:
            continue
        new_obj = module.__dict__[name]
        if old_obj is new_obj:
            continue
        if isinstance(old_obj, FunctionType) and isinstance(new_obj, FunctionType):
            changed = _nested_code_changes(old_obj.__code__, new_obj.__code__)
            if update_function(old_obj, new_obj):
                module.__dict__[name] = old_obj
                if changed:
                    report.warnings.append(
                        f"{module.__name__}.{name}: nested closure(s) {changed} "
                        f"changed — externally-held closures (e.g. watcher slots) "
                        f"keep old code; scope=app recommended"
                    )
            else:
                report.warnings.append(
                    f"{module.__name__}.{name}: closure shape changed — replaced "
                    f"with new object; external refs keep old code; scope=app recommended"
                )
        elif _is_lru_cache(old_obj) and _is_lru_cache(new_obj):
            update_lru_cache(old_obj, new_obj, report)
            module.__dict__[name] = old_obj
        elif isinstance(old_obj, type) and isinstance(new_obj, type):
            update_class(old_obj, new_obj, report)
            module.__dict__[name] = old_obj
        # else: a new object of a different kind — leave the reloaded binding.

    _remove_stale_module_names(module, old_dict, new_names, report)

    for name in module.__dict__.get("__hot_preserve__", []) or []:
        if name in old_dict:
            module.__dict__[name] = old_dict[name]

    hook = module.__dict__.get("__on_reload__")
    if callable(hook):
        try:
            hook(ctx)
        except Exception as e:  # noqa: BLE001
            report.warnings.append(f"{record.name}.__on_reload__ raised: {e!r}")

    report.reloaded.append(record.name)
    return True


# ---------------------------------------------------------------------------
# Topo sort of changed modules
# ---------------------------------------------------------------------------

def _toposort(records: list[ModuleRecord], report: ReloadReport) -> list[ModuleRecord]:
    """Order changed modules so imported-from deps reload before importers.

    Only intra-changeset, top-level imports create edges (function-local imports
    are ignored). On a cycle, falls back to the input order with a warning.
    """
    by_name = {r.name: r for r in records}
    names = set(by_name)
    deps: dict[str, set[str]] = {n: set() for n in names}
    for r in records:
        try:
            tree = ast.parse(r.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — syntax already guarded; be safe
            continue
        for node in tree.body:  # top-level only
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in names:
                        deps[r.name].add(a.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module in names:
                    deps[r.name].add(node.module)

    ordered: list[str] = []
    remaining = dict(deps)
    while remaining:
        ready = sorted(n for n, d in remaining.items() if not (d & set(remaining)))
        if not ready:  # cycle
            report.warnings.append(
                "import cycle among changed modules; using import order"
            )
            ordered.extend(r.name for r in records if r.name not in ordered)
            break
        for n in ready:
            ordered.append(n)
            del remaining[n]
    return [by_name[n] for n in ordered]


# ---------------------------------------------------------------------------
# HotReloader
# ---------------------------------------------------------------------------

class HotReloader:
    """Tracks repo modules + analyses and performs in-place reloads."""

    def __init__(self, root: Path | None = None):
        self.root = (root or repo_root()).resolve()
        self.records: dict[str, ModuleRecord] = {}
        self.analyses: dict[tuple[str, str], str] = {}  # (dataset, name) -> baseline sha1
        self.scan()

    # -- discovery --

    def scan(self) -> None:
        """Refresh the tracked module set from sys.modules.

        New modules get a baseline sha (not flagged changed). Existing records
        keep their baseline sha (so an edit still registers as a change) but
        refresh their module reference.
        """
        for name in list(sys.modules):
            if not is_project_module(name, self.root):
                continue
            mod = sys.modules[name]
            path = Path(mod.__file__).resolve()
            if name in self.records:
                self.records[name].module = mod
                self.records[name].path = path
                continue
            try:
                sha = _file_sha1(path)
            except OSError:
                continue
            self.records[name] = ModuleRecord(name, mod, path, sha)

    def _scan_analyses(self, open_pairs) -> dict[tuple[str, str], str]:
        """Return {(dataset, name): sha1} for the currently open analysis tabs.

        open_pairs: Iterable[(dataset, name)] = open analysis tabs. Tier-2 reload
        only applies to open tabs, so we never glob unopened analyses.
        """
        current: dict[tuple[str, str], str] = {}
        for ds, nm in open_pairs:
            try:
                current[(ds, nm)] = _file_sha1(dataset_config.analysis_file(ds, nm))
            except (OSError, ValueError, KeyError, RuntimeError):
                continue
        return current

    def changed_modules(self) -> list[ModuleRecord]:
        out: list[ModuleRecord] = []
        for rec in self.records.values():
            try:
                sha = _file_sha1(rec.path)
            except OSError:
                continue
            if sha != rec.sha1:
                out.append(rec)
        return out

    def changed_analyses(self, open_pairs) -> list[tuple[str, str]]:
        """Report open analyses whose sha diverged from their clean baseline.

        Only keys with an existing baseline (registered at tab-open time via
        mark_analysis_clean) are considered — baselines are never seeded
        implicitly here, so an analysis edited after it was opened reports on the
        first reload.
        """
        current = self._scan_analyses(open_pairs)
        return sorted(
            k for k, sha in current.items()
            if k in self.analyses and self.analyses[k] != sha
        )

    # -- reload --

    def mark_analysis_clean(self, dataset: str, name: str) -> None:
        """Record an analysis's current sha as the new baseline.

        Used both for tab-open baseline registration and Tier-2 post-reload
        re-baseline.
        """
        try:
            af = dataset_config.analysis_file(dataset, name)
            self.analyses[(dataset, name)] = _file_sha1(af)
        except (OSError, ValueError, KeyError, RuntimeError):
            self.analyses.pop((dataset, name), None)

    def reload(self, ctx: object = None) -> ReloadReport:
        """Tier 1: syntax-guard + patch every changed repo module in place."""
        report = ReloadReport()
        self.scan()
        window = getattr(ctx, "window", None)
        open_pairs = set()
        if window is not None:
            for tab in window.tabs():
                spec = getattr(tab, "session_spec", None) or {}
                if spec.get("kind") == "analysis":
                    ds = spec.get("dataset") or getattr(tab, "dataset", None)
                    if ds:
                        open_pairs.add((ds, tab.name))
        report.analyses_changed = self.changed_analyses(open_pairs)
        changed = self.changed_modules()
        if not changed:
            return report

        # Syntax guard: compile every changed file first; abort the whole batch
        # on any failure (old code keeps running).
        sources: dict[str, str] = {}
        for rec in changed:
            try:
                src = rec.path.read_text(encoding="utf-8")
                compile(src, str(rec.path), "exec")
                sources[rec.name] = src
            except SyntaxError as e:
                report.skipped_syntax.append(f"{e.filename}:{e.lineno}: {e.msg}")
            except OSError as e:
                report.skipped_syntax.append(f"{rec.path}: {e!r}")
        if report.skipped_syntax:
            return report

        for rec in _toposort(changed, report):
            ok = superreload(rec, report, ctx)
            if ok:
                try:
                    rec.sha1 = _file_sha1(rec.path)
                except OSError:
                    pass
        # Pick up modules newly imported during reload.
        self.scan()
        return report
