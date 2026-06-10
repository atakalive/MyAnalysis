"""Filesystem path helpers. All paths are derived from __file__ so the module
location determines repo root. Move this file → repo_root changes accordingly."""
from pathlib import Path

# Windows のファイル名に使えない文字。パス区切り (/ \) と制御文字は別途・汎用で拒否。
_WINDOWS_FORBIDDEN = set('<>:"|?*')

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
    """Raise ValueError if name is unsafe as a dataset / analysis name.

    拒否リスト方式。英大小文字・非ASCII (日本語等)・任意位置の数字・'-'・'_'
    は許可し、本当に危険なものだけ拒否する。

    check_reserved=True は解析名用 = analyses/<name>/ という実フォルダになるため、
    汎用ハイジーンに加えて Windows のファイル名規則 (禁止文字・末尾ドット・予約
    デバイス名) も適用する。
    check_reserved=False はデータセット名用 = DATASETS の dict キーにしかならず
    パスにならないので、汎用ハイジーンのみ。
    """
    if not name:
        raise ValueError("名前を入力してください。")
    if "/" in name or "\\" in name:
        raise ValueError(f"名前にパス区切り文字 (/ \\) は使えません: {name!r}")
    if name in (".", "..") or name.startswith("."):
        raise ValueError(f"名前を '.' で始めることはできません: {name!r}")
    for c in name:
        if c.isspace():
            raise ValueError(f"名前に空白文字は使えません: {name!r}")
        if ord(c) < 32 or ord(c) == 127:
            raise ValueError(f"名前に制御文字は使えません: {name!r}")
    if check_reserved:
        bad = sorted(set(name) & _WINDOWS_FORBIDDEN)
        if bad:
            raise ValueError(
                f"名前に Windows のファイル名で使えない文字 {''.join(bad)} "
                f"が含まれています: {name!r}"
            )
        if name.endswith("."):
            raise ValueError(f"名前を '.' で終えることはできません: {name!r}")
        if name.lower() in _WINDOWS_RESERVED:
            raise ValueError(f"{name!r} は Windows の予約デバイス名のため使えません。")


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
