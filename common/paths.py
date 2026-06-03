"""Filesystem path helpers. All paths are derived from __file__ so the module
location determines repo root. Move this file → repo_root changes accordingly."""
import re
from pathlib import Path

# 英小文字始まり + 英小文字・数字・アンダースコアのみ。
# コードインジェクション・Windows 予約文字・`.`/`_` 始まり・空白・パス区切りを排除する。
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Windows 予約デバイス名 (大文字小文字を区別しないため小文字でも拒否)。
_WINDOWS_RESERVED = frozenset(
    {
        "con", "prn", "aux", "nul",
        "com1", "com2", "com3", "com4", "com5",
        "com6", "com7", "com8", "com9",
        "lpt1", "lpt2", "lpt3", "lpt4", "lpt5",
        "lpt6", "lpt7", "lpt8", "lpt9",
    }
)


def validate_identifier_name(name: str, *, check_reserved: bool = True) -> None:
    """Raise ValueError if name is not a valid identifier.

    check_reserved=True: regex + Windows reserved name check (for analysis name).
    check_reserved=False: regex only (for dataset name).
    """
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"invalid name: {name!r} (must match ^[a-z][a-z0-9_]*$)"
        )
    if check_reserved and name.lower() in _WINDOWS_RESERVED:
        raise ValueError(
            f"invalid name: {name!r} is a Windows reserved device name"
        )


def validate_name(name: str) -> None:
    """Raise ValueError if name is not a simple directory name.

    Rejects empty strings, path separators (/ \\), relative-path
    components (. and ..), and NUL bytes.  This prevents constructing
    paths outside data/analyses/.
    """
    if not name or "/" in name or "\\" in name or name in (".", "..") or "\0" in name:
        raise ValueError(
            f"name must be a simple directory name without path separators, "
            f"relative components, or NUL bytes: {name!r}")


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
    validate_name(name)
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
