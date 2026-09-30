"""解析モジュール生成器 (オンボーディング雛形)。

使用法:
    python -m newanalysis <name> --dataset <key>

`<dataset_dir>/analyses/<name>/` を作成し、標準パターン
(build_export_figs + 注釈ハンドラ + connect_annotations + attach_tab) を内包する
`analysis.py` と `README.md` を書き出す。生成物は同期ドライブ側に置かれ、
リポジトリへのコミット対象外。

テンプレートは本ファイル内の in-code 文字列で持つ (`analyses/_template/` は作らない)。
理由: 解析一覧の列挙 (gui/tools.py の list_analyses 等) が当該データセットの
analyses/*/analysis.py を glob するため、テンプレートディレクトリが一覧に出てしまう。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from common.paths import atomic_write_text, validate_identifier_name
import dataset_config
from dataset_config import KNOWN_FORMATS


_LOAD_COMMENT_CSV = '''\
    # TODO: 実データの読み込みに差し替えてください。
    # 既定の読み込みパターンはありません。まず dataset_summary("<your_dataset_key>") で
    # 実際のフォルダ構成と CSV 名を確認し、<pattern> / <filename>.csv を実データに合わせて埋めます。
    # from config import get_dataset_dir
    # from common.loaders import load_csv_per_subdir
    # sessions = load_csv_per_subdir(
    #     root=get_dataset_dir("<your_dataset_key>"),
    #     subdir_pattern="<pattern>",
    #     csv_name="<filename>.csv",
    # )
    # return {"sessions": sessions}'''

_LOAD_COMMENT_CUSTOM = '''\
    # TODO: データセット固有の読み込み処理を実装してください。
    # このデータセットは format="custom" です。load_dataset() は使用できません。
    # from config import get_dataset_dir
    # root = get_dataset_dir(DATASET)
    # 以下にデータ読み込みを実装:'''

_LOAD_COMMENTS = {
    "csv_per_subdir": _LOAD_COMMENT_CSV,
    "custom": _LOAD_COMMENT_CUSTOM,
}


_ANALYSIS_TEMPLATE = '''\
"""__GEN_NAME__ 解析モジュール。

python -m newanalysis で生成された雛形。未編集でも GUI で開ける／export できる
(runnable-empty 原則)。実データへの依存は TODO コメントで例示のみ。
"""

from __future__ import annotations
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget
    from gui.tab import AnalysisTab
    from matplotlib.figure import Figure

import llm_bridge

NAME = "__GEN_NAME__"
__GEN_DATASET_LINE__


def load() -> dict[str, Any]:
    """解析データを読み込む。

    GUI の add-tab コマンドおよび python -m export の両方から呼ばれる
    共有ローダー。close → 再 open で同一プロセス内で再実行される。
    キャッシュが必要ならこの関数内で自前のメモ化を実装すること。
    GUI 依存の副作用を入れないこと (export が壊れる)。
    """
__GEN_LOAD_COMMENT__
    return {"rows": []}


def build_export_figs(data: dict[str, Any]) -> dict[str, Figure]:
    """ヘッドレス PNG 書き出し用の Figure を返す。python -m export から呼ばれる。"""
    # TODO: 解析図の生成を実装してください。例:
    # import matplotlib.pyplot as plt
    # fig, ax = plt.subplots()
    # ax.plot(...)
    # return {"overview.png": fig}
    return {}


def build_tab(parent: QWidget | None, data: dict[str, Any]) -> AnalysisTab:
    """AnalysisTab を組み立てて返す。llm_bridge の配線込み。"""
    from PySide6.QtWidgets import QLabel
    from gui.tab import AnalysisTab

    tab = AnalysisTab(name=NAME, parent=parent)
    # パネルキーはタブ内で一意であること。
    # 複数パネルの配置は MyAnalysis の docs/analysis_module_ja.md（AnalysisTab.add_panel）を参照。
    tab.add_panel("main", QLabel("TODO: パネルを実装"), "left")

    def _get_state() -> dict:
        return {"status": "placeholder"}

    def _annotations_handler(ann: dict) -> None:
        if not isinstance(ann, dict):
            return
        markers = ann.get("markers", [])
        notes = ann.get("notes", [])
        if not isinstance(markers, list) or not isinstance(notes, list):
            return
        # TODO: パネル実装後にマーカー/ノートの描画を配線。例:
        # traj.clear_markers()
        # img.clear_notes()
        # for m in markers: traj.add_marker(...)
        # for n in notes: img.add_note(...)

    # connect_annotations は attach_tab より前に呼ぶこと。
    # attach_tab 内で annotations.start_watcher(tab) が起動し、初回読み出し時に
    # tab.apply_annotations() を呼ぶ。ハンドラ未接続だと初回適用が空振りする。
    tab.connect_annotations(_annotations_handler)
    tab._watchers = llm_bridge.attach_tab(tab, _get_state)
    tab.dispatch_command("refresh-state")
    return tab


# ホットリロード Tier 2 (reload scope=tab) の状態復元フック (任意)。
# 実装すると、リロード時に旧タブの state (current.json) を新タブへ復元できる。
# 未実装なら build_tab の初期状態で表示される (state.json と UI は常に一致)。
# 実装する場合はコメントを外し、viewbox/selection 等の復元ロジックを書くこと:
#
# def apply_state(tab, state):
#     """旧タブの捕捉 state を新タブへ適用する。例外を出すと旧タブが保持される。"""
#     sel = state.get("selection")
#     if sel is not None:
#         ...  # tab のパネルに選択を反映
'''


_README_TEMPLATE = '''\
# __GEN_NAME__

## 目的

TODO: この解析の目的を記述してください。

## データ

TODO: データセット構造・ディレクトリ構成・CSV カラム等を記述してください。

## 実行

```bash
# GUI 起動
python tool.py
# → チャットエージェント経由、または CLI で解析タブを追加:
#   python -m llm_bridge window add-tab name=__GEN_NAME__ dataset=<dataset>

# ヘッドレスエクスポート
python -m export <dataset> __GEN_NAME__
```

## CLI verb

TODO: tab.register_command で登録するカスタムコマンドを記述してください。

## 出力

- `_work/analyses/__GEN_NAME__/state/current.json` — GUI 現選択
- `_work/analyses/__GEN_NAME__/state/current_view.png` — snapshot 実行後に生成される GUI 現表示 PNG
- `_work/analyses/__GEN_NAME__/state/annotations.json` — markers + notes

## 知見メモ

(解析から得られた知見をここに追記)
'''


def _render_analysis(name: str, dataset: str, fmt: str = "csv_per_subdir") -> str:
    if fmt not in KNOWN_FORMATS:
        raise ValueError(
            f"unknown format {fmt!r}; known formats: {KNOWN_FORMATS}"
        )
    dataset_line = f'DATASET = "{dataset}"'
    return (
        _ANALYSIS_TEMPLATE
        .replace("__GEN_LOAD_COMMENT__", _LOAD_COMMENTS[fmt])
        .replace("__GEN_DATASET_LINE__", dataset_line)
        .replace("__GEN_NAME__", name)
    )


def _render_readme(name: str) -> str:
    return _README_TEMPLATE.replace("__GEN_NAME__", name)


def create_analysis(
    name: str, dataset: str, fmt: str = "csv_per_subdir",
) -> tuple[Path, Path]:
    """Create analysis scaffold under the dataset dir. Returns (analysis_py, readme).

    Raises ValueError (invalid name/dataset), FileExistsError (already exists),
    KeyError (unregistered dataset), RuntimeError (no host path for dataset).
    Rolls back partial creation on any exception. `dataset` is required.
    """
    validate_identifier_name(name, check_reserved=True)
    validate_identifier_name(dataset, check_reserved=False)

    target_dir = dataset_config.analyses_root(dataset) / name
    if target_dir.exists():
        raise FileExistsError(f"{dataset}/analyses/{name}/ already exists")

    target_dir.mkdir(parents=True)
    try:
        analysis_path = target_dir / "analysis.py"
        readme_path = target_dir / "README.md"
        atomic_write_text(analysis_path, _render_analysis(name, dataset, fmt))
        atomic_write_text(readme_path, _render_readme(name))
    except BaseException:
        # 不完全な生成物が残ると次回の「already exists」チェックを妨げる。
        # ignore_errors=True はロールバック自体の失敗 (ファイルロック等) で
        # クラッシュしないための防御。削除対象は今回作成した <dataset>/analyses/<name>/ のみ。
        shutil.rmtree(target_dir, ignore_errors=True)
        raise

    return analysis_path, readme_path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m newanalysis",
        description="解析モジュール雛形を生成する",
    )
    parser.add_argument(
        "name", help="解析ディレクトリ名 (<dataset_dir>/analyses/<name>/ として作成)"
    )
    parser.add_argument(
        "--dataset", required=True, help="config.DATASETS のキー文字列 (必須)"
    )
    parser.add_argument(
        "--format", default="csv_per_subdir",
        choices=("csv_per_subdir", "custom"),
        help="Dataset format (default: csv_per_subdir)",
    )
    args = parser.parse_args(argv)

    name: str = args.name.strip()
    dataset: str = args.dataset

    try:
        create_analysis(name, dataset, fmt=args.format)
    except (ValueError, FileExistsError, KeyError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"created {dataset}/analyses/{name}/analysis.py")
    print(f"created {dataset}/analyses/{name}/README.md")


if __name__ == "__main__":
    main()
