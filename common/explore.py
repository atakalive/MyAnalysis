"""Exploratory analysis helpers: load, summarise, and save scratch figures/code."""
from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from matplotlib.figure import Figure


def load_dataset(
    name: str,
    *,
    subdir_pattern: str = "session_*",
    csv_name: str = "samples.csv",
    encoding: str | None = None,
) -> list[dict]:
    """config.get_dataset_dir + load_csv_per_subdir を1呼び出しにまとめる。

    Returns list[dict] — 各 dict は {"name": str, "dir": Path, "df": DataFrame}。
    load_csv_per_subdir の戻り値そのまま。

    マッチするセッションが0件の場合は FileNotFoundError を送出する
    （パターン不一致によるサイレント障害を防止）。

    Raises:
        KeyError: name が DATASETS に未登録。
        RuntimeError: 現ホストのパスが DATASETS に未登録。
        FileNotFoundError: dataset ディレクトリが存在しない、
                           またはマッチするセッションが0件。
    """
    from config import get_dataset_dir
    from common.loaders import load_csv_per_subdir
    root = get_dataset_dir(name)
    sessions = load_csv_per_subdir(
        root=root,
        subdir_pattern=subdir_pattern,
        csv_name=csv_name,
        encoding=encoding,
    )
    if not sessions:
        raise FileNotFoundError(
            f"no sessions found under {root} "
            f"(pattern={subdir_pattern!r}, csv={csv_name!r})"
        )
    return sessions


def save_fig(fig: Figure, label: str) -> Path:
    """図を data/scratch/figures/<label>.png に保存し、パスを返す。

    label のバリデーション: common.paths.validate_name を使用。
    figは save 後に close される（core/figures.save の仕様）。

    Returns: 保存先の Path（絶対パス）。

    Raises:
        ValueError: label が不正。
    """
    from common.paths import scratch_dir, validate_name
    from core.figures import save
    validate_name(label)
    path = scratch_dir() / "figures" / f"{label}.png"
    save(fig, path)
    return path


def save_code(label: str, content: str) -> Path:
    """ad-hocコードスニペットを data/scratch/code/<label>.py に保存し、パスを返す。

    label のバリデーション: common.paths.validate_name を使用。
    content は UTF-8 で書き出す。既存ファイルは上書き。

    Returns: 保存先の Path（絶対パス）。

    Raises:
        ValueError: label が不正。
    """
    from common.paths import scratch_dir, validate_name
    validate_name(label)
    path = scratch_dir() / "code" / f"{label}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def dataset_summary(
    name: str,
    *,
    subdir_pattern: str = "session_*",
    csv_name: str = "samples.csv",
    encoding: str | None = None,
) -> dict:
    """データセットの構造要約を返す。

    Returns:
        {
            "name": str,           # データセット名
            "path": str,           # 解決済みフルパス
            "sessions": [          # load_csv_per_subdir の各エントリに対応
                {
                    "name": str,       # サブディレクトリ名
                    "rows": int,       # DataFrame の行数
                    "columns": list[str],  # カラム名のリスト
                    "dtypes": dict[str, str],  # {カラム名: dtype文字列}
                },
                ...
            ]
        }

    Raises: load_dataset と同じ例外（0件時 FileNotFoundError 含む）。
    """
    from config import get_dataset_dir
    from common.loaders import load_csv_per_subdir
    root = get_dataset_dir(name)
    sessions = load_csv_per_subdir(
        root=root,
        subdir_pattern=subdir_pattern,
        csv_name=csv_name,
        encoding=encoding,
    )
    if not sessions:
        raise FileNotFoundError(
            f"no sessions found under {root} "
            f"(pattern={subdir_pattern!r}, csv={csv_name!r})"
        )
    return {
        "name": name,
        "path": str(root),
        "sessions": [
            {
                "name": s["name"],
                "rows": len(s["df"]),
                "columns": list(s["df"].columns),
                "dtypes": {c: str(s["df"][c].dtype) for c in s["df"].columns},
            }
            for s in sessions
        ],
    }
