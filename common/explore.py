"""Exploratory analysis helpers: load, summarise, and save figures/code to a dataset's work_dir."""
from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING

from common.mount_compat import install as _install_mount_compat
from common.paths import atomic_write_text, safe_resolve

_install_mount_compat()  # PIL/matplotlib の realpath(→WinError 1005) をマウント上で救う

if TYPE_CHECKING:
    from matplotlib.figure import Figure


_FIXED_EXCLUDE = frozenset({"analyses"})
_MAX_SUBDIRS = 50
_MAX_FILES = 50
_MAX_CSV_SAMPLES = 20
_MAX_CSV_BYTES_FOR_ROWS = 50 * 1024 * 1024  # 50 MiB: これを超える CSV は行数を数えない


def _count_csv_data_rows(path: Path) -> int | None:
    """CSV のデータ行数（ヘッダ1行を除く）を軽量に数える。

    改行バイトを O(1) メモリで数え（全行を pandas に載せない＝OOM 回避）、
    **末尾に改行が無いファイルでも最終行を数えるよう +1 補正**する。これにより
    クォート無しの通常 CSV では末尾改行の有無に依らず厳密一致する
    （例: `i,j\\n1,2` はヘッダ1 + データ1 = rows 1、末尾改行なしでも正）。
    ファイルが _MAX_CSV_BYTES_FOR_ROWS を超える場合は走査せず None を返す（I/O 上限）。
    近似の性質（best-effort の誤差境界を正直に開示）: クォート内改行を含む行、または
    末尾の空行がある場合は実データ行数と ±数行ずれうる（summary/サンプル用途では許容）。
    空ファイルは 0、読み取り失敗も None を返す。
    """
    try:
        if path.stat().st_size > _MAX_CSV_BYTES_FOR_ROWS:
            return None
        newlines = 0
        last_byte = b""
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                newlines += chunk.count(b"\n")
                last_byte = chunk[-1:]
    except OSError:
        return None
    if last_byte == b"":
        return 0  # 空ファイル: ヘッダもデータも無い
    # 物理行数 = 改行数 + (最終バイトが \n でなければ未終端の最終行1つ)。ヘッダ1行を引く。
    physical = newlines + (0 if last_byte == b"\n" else 1)
    return max(physical - 1, 0)


def load_dataset(
    name: str,
    *,
    subdir_pattern: str | None = None,
    csv_name: str | None = None,
    encoding: str | None = None,
) -> list[dict]:
    """dataset の CSV を読み込む。既定の読み込みパターンは持たない。

    csv_per_subdir では subdir_pattern と csv_name を**明示指定**する必要がある
    （どちらの形かはデータセットごとに異なるため）。まず dataset_summary(name) で
    実際のフォルダ構成と CSV 名を確認してから渡すこと。

    Returns list[dict] — 各 dict は {"name": str, "dir": Path, "df": DataFrame}。

    Raises:
        KeyError: name が DATASETS に未登録。
        RuntimeError: 現ホストのパスが DATASETS に未登録。
        ValueError: csv_per_subdir で subdir_pattern / csv_name が未指定（None）、
                    または未知の format。
        FileNotFoundError: dataset ディレクトリが存在しない、
                           またはマッチするセッションが0件。
        NotImplementedError: format="custom"（対応する analysis module の load() を使用）。
    """
    from config import get_dataset_dir
    import dataset_config
    root = get_dataset_dir(name)
    fmt = dataset_config.load_config(name)["format"]
    if fmt == "csv_per_subdir":
        if subdir_pattern is None or csv_name is None:
            raise ValueError(
                f"dataset {name!r} has no default load pattern. "
                f"Run dataset_summary({name!r}) to inspect the actual layout, then "
                f"pass the real folder pattern and csv filename, e.g. "
                f"load_dataset({name!r}, subdir_pattern=..., csv_name=...) — "
                f"or implement the analysis module's load()."
            )
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
    atomic_write_text(path, content)   # マウント上の truncate-in-place（0byte 化）を避ける
    return path


def dataset_summary(
    name: str,
    *,
    encoding: str | None = None,
    max_subdirs: int = _MAX_SUBDIRS,
    max_files: int = _MAX_FILES,
    max_csv_samples: int = _MAX_CSV_SAMPLES,
) -> dict:
    """データセット直下の実ディレクトリ構成を検査して報告する探索ツール。

    既定の読み込みパターンは仮定しない。フォルダ名パターンも CSV 名も決め打ちせず、
    データセットディレクトリ直下を歩いて実在の構成を返す。エージェントが「まずデータを
    見る」ための最初のステップ。format 分岐に依存しない。

    走査範囲と上限（過大走査を避ける。上限は引数で調整可）:
      - 直下サブディレクトリを name 昇順で最大 max_subdirs 件。各サブディレクトリにつき
        直下ファイル名を最大 max_files 件、直下のさらなるサブディレクトリ名（ネスト検知用）
        を最大 max_files 件。**再帰はしない**（2 階層目より深い CSV は見えない。深い構造は
        各 subdir の `subdirs` が非空であることを手掛かりに、より深い探索や
        format='custom' の load() 実装を検討すること）。
      - CSV ヘッダ／行数サンプルは最大 max_csv_samples 件。**サブディレクトリ内 CSV を優先**し
        （csv_per_subdir の主計測データがトップレベルの無関係 CSV に押し出されないため）、
        続いて直下 CSV を対象にする。
      - 除外: 設定済み work_dir（既定 `_work`。myanalysis.toml で変更された場合はその実値を
        dataset_config.get_work_dir(name, create=False) で解決して除外）と `analyses`
        ディレクトリ、および `.` 始まりの名前（dotfile/dotdir。サブディレクトリ内の
        ファイル/ディレクトリにも適用）。CSV 以外の直下ファイル（myanalysis.toml / meta.json 等）
        は subdirs にも csv_samples にも現れない（意図的。実データ発見に不要なため）。
      - 上限到達・読み取り失敗は stderr に warning を出す（silent truncation を避ける）。

    Returns:
        {
            "name": str,
            "path": str,                # 解決済みデータセットディレクトリ（絶対パス）
            "subdirs": [                # 直下サブディレクトリ（name 昇順、最大 max_subdirs 件）
                {"name": str,
                 "files": list[str],    # 直下ファイル名（dotfile 除外、昇順、最大 max_files 件）
                 "subdirs": list[str]}, # 直下サブディレクトリ名（ネスト検知、最大 max_files 件）
                ...
            ],
            "csv_samples": [
                {"path": str,           # データセットディレクトリからの相対パス（posix 区切り）
                 "columns": list[str],  # ヘッダ（pd.read_csv(nrows=0) で軽量取得）
                 "rows": int},          # データ行数（best-effort。巨大/失敗時は "rows" を省く）
                ...
            ],
        }

    旧 dataset_summary からの意図的な挙動変更:
      - 戻り値形を全面変更（旧 `sessions` / `format` / `note` キーは廃止）。
      - format 非依存。旧実装では format='custom' でデータセットディレクトリが存在しなくても
        メタ辞書を返していたが、本実装はディレクトリを実検査するため、存在しなければ
        **format に関わらず** FileNotFoundError を送出する（下記 Raises）。

    Raises:
        KeyError: name が DATASETS に未登録。
        RuntimeError: 現ホストのパスが DATASETS に未登録。
        FileNotFoundError: データセットディレクトリが存在しない（未マウント等）。
    """
    import sys
    import pandas as pd
    from config import get_dataset_dir
    import dataset_config

    root = get_dataset_dir(name)
    if not root.exists():
        raise FileNotFoundError(f"dataset directory not found: {root}")
    root_r = safe_resolve(root)

    # 除外集合: 固定の analyses + 設定済み work_dir のトップレベル成分。
    exclude = set(_FIXED_EXCLUDE)
    try:
        wd = safe_resolve(dataset_config.get_work_dir(name, create=False))
    except (OSError, ValueError) as e:
        exclude.add("_work")  # 設定読取り失敗 → 既定 work_dir 名を除外
        print(
            f"warning: could not resolve work_dir for {name!r}, "
            f"excluding default '_work': {e}",
            file=sys.stderr,
        )
    else:
        try:
            rel = wd.relative_to(root_r)
        except ValueError:
            rel = None  # work_dir がデータセット外 → root 直下に除外対象なし
        if rel is not None and rel.parts:
            exclude.add(rel.parts[0])

    entries = sorted(root.iterdir(), key=lambda p: p.name)
    dirs = [
        e for e in entries
        if e.is_dir() and not e.name.startswith(".") and e.name not in exclude
    ]
    top_csvs = [
        e for e in entries
        if e.is_file() and not e.name.startswith(".")
        and e.name.lower().endswith(".csv")
    ]
    if len(dirs) > max_subdirs:
        print(
            f"warning: {len(dirs)} subdirs under {root}; reporting first {max_subdirs}",
            file=sys.stderr,
        )

    subdirs: list[dict] = []
    subdir_csv_paths: list[Path] = []
    for d in dirs[:max_subdirs]:
        try:
            children = sorted(d.iterdir(), key=lambda p: p.name)
        except OSError as e:
            print(f"warning: cannot list {d}: {e}", file=sys.stderr)
            subdirs.append({"name": d.name, "files": [], "subdirs": []})
            continue
        files = [
            c.name for c in children
            if c.is_file() and not c.name.startswith(".")
        ]
        child_dirs = [
            c.name for c in children
            if c.is_dir() and not c.name.startswith(".")
        ]
        if len(files) > max_files:
            print(
                f"warning: {d} has {len(files)} files; listing first {max_files}",
                file=sys.stderr,
            )
        if len(child_dirs) > max_files:
            print(
                f"warning: {d} has {len(child_dirs)} subdirs; listing first {max_files}",
                file=sys.stderr,
            )
        files = files[:max_files]
        child_dirs = child_dirs[:max_files]
        subdirs.append({"name": d.name, "files": files, "subdirs": child_dirs})
        subdir_csv_paths += [d / f for f in files if f.lower().endswith(".csv")]

    # サブディレクトリ内 CSV を先に、続いて直下 CSV をサンプル対象にする。
    csv_paths = subdir_csv_paths + list(top_csvs)
    csv_samples: list[dict] = []
    truncated = False
    for csv in csv_paths:
        if len(csv_samples) >= max_csv_samples:
            truncated = True
            break
        try:
            columns = list(pd.read_csv(csv, nrows=0, encoding=encoding).columns)
        except (OSError, ValueError) as e:
            print(f"warning: cannot read header of {csv}: {e}", file=sys.stderr)
            continue
        sample: dict = {"path": csv.relative_to(root).as_posix(), "columns": columns}
        rows = _count_csv_data_rows(csv)
        if rows is not None:
            sample["rows"] = rows
        csv_samples.append(sample)
    if truncated:
        print(
            f"warning: {len(csv_paths)} csv files under {root}; "
            f"sampling first {max_csv_samples}",
            file=sys.stderr,
        )

    return {
        "name": name,
        "path": str(root),
        "subdirs": subdirs,
        "csv_samples": csv_samples,
    }
