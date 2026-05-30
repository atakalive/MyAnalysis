"""解析モジュール生成器 (オンボーディング雛形)。

使用法:
    python -m newanalysis <name> [--dataset <key>]

`analyses/<name>/` を作成し、#13 で確立した標準パターン
(build_export_figs + 注釈ハンドラ + connect_annotations + attach_tab) を内包する
`analysis.py` と `README.md` を書き出す。生成物はリポジトリへのコミット対象外。

テンプレートは本ファイル内の in-code 文字列で持つ (`analyses/_template/` は作らない)。
理由: gui/window.py の _open_analysis() が analyses/*/analysis.py を glob で列挙するため、
テンプレートディレクトリがメニューに出てしまう。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys

from common.paths import analyses_root

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


def _validate(value: str, *, kind: str) -> None:
    """Validate name/dataset; print to stderr + sys.exit(1) on failure."""
    if not _NAME_RE.match(value):
        print(
            f"error: invalid {kind}: {value!r} "
            f"(must match ^[a-z][a-z0-9_]*$)",
            file=sys.stderr,
        )
        sys.exit(1)
    if value in _WINDOWS_RESERVED:
        print(
            f"error: invalid {kind}: {value!r} is a Windows reserved device name",
            file=sys.stderr,
        )
        sys.exit(1)


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
    # TODO: 実データの読み込みに差し替えてください。例:
    # from config import get_dataset_dir
    # from common.loaders import load_csv_per_subdir
    # sessions = load_csv_per_subdir(
    #     root=get_dataset_dir("<your_dataset_key>"),
    #     subdir_pattern="<pattern>",
    #     csv_name="<filename>.csv",
    # )
    # return {"sessions": sessions}
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
    # 複数パネルの配置例は example 実装を参照。
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
# → メニュー「ファイル → 解析を開く」→ 一覧から __GEN_NAME__ を選択

# ヘッドレスエクスポート
python -m export __GEN_NAME__
```

## CLI verb

TODO: tab.register_command で登録するカスタムコマンドを記述してください。

## 出力

- `data/analyses/__GEN_NAME__/state/current.json` — GUI 現選択
- `data/analyses/__GEN_NAME__/state/current_view.png` — snapshot 実行後に生成される GUI 現表示 PNG
- `data/analyses/__GEN_NAME__/state/annotations.json` — markers + notes

## 知見メモ

(解析から得られた知見をここに追記)
'''


def _render_analysis(name: str, dataset: str | None) -> str:
    if dataset:
        dataset_line = f'DATASET = "{dataset}"'
    else:
        dataset_line = (
            'DATASET = ""  # TODO: config.DATASETS のキーを設定してください'
        )
    return _ANALYSIS_TEMPLATE.replace("__GEN_DATASET_LINE__", dataset_line).replace(
        "__GEN_NAME__", name
    )


def _render_readme(name: str) -> str:
    return _README_TEMPLATE.replace("__GEN_NAME__", name)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m newanalysis",
        description="解析モジュール雛形を生成する",
    )
    parser.add_argument("name", help="解析ディレクトリ名 (analyses/<name>/ として作成)")
    parser.add_argument(
        "--dataset", default=None, help="config.DATASETS のキー文字列 (任意)"
    )
    args = parser.parse_args(argv)

    name: str = args.name.strip()
    dataset: str | None = args.dataset

    _validate(name, kind="name")
    if dataset is not None:
        _validate(dataset, kind="dataset")

    target_dir = analyses_root() / name
    if target_dir.exists():
        print(f"error: analyses/{name}/ already exists", file=sys.stderr)
        sys.exit(1)

    target_dir.mkdir(parents=True)
    try:
        analysis_path = target_dir / "analysis.py"
        readme_path = target_dir / "README.md"
        analysis_path.write_text(_render_analysis(name, dataset), encoding="utf-8")
        readme_path.write_text(_render_readme(name), encoding="utf-8")
    except BaseException:
        # 不完全な生成物が残ると次回の「already exists」チェックを妨げる。
        # ignore_errors=True はロールバック自体の失敗 (ファイルロック等) で
        # クラッシュしないための防御。削除対象は今回作成した analyses/<name>/ のみ。
        shutil.rmtree(target_dir, ignore_errors=True)
        raise

    print(f"created analyses/{name}/analysis.py")
    print(f"created analyses/{name}/README.md")
    if dataset:
        print("# config.py に未登録なら追記してください:")
        print(f'#   DATASETS["{dataset}"] = "<relative_path>"')


if __name__ == "__main__":
    main()
