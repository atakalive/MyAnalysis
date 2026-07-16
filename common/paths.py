"""Filesystem path helpers. All paths are derived from __file__ so the module
location determines repo root. Move this file → repo_root changes accordingly."""
import os
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

    check_reserved=True は解析名用 = <dataset_dir>/analyses/<name>/ という実フォルダに
    なるため、汎用ハイジーンに加えて Windows のファイル名規則 (禁止文字・末尾ドット・
    予約デバイス名) も適用する。
    check_reserved=False はデータセット名用 = DATASETS の dict キーにしかならず
    パスにならないので、汎用ハイジーンのみ。
    """
    from common.i18n import tr
    if not name:
        raise ValueError(tr("validate.empty"))
    if "/" in name or "\\" in name:
        raise ValueError(tr("validate.path_sep", name=name))
    if name in (".", "..") or name.startswith("."):
        raise ValueError(tr("validate.leading_dot", name=name))
    for c in name:
        if c.isspace():
            raise ValueError(tr("validate.whitespace", name=name))
        if ord(c) < 32 or ord(c) == 127:
            raise ValueError(tr("validate.control_char", name=name))
    if check_reserved:
        bad = sorted(set(name) & _WINDOWS_FORBIDDEN)
        if bad:
            raise ValueError(
                tr("validate.forbidden_char", chars="".join(bad), name=name)
            )
        if name.endswith("."):
            raise ValueError(tr("validate.trailing_dot", name=name))
        if name.lower() in _WINDOWS_RESERVED:
            raise ValueError(tr("validate.reserved_name", name=name))


def validate_name(name: str) -> None:
    """Raise ValueError if name is not a simple directory name.

    Rejects empty strings, path separators (/ \\), relative-path
    components (. and ..), and NUL bytes.  This prevents constructing
    paths outside the analysis output tree.
    """
    from common.i18n import tr
    if not name or "/" in name or "\\" in name or name in (".", "..") or "\0" in name:
        raise ValueError(tr("validate.simple_name", name=name))


def repo_root() -> Path:
    """Return the repo root, derived from this file's location.

    Layout invariant: common/paths.py lives at <repo>/common/paths.py.
    """
    return Path(__file__).resolve().parent.parent


def i18n_dir() -> Path:
    """Return <repo>/i18n/ (committed catalogs; not created)."""
    return repo_root() / "i18n"


def safe_resolve(path) -> Path:
    """resolve() が使えない FS でも死なない絶対パス正規化（.resolve() の drop-in 代替）。

    Path.resolve() / os.path.realpath は Windows で GetFinalPathNameByHandle を呼ぶが、
    WinFsp/rclone/一部のネットワークマウントはこれを実装しておらず
    OSError [WinError 1005]「このボリュームは認識可能なファイルシステムではありません」を
    投げる。通常の FS では resolve() の結果をそのまま返し（symlink 解決・大文字小文字の
    正準化を保つ＝既存挙動・既存テスト不変）、resolve() が OSError のときだけ
    os.path.abspath（純 lexical 正規化：絶対化＋'..' の字句畳み込み、FS 問い合わせ無し）に
    フォールバックしてマウント上でも動く。ここが置換する封じ込めチェックには lexical 正規化で
    十分であり、symlink を辿らない分だけ（マウント外へ逃げる work_dir に対して）むしろ堅牢。
    """
    p = Path(path)
    try:
        return p.resolve()
    except OSError:
        return Path(os.path.abspath(p))
