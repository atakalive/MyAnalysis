"""Per-dataset settings: <dataset_dir>/myanalysis.toml.

config.py holds the dataset *registry* (which dataset lives where, per host).
This module holds *per-dataset* settings that travel with the data on the synced
drive. Today the only setting is the output location (`work_dir`, default
`_work`); the file is a sidecar the tools write mechanically.

Read is non-destructive: load_config() never writes. ensure_config()/get_work_dir()
write only at *save* time, and only the sidecar (myanalysis.toml) + the output
dir — never the measurement files themselves.
"""
from __future__ import annotations

import tomllib
from pathlib import Path, PureWindowsPath

CONFIG_FILENAME = "myanalysis.toml"

_DEFAULTS = {"work_dir": "_work"}

_TEMPLATE = """\
# MyAnalysis per-dataset settings (written by the tools; safe to hand-edit).
#
# work_dir: where analysis output (figures/, code/) is saved for this dataset.
#   - relative (e.g. "_work", "results", "out/figures") → resolved under this
#     dataset directory.
#   - absolute → used as-is (output may land outside the dataset directory).
#   '..', Windows drive-relative ("C:foo") and root-relative ("\\foo") paths are
#   rejected.
work_dir = "_work"
"""


def load_config(name: str) -> dict:
    """Read <dataset_dir>/myanalysis.toml, merged onto defaults.

    Missing file → defaults (no write; read is non-destructive). A parse error
    (tomllib.TOMLDecodeError) propagates — a corrupt config should surface
    immediately rather than be silently masked.

    Raises:
        ValueError: work_dir is present but not a string (hand-edit slipped in
                    e.g. ``work_dir = 123``).
    """
    from config import get_dataset_dir
    config_path = get_dataset_dir(name) / CONFIG_FILENAME
    cfg = dict(_DEFAULTS)
    try:
        with open(config_path, "rb") as f:
            cfg.update(tomllib.load(f))
    except FileNotFoundError:
        pass
    if not isinstance(cfg["work_dir"], str):
        raise ValueError(
            f"work_dir must be a string, got {type(cfg['work_dir']).__name__}: "
            f"{cfg['work_dir']!r}"
        )
    return cfg


def ensure_config(name: str) -> Path:
    """Create <dataset_dir>/myanalysis.toml from the template if absent; return its path.

    Called at save time. Uses exclusive creation (open mode "x") to avoid a
    TOCTOU race and to preserve a hand-edited file: an existing file raises
    FileExistsError, which we catch and ignore. tomllib cannot write, so the
    fixed template string is written directly (no new dependency).
    """
    from config import get_dataset_dir
    config_path = get_dataset_dir(name) / CONFIG_FILENAME
    try:
        with open(config_path, "x", encoding="utf-8") as f:
            f.write(_TEMPLATE)
    except FileExistsError:
        pass
    return config_path


def get_work_dir(name: str) -> Path:
    """Resolve (and create) this dataset's output directory. Called at save time.

    Ensures myanalysis.toml exists, reads work_dir, validates it, resolves a
    relative value under the dataset dir (absolute is used as-is), creates the
    directory, and returns it. Default "_work".

    Raises:
        ValueError: work_dir is a drive-relative ("C:foo"), root-relative
                    ("\\foo", "/foo" on Windows semantics), or '..'-containing
                    path, or resolves outside the dataset dir.
    """
    from config import get_dataset_dir
    ensure_config(name)
    work_dir = load_config(name)["work_dir"]

    p = Path(work_dir)
    pw = PureWindowsPath(work_dir)
    if p.is_absolute():
        resolved = p
    else:
        # PureWindowsPath is OS-independent, so these Windows-specific danger
        # patterns are rejected on POSIX too (where Path("C:foo").drive == "").
        if pw.drive:
            raise ValueError(
                f"work_dir にドライブ相対パスは使えません: {work_dir!r}"
            )
        if pw.root:
            raise ValueError(
                f"work_dir にルート相対パスは使えません: {work_dir!r}"
            )
        if ".." in pw.parts:
            raise ValueError(f"work_dir に '..' は使えません: {work_dir!r}")
        dataset_dir = get_dataset_dir(name)
        resolved = dataset_dir / p
        # defense-in-depth: re-check containment after resolution.
        if not resolved.resolve().is_relative_to(dataset_dir.resolve()):
            raise ValueError(
                f"work_dir が dataset dir 外に解決されました: {resolved}"
            )
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved
