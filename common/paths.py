"""Filesystem path helpers. All paths are derived from __file__ so the module
location determines repo root. Move this file → repo_root changes accordingly."""
from pathlib import Path


def _validate_name(name: str) -> None:
    """Raise ValueError if name is not a simple directory name.

    Rejects empty strings, path separators (/ \\), and relative-path
    components (. and ..).  This prevents constructing paths outside
    data/analyses/.
    """
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(
            f"name must be a simple directory name without path separators "
            f"or relative components: {name!r}")


def repo_root() -> Path:
    """Return the repo root, derived from this file's location.

    Layout invariant: common/paths.py lives at <repo>/common/paths.py.
    """
    return Path(__file__).resolve().parent.parent


def analyses_root() -> Path:
    return repo_root() / "analyses"


def analysis_out_dir(name: str) -> Path:
    """Return data/analyses/<name>/, creating it if missing.

    name must be a simple directory name (no /, \\, ., or ..).
    """
    _validate_name(name)
    p = repo_root() / "data" / "analyses" / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_dir(name: str) -> Path:
    """Return data/analyses/<name>/state/, creating it if missing."""
    p = analysis_out_dir(name) / "state"
    p.mkdir(parents=True, exist_ok=True)
    return p


def batch_dir(name: str) -> Path:
    """Return data/analyses/<name>/batch/, creating it if missing."""
    p = analysis_out_dir(name) / "batch"
    p.mkdir(parents=True, exist_ok=True)
    return p
