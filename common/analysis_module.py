"""Execute an analysis.py from source under a transient sys.modules entry (Issue #100 D-2)."""

import contextlib
import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

_MISSING = object()


def analysis_module_name(dataset: str, name: str) -> str:
    """sys.modules key for an analysis. Includes the dataset so that same-named
    analyses in different datasets get distinct names (diagnostics only — the
    entry is removed again when `analysis_module` exits)."""
    return f"_myanalysis_analysis_{dataset}__{name}"


@contextlib.contextmanager
def analysis_module(mod_name: str, path: Path, source: str) -> Iterator[ModuleType]:
    """Create a module for `path`, register it as sys.modules[mod_name], exec
    `source` in it and yield it. On exit (normal or exception) the previous
    sys.modules entry is restored, or the key is removed if there was none.

    Compiles from `source` instead of spec.loader.exec_module so that no stale
    .pyc is read and no __pycache__ is written next to the analysis (D-8).
    `source` is compiled as given: a leading U+FEFF (UTF-8 BOM) is a SyntaxError
    here, the same as in the GUI's other compile / ast.parse paths. export strips
    a BOM when it reads the file, to keep accepting BOM files as its old
    exec_module did.
    """
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None:
        raise ImportError(f"could not build module spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    prev = sys.modules.get(mod_name, _MISSING)
    sys.modules[mod_name] = mod
    try:
        exec(compile(source, str(path), "exec"), mod.__dict__)
        yield mod
    finally:
        # 自分が入れたエントリのときだけ戻す（途中で別物に差し替わっていたら触らない）
        if sys.modules.get(mod_name) is mod:
            if prev is _MISSING:
                del sys.modules[mod_name]
            else:
                sys.modules[mod_name] = prev
