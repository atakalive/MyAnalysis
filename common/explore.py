"""Exploratory analysis helpers: load, summarise, and save figures/code to a dataset's work_dir."""
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
        NotImplementedError: format="custom"（対応する analysis module の
                             load() を使用すること）。
        ValueError: 未知の format。
    """
    from config import get_dataset_dir
    import dataset_config
    root = get_dataset_dir(name)
    fmt = dataset_config.load_config(name)["format"]
    if fmt == "csv_per_subdir":
        from common.loaders import load_csv_per_subdir
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
    if fmt == "custom":
        raise NotImplementedError(
            f"dataset {name!r} uses format='custom'. Use the corresponding "
            "analysis module's load() function instead "
            "(see 'python -m llm_bridge list-analyses')."
        )
    raise ValueError(f"unknown format {fmt!r} for dataset {name!r}")


def save_fig(name: str, fig: Figure, label: str) -> Path:
    """図を <dataset work_dir>/figures/<label>.png に保存し、パスを返す。

    work_dir は per-dataset 設定 myanalysis.toml で決まる（既定 _work）。
    label のバリデーション: common.paths.validate_name を使用。
    figは save 後に close される（core/figures.save の仕様）。

    Returns: 保存先の Path（絶対パス）。

    Raises:
        ValueError: label が不正、または work_dir 設定が不正。
    """
    from common.paths import validate_name
    from dataset_config import get_work_dir
    from core.figures import save
    validate_name(label)
    path = get_work_dir(name) / "figures" / f"{label}.png"
    save(fig, path)
    return path


def save_code(name: str, label: str, content: str) -> Path:
    """ad-hocコードスニペットを <dataset work_dir>/code/<label>.py に保存し、パスを返す。

    work_dir は per-dataset 設定 myanalysis.toml で決まる（既定 _work）。
    label のバリデーション: common.paths.validate_name を使用。
    label にファイル拡張子は含めない（.py は自動付与）。
    content は UTF-8 で書き出す。既存ファイルは上書き。

    Returns: 保存先の Path（絶対パス）。

    Raises:
        ValueError: label が不正、または work_dir 設定が不正。
    """
    from common.paths import validate_name
    from dataset_config import get_work_dir
    validate_name(label)
    path = get_work_dir(name) / "code" / f"{label}.py"
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

    format="custom" の場合は CSV 詳細ではなくメタ情報のみの dict を返す
    （FileNotFoundError は送出しない）。

    Raises:
        load_dataset と同じ例外（csv_per_subdir で0件時 FileNotFoundError 含む）。
        ValueError: 未知の format。
    """
    from config import get_dataset_dir
    import dataset_config
    root = get_dataset_dir(name)
    fmt = dataset_config.load_config(name)["format"]
    if fmt == "csv_per_subdir":
        from common.loaders import load_csv_per_subdir
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
            "format": "csv_per_subdir",
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
    if fmt == "custom":
        return {
            "name": name,
            "path": str(root),
            "format": "custom",
            "note": "load_dataset() 不可。対応する analysis module の load() を"
                    "使用してください (python -m llm_bridge list-analyses で確認)。",
        }
    raise ValueError(f"unknown format {fmt!r} for dataset {name!r}")
