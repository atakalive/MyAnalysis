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

import re
import tomllib
from pathlib import Path, PureWindowsPath

from common.i18n import tr
from common.paths import atomic_write_text, safe_resolve, validate_identifier_name

CONFIG_FILENAME = "myanalysis.toml"

KNOWN_FORMATS: tuple[str, ...] = ("csv_per_subdir", "custom")

_DEFAULTS = {"work_dir": "_work", "format": "csv_per_subdir"}

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
# format: データセットの読み込み形式。load_dataset() のディスパッチに使用。
#   - "csv_per_subdir" (既定): subdir_pattern/csv_name で glob → pd.read_csv
#   - "custom": analysis module の load() で固有実装。load_dataset() は使用不可
format = "csv_per_subdir"
"""


class ConfigUnreadableError(ValueError):
    """myanalysis.toml が在るのに読めない/空（Issue #96）。

    **空の TOML を「設定なし」と解釈してはならない。** tomllib は 0 バイトのファイルを
    正常な空テーブルとして parse するので、同期マウント上で truncate されると
    `work_dir` が黙って既定の `_work` に戻り、解析出力・session.json・chat_sessions が
    別ディレクトリへ書かれて既存の作業が「セッション無し」に見える。ファイルが存在する
    以上、空は破損の徴候であって設定内容ではない。

    `ValueError` サブクラスにしてあるのは、既存の呼び出し側が
    `except (..., ValueError, tomllib.TOMLDecodeError, ...)` で受けており
    （`TOMLDecodeError` 自体も ValueError）、1 データセットの破損でピッカー全体が
    落ちないようになっているため。
    """


def load_config(name: str) -> dict:
    """Read <dataset_dir>/myanalysis.toml, merged onto defaults.

    Missing file → defaults (no write; read is non-destructive). A parse error
    (tomllib.TOMLDecodeError) propagates — a corrupt config should surface
    immediately rather than be silently masked.

    Raises:
        ValueError: work_dir is present but not a string (hand-edit slipped in
                    e.g. ``work_dir = 123``).
        ConfigUnreadableError: the file exists but is empty / unreadable.
    """
    from config import get_dataset_dir
    config_path = get_dataset_dir(name) / CONFIG_FILENAME
    cfg = dict(_DEFAULTS)
    try:
        raw = config_path.read_bytes()
    except FileNotFoundError:
        raw = None                       # 本当に無い＝既定でよい（新規データセット）
    except OSError as e:
        raise ConfigUnreadableError(f"cannot read {config_path}: {e}") from e
    if raw is not None:
        if not raw.strip():
            raise ConfigUnreadableError(
                f"{config_path} is empty (possible truncation on the synced mount); "
                f"refusing to fall back to defaults, which would silently relocate work_dir")
        cfg.update(tomllib.loads(raw.decode("utf-8")))
    if not isinstance(cfg["work_dir"], str):
        raise ValueError(
            f"work_dir must be a string, got {type(cfg['work_dir']).__name__}: "
            f"{cfg['work_dir']!r}"
        )
    cfg.setdefault("format", _DEFAULTS["format"])
    if not isinstance(cfg["format"], str):
        raise ValueError(
            f"format must be a string, got {type(cfg['format']).__name__}: "
            f"{cfg['format']!r}"
        )
    if cfg["format"] not in KNOWN_FORMATS:
        raise ValueError(
            f"unknown format {cfg['format']!r}; known formats: {KNOWN_FORMATS}"
        )
    return cfg


def set_format(name: str, fmt: str) -> None:
    """myanalysis.toml の format 値を更新する（原子的に書き換える）。

    既存の format 行（ダブル/シングルクォート、行頭インデント許容）を検出して
    置換する。検出できなければ末尾に追記する（改行を保証してから）。

    Raises:
        ValueError: fmt が KNOWN_FORMATS に含まれない。
    """
    if fmt not in KNOWN_FORMATS:
        raise ValueError(
            f"unknown format {fmt!r}; known formats: {KNOWN_FORMATS}"
        )
    config_path = ensure_config(name)
    text = config_path.read_text(encoding="utf-8")
    new_line = f'format = "{fmt}"'
    pattern = re.compile(r"""^\s*format\s*=\s*(?:"[^"]*"|'[^']*')""", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(new_line, text, count=1)
    else:
        if not text.endswith("\n"):
            text += "\n"
        text += new_line + "\n"
    atomic_write_text(config_path, text)


def ensure_config(name: str) -> Path:
    """Create <dataset_dir>/myanalysis.toml from the template if absent; return its path.

    Called at save time. 既存ファイルは手編集の可能性があるので絶対に上書きしない。

    Issue #96: 以前は `open(config_path, "x")` の生書込だった。(a) 非 atomic なので書込中に
    失敗すると部分的な TOML が残り `load_config` が恒久的に例外を投げるようになる、
    (b) 同期マウント上の失敗が検証されない、(c) FileExistsError を握るので失敗が完全に
    見えない、の 3 点で危険だった。存在確認 → `atomic_write_text`（read-back 検証つき）に
    変更する。存在チェックと作成の間の TOCTOU は、同一マシン内では呼び出し側が
    exclusive_lock 下で呼ぶ経路が主で、かつ「既存を上書きしない」意図は
    atomic_write_text の冪等な書込（同内容なら no-op）で実害が出ない。
    """
    from config import get_dataset_dir
    config_path = get_dataset_dir(name) / CONFIG_FILENAME
    try:
        if config_path.exists():
            return config_path
    except OSError:
        return config_path        # 判定不能なら触らない（既存を守る方に倒す）
    atomic_write_text(config_path, _TEMPLATE)
    return config_path


def get_work_dir(name: str, *, create: bool = True) -> Path:
    """Resolve this dataset's output directory.

    With ``create=True`` (the save-time default): ensures myanalysis.toml exists,
    reads work_dir, validates it, resolves a relative value under the dataset dir
    (absolute is used as-is), creates the directory, and returns it. Default
    work_dir is "_work".

    With ``create=False`` (read path): performs the same validation + resolution
    but does NOT call ensure_config() and does NOT mkdir — so a read-only resolve
    never writes the myanalysis.toml sidecar or grows a work_dir tree on the
    synced drive.

    Raises:
        ValueError: work_dir is a drive-relative ("C:foo"), root-relative
                    ("\\foo", "/foo" on Windows semantics), or '..'-containing
                    path, or resolves outside the dataset dir.
    """
    from config import get_dataset_dir
    if create:
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
            raise ValueError(tr("workdir.drive_rel", work_dir=work_dir))
        if pw.root:
            raise ValueError(tr("workdir.root_rel", work_dir=work_dir))
        if ".." in pw.parts:
            raise ValueError(tr("workdir.dotdot", work_dir=work_dir))
        dataset_dir = get_dataset_dir(name)
        resolved = dataset_dir / p
        # defense-in-depth: re-check containment after resolution.
        if not safe_resolve(resolved).is_relative_to(safe_resolve(dataset_dir)):
            raise ValueError(tr("workdir.escape", resolved=resolved))
    if create:
        resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def analyses_root(dataset: str) -> Path:
    """Return <dataset_dir>/analyses/ (read-only; never created here)."""
    from config import get_dataset_dir
    return get_dataset_dir(dataset) / "analyses"


def analysis_file(dataset, name: str) -> Path:
    """Resolve <dataset_dir>/analyses/<name>/analysis.py (containment-checked).

    Validates `name`, ensures the resolved path stays under analyses_root, and
    returns it. Does NOT check existence (caller does) and does NOT mkdir.
    """
    validate_identifier_name(name, check_reserved=True)
    root = safe_resolve(analyses_root(dataset))
    f = safe_resolve(analyses_root(dataset) / name / "analysis.py")
    if not f.is_relative_to(root):
        raise ValueError(f"analysis.py escapes analyses/: {name!r}")
    return f


def analysis_out_dir(dataset, name: str, *, create: bool = True) -> Path:
    """Return <work_dir>/analyses/<name>/ for this dataset's analysis output."""
    validate_identifier_name(name, check_reserved=True)
    p = get_work_dir(dataset, create=create) / "analyses" / name
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def state_dir(dataset, name: str, *, create: bool = True) -> Path:
    """Return <work_dir>/analyses/<name>/state/."""
    d = analysis_out_dir(dataset, name, create=create) / "state"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def batch_dir(dataset, name: str, *, create: bool = True) -> Path:
    """Return <work_dir>/analyses/<name>/batch/."""
    d = analysis_out_dir(dataset, name, create=create) / "batch"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d
