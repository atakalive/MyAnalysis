# 自作の解析モジュール

[← README に戻る](../README_ja.md)

解析モジュールは `<データセット>/analyses/<解析名>/analysis.py` の Python 1 ファイルで、データと一緒に同期される。エージェントに作らせることも、自分で書くこともできる。

> 以下のコマンドはリポジトリ直下で、GUI と同じ venv の Python で実行する。

## 雛形の作成

```bash
python -m newanalysis <解析名> --dataset <データセット名> [--format csv_per_subdir|custom]
```

- `analysis.py` と `README.md` を作る。同名のフォルダがあると失敗する。
- `--format` はデータセットの設定を自動では読まない。`custom` のデータセットでは明示する。
- 雛形は編集しなくても開ける（中身は TODO 表示）。`load()` → `build_tab()` 内のパネル → 状態を返す関数 → 注釈ハンドラ → `build_export_figs()` の順に書き換える。

## 開き方

GUI に解析を開くメニューは無い。

- チャットで「`<解析名>` を開いて」と頼む。
- CLI: `python -m llm_bridge window add-tab name=<解析名> dataset=<データセット名> --wait 60`（先にそのデータセットを開いておく）。
- 保存したセッションに含まれていれば、データセットを開いたときに復元される。

## モジュールの約束

```python
NAME = "my_analysis"      # フォルダ名と同じにする（違うと "tab name mismatch" で開けない）
DATASET = "my_dataset"    # load() がデータを読むデータセット（通常は所在するデータセットと同じ名前）

def load() -> dict:                        # データ読み込み（GUI 部品は使わない）
    from config import get_dataset_dir
    root = get_dataset_dir(DATASET)        # この PC でのパス（絶対パスを直接書かない）
    ...
def build_tab(parent, data):               # GUI タブを作る（必須）
    from gui.tab import AnalysisTab
    import llm_bridge
    tab = AnalysisTab(name=NAME, parent=parent)
    ...                                    # パネルを配置
    tab._watchers = llm_bridge.attach_tab(tab, get_state)   # 必須
    return tab
def build_export_figs(data) -> dict:       # PNG 出力用 {"ファイル名.png": Figure}
    ...
def apply_state(tab, state):               # 任意: 前回の表示状態を復元
    ...
```

| 関数 | 必須か | 説明 |
|---|---|---|
| `build_tab(parent, data)` | GUI で必須 | `AnalysisTab` を作り、`llm_bridge.attach_tab(tab, get_state)` を呼んで返す |
| `load()` | GUI では任意、export では必須 | データを読んで返す。開くたびに実行される。Qt の import は `build_tab` 内に置く |
| `build_export_figs(data)` | export のみ | `{ファイル名: Figure}` を返す。キーがそのままファイル名になるので `.png` まで書く |
| `apply_state(tab, state)` | 任意 | 開くたびに前回の状態（`current.json`。初回は `{}`）を受け取る。例外を出すとタブが開かない（再読み込み時は古いタブが残る）ので、防御的に書く |

- **状態（`current.json`）**: 選択が変わるたびに `tab.dispatch_command("refresh-state")` を呼ぶと、`get_state()` の返り値が `<work_dir>/analyses/<解析名>/state/current.json` に書かれる。エージェントはこれを読んで「ユーザーがいま何を見ているか」を知る。値は JSON にできる型にする（numpy の数値は `int()` / `float()` で変換する）。
- **注釈**: `python -m llm_bridge annotate <解析名> marker|note k=v … [--dataset DS]`（またはエージェント）で追加した注釈が、`{"markers": [...], "notes": [...]}`（各要素は k=v を辞書にしたもの）としてハンドラに渡される。`tab.connect_annotations(handler)` を `attach_tab` の前に呼び、ハンドラで描画する。雛形のハンドラは空なので、書くまで何も表示されない。
- **使える部品**:
  - `AnalysisTab`（`gui.tab`）: `add_panel(key, widget, "top"|"left"|"right")`、`set_split_orientation("horizontal"|"vertical")`、`set_split_ratio(l, r)`、`register_command(verb, handler)` など。
  - `gui.panels`: `SelectorPanel`（コンボボックス）、`TrajectoryPanel`（散布図）、`ImagePanel`（配列画像）、`FigurePanel`（PNG 表示）。
  - `gui.imageviewer.attach_image_viewer(tab, image, panel="left")`（画像ビューア）。
- **独自コマンド**: `tab.register_command("select", handler)` で登録すると、`python -m llm_bridge tab <解析名> select session=... --wait` やエージェントから呼べる。`k=v` はキーワード引数として渡されるので、ハンドラの引数名をキーに合わせる。値は、`name` / `dataset` / `path` / `slot` / `text` などの名前・パス・自由文のキー（`llm_bridge/__main__.py` の `_STRING_KEYS`）では文字列のまま、それ以外は普通の 10 進数（`12`・`-3`・`0.5`・`1e3`）のときだけ数値に変換される。独自コマンドの引数名が `_STRING_KEYS` に入っていると、数字でも文字列のまま届く。`true` / `false` は文字列のまま。返り値は結果の `result` に入り、例外は `status: error` になる。`set-split` / `close-pane` / `list-panes` / `snapshot` / `refresh-state` は予約済み。`dataset=` はタブの指定に使われ、ハンドラには渡らない。用途は解析の `README.md` に書いておく。
- **import**: `common.*` / `core.*` / `gui.*` / `config` は使える。同じフォルダに置いた補助 `.py` は import できないので、1 ファイルにまとめる。追加パッケージは GUI と同じ venv に入れる（Qt 以外はモジュール先頭で import してよい）。
- **`@dataclass` と型注釈**: `@dataclass` や型注釈はそのまま使える（雛形の `from __future__ import annotations` も外さなくてよい）。ただし `analysis.py` は開くときだけモジュールとして登録されるので、解析のオブジェクトの pickle と、Windows の multiprocessing（spawn）は使えない。タブを開いた後に関数の中で `@dataclass` を定義するのも避ける。注釈の評価（`typing.get_type_hints`）やソースの取得（`inspect.getsource`）は、開いた後だと解析の中で定義した名前を解決できず失敗するので、`load()` / `build_tab()` の中で済ませる。BOM 付きで保存した `analysis.py` は GUI では開けない（`U+FEFF` の構文エラー）ので、BOM 無しの UTF-8 で保存する。

## 編集後の反映

- タブを右クリック → **タブを閉じる** → 開き直す。
- または `python -m llm_bridge window reload scope=tab target=<解析名> --wait`。成否は出力の `result`（`reloaded-tab:…` / `reload-tab-error:…`。モーダルダイアログを開いている間は `reload-busy:…`）で確認する。失敗すると古いタブが残る。1 つのデータセットにしか無い名前なら、どのデータセットが前面でもそのタブが対象。同じ名前のタブが複数のデータセットにあるときは `dataset=<ds>` で対象を選ぶ（省略時はアクティブなデータセットのもの。チャットのエージェントが実行したときはチャットのデータセットのもの）。

## 同期ドライブ上で安全に編集する

rclone などのマウント上でエディタが「一時ファイルに書いて置き換える」方式で保存すると、ファイルが 0 バイトになることがある。次の手順を推奨する（ローカルディスクなら直接編集してよい）。

```bash
python -m llm_bridge draft-analysis <解析名> --dataset <データセット名>   # 下書きを作る（パスが表示される）
# 表示された analysis.draft.py を編集
python -m llm_bridge apply-analysis <解析名> --dataset <データセット名>   # 構文と build_tab を確認してから analysis.py に書き戻す
```

書き戻した後、開いているタブは [編集後の反映](#編集後の反映) の手順で読み込み直す。`draft-analysis` を再実行すると未反映の下書きは上書きされる。

`analysis.py` が 0 バイトになってしまったら `python -m llm_bridge recover-analysis <解析名> --dataset <データセット名>`（最後に正常に開けた版 `analysis.py.bak` から戻す。0 バイトか欠損のときだけ動く）。`.bak` はデータと同じドライブにあるので、大事な解析コードは Git 等でも管理する。

## エラーの確認

- CLI の `--wait` の出力（JSON の `status` / `error` / `result`）か、エージェントの返答で確認する。
- 解析が 0 バイト・見つからない・`build_tab` が無い・名前の不一致などは、エラーメッセージにそのまま出る。
- `load()` / `build_tab()` の例外は、`error` に型とメッセージが、`traceback` に末尾 10 フレームのトレースバックが入る。詳しく調べるときは、自分のスクリプトから `load()` を呼んでみる。
- `apply_state` の例外は、`could not establish state for … (apply_state failed: <型>: <メッセージ>)` というエラーになり、`traceback` にも元の例外の連鎖（`The above exception was the direct cause …`）として解析側のフレームが入る。ただし再読み込み（`reload scope=tab`）の失敗は `status: "ok"` の戻り値の文字列として返るので、`traceback` は `null` のまま。詳しい traceback は、ログを書ける限り `data/logs/myanalysis.log` に残る（書けない場合 → [トラブルシューティング](troubleshooting_ja.md#トラブルシューティング)。`python tool.py` で起動したときはコンソールにも出る）。雛形のように `build_tab` の中で `refresh-state` を呼ぶ場合、状態を返す関数の失敗（JSON にできない値など）は `build_tab` の例外として型とメッセージが返る。
- 保存したセッションの復元に失敗したタブは、黙って飛ばされる。原因は、ログを書ける限り `data/logs/myanalysis.log` に残る（書けない場合 → [トラブルシューティング](troubleshooting_ja.md#トラブルシューティング)。`python tool.py` で起動したときはコンソールにも出る）。ボタン操作などで起きた未捕捉の例外は `data/logs/gui-crash-*.log` に残る。
- `load()` は GUI のスレッドで動くので、重い読み込みの間はウィンドウが固まる。CLI では `--wait 120` のように長めに待つ。

## PNG の一括出力（GUI 不要）

```bash
python -m export <データセット名> <解析名>
```

`load()` の結果を `build_export_figs()` に渡し、返された図を `<work_dir>/analyses/<解析名>/batch/` に PNG で保存する（`load()` が `None` を返すと失敗する）。

## Python から直接使う

エージェントが使うのと同じ関数を、自分のスクリプトからも使える（`common/explore.py`）。スクリプトをリポジトリ直下以外（サブフォルダを含む）に置く場合は、`PYTHONPATH=<リポジトリ>` を設定して実行する。

```python
from common.explore import dataset_summary, load_dataset, save_fig, save_code, save_text

summary = dataset_summary("my_dataset")          # まず実際の構成（サブフォルダ・代表 CSV の列と行数）を見る
sessions = load_dataset("my_dataset", subdir_pattern="<実在するパターン>", csv_name="<実在する>.csv")
# → [{"name": サブフォルダ名, "dir": Path, "df": DataFrame}, ...]（format = csv_per_subdir のみ）

save_fig("my_dataset", fig, "overview")          # → <work_dir>/figures/overview.png
save_code("my_dataset", "helper", code)          # → <work_dir>/code/helper.py
save_text("my_dataset", "reports/summary.md", md)  # → <work_dir>/reports/summary.md
```

- `subdir_pattern`（データセット直下からの glob パターン）/ `csv_name` はキーワード引数。既定値は無いので、`dataset_summary` で見た実際の名前を渡す。Shift-JIS の CSV は `encoding="cp932"` を付ける。
- `save_fig` は保存後に図を閉じる。`save_fig` / `save_code` のラベルに拡張子は付けない。その他の形式（`.md` / `.csv` / `.json` など）は `save_text` で書く。
- `save_text` のパスは `work_dir` の中に限られる（`..`・絶対パス・予約名などは拒否。`session.json`・`chat_sessions/`・`*.bak` も書けない）。
