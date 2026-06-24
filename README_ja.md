# MyAnalysis

実験計測データの解析プロジェクト（Python）。**PySide6 GUI** + **LLM チャットドック** + **ホットリロード**開発環境を備える。計測データはリポジトリ外（同期ドライブ）に置き、このリポジトリにはコードのみをバージョン管理する。

> このファイルは人間の利用者・新規参加者向けの概要です。開発者・LLM エージェント向けの詳細な規約は [CLAUDE.md](CLAUDE.md) を参照してください。英語版は [README.md](README.md) にあります。

---

## 必要環境 / セットアップ

- **Python 3.11 以上が必須**（`tomllib` を標準ライブラリとして使用するため。3.10 以前では動きません）。
- 依存定義ファイル（`requirements.txt` / `pyproject.toml`）は**未整備**です。現状は import から判明する依存を手動で入れてください。

**コア依存**（GUI 起動に必要）:

```
PySide6  pyqtgraph  numpy  pandas  matplotlib
```

**任意依存**（特定の解析を動かす時だけ）:

- `h5py` — `.h5` カメラ画像を扱う解析（例: `dataset_d`）でのみ必要。`load()` 内で遅延 import される。

**設定ファイル**（いずれも `.example` をコピーして使う。実体は gitignore）:

| コピー元 | コピー先 | 用途 |
|---|---|---|
| [.env.example](.env.example) | `.env` | LLM 接続・秘密（`OPENAI_BASE_URL` / `OPENAI_API_KEY` / `PI_API_KEY` / `LLM_BACKEND` 等） |
| [llm_backend/config.example.toml](llm_backend/config.example.toml) | `llm_backend/config.toml` | バックエンド選択と運用設定 |
| [models.example.toml](models.example.toml) | `models.toml` | モデル設定（model / thinking / effort / provider） |

LLM チャットを使わない場合でも GUI 自体は起動します（`.env` 等が無ければチャットはバックエンド未設定で動かないだけ）。

---

## 起動方法

```bash
# Windows: ダブルクリックでも可。.venv があれば自動で activate して tool.py を実行
run.bat

# 直接起動
python tool.py
python tool.py --demo            # 合成データで全パネル種別を表示する検証用タブ付き
python tool.py --resume-session  # リロード manifest からウィンドウ/タブ/チャットを復元（主にホットリロード用）
```

GUI 本体は [tool.py](tool.py)（PySide6）。

---

## ディレクトリ構成

| パス | 役割 |
|---|---|
| [tool.py](tool.py) | GUI エントリポイント（`QApplication` + `ToolWindow` 起動） |
| [run.bat](run.bat) | Windows 用ランチャ（`.venv` activate → `python tool.py`） |
| [config.py](config.py) | データセット登録簿。`DATASETS`（dataset 名 → {ホスト名: フルパス}） |
| [dataset_config.py](dataset_config.py) | データセット個別設定（`myanalysis.toml`）の読み書き |
| [common/](common/) | 共有ユーティリティ（`explore.py`, `loaders.py`, `paths.py`, `filelock.py`, `env.py`） |
| [core/](core/) | 低レベルモジュール（`figures.py` = matplotlib ヘルパ、import 時に Agg 確定） |
| [gui/](gui/) | PySide6 GUI（`window.py`, `tab.py`, `chat.py`, `panels.py`, `tools.py`） |
| [llm_backend/](llm_backend/) | LLM バックエンド抽象（claude / openai / pi / mock） |
| [llm_bridge/](llm_bridge/) | GUI↔CLI ブリッジ（ファイルシステム経由・クロスプラットフォーム） |
| [devtools/](devtools/) | ホットリロード（`hotreload.py`, `qt_integration.py`） |
| [analyses/](analyses/) | 解析モジュール群（1 ディレクトリ = 1 解析、各 `analysis.py`） |
| [newanalysis/](newanalysis/) | 解析モジュールの雛形生成器 |
| [export/](export/) | ヘッドレス PNG エクスポート driver |
| [tests/](tests/) | pytest テスト群 |
| `data/` | gitignore のローカル作業領域（出力・キャッシュ）。中身はコミットしない |
| `docs/` | ドキュメント（現状ほぼ空） |

---

## データアクセス

計測データはリポジトリの外、同期ドライブ上にあり、**マウント先のドライブレターは PC ごとに異なります**。この差を吸収するため、[config.py](config.py) の `DATASETS` が各データセットを「ホスト名 → その PC でのフルパス」で持ちます。

```python
DATASETS = {
    "dataset_a": {
        "HOST_A": r"G:\同期\測定\000000\example",
        "HOST_B": r"H:\同期\測定\000000\example",
    },
    ...
}
```

**解析コードに `G:\...` 等の絶対パスをハードコードしてはいけません。** 必ず次を経由します:

```python
from config import get_dataset_dir
path = get_dataset_dir("dataset_a")  # 現在のホスト名で解決
```

未知のデータセット名・未登録ホストは、`config.py` を指し示す説明的なエラーになります。新しい PC で使う時は、各データセットにそのホスト名（大文字）のエントリを追加してください。

### データセットの登録

```bash
# CLI（config.py を ast ベースで原子的に書き換える。GUI 起動中なら自動で開く）
python -m llm_bridge register-dataset <name> <path> [--host H] [--with-analysis [NAME]] [--no-open]

# GUI: ファイル → データセットを新規登録
```

### セッションフォルダ命名規則

データセットディレクトリの中は、`session_<yyyymmdd>_<hhmmss>_<id>` という名前のセッションフォルダで構成されます。

### データセット個別設定 — `myanalysis.toml`

データセット固有の設定は、`config.py` ではなく**そのデータセットディレクトリ直下の `myanalysis.toml`**（[dataset_config.py](dataset_config.py) が管理）に置きます。データと一緒に同期ドライブで運ばれるためです。

- `work_dir`（既定 `_work`）— 解析出力の保存先。相対パスはデータセットディレクトリ基準。
- `format`（既定 `csv_per_subdir`）— 読み込み形式。`csv_per_subdir` または `custom`。

計測ファイル（CSV 等）は決して書き換えられません。`myanalysis.toml` は出力保存時に機械的に生成され、手編集も安全です。

---

## 解析の探索とコード実行

LLM エージェント／対話的な探索のために、[common/explore.py](common/explore.py) が最小限の API を提供します。

```python
from common.explore import load_dataset, save_fig, save_code, dataset_summary

sessions = load_dataset("my_dataset")
# → list[dict]: 各 {"name": str, "dir": Path, "df": DataFrame}

save_fig("my_dataset", fig, "overview")    # → <work_dir>/figures/overview.png（保存後 fig は close）
save_code("my_dataset", "helper", code)    # → <work_dir>/code/helper.py
summary = dataset_summary("my_dataset")    # カラム・dtype・行数をセッションごとに要約
```

下位ローダは [common/loaders.py](common/loaders.py) の `load_csv_per_subdir`（サブディレクトリを glob して `pd.read_csv`）。出力先はデータセットの `work_dir`。

---

## 解析モジュールの追加と export

各解析は `<dataset_dir>/analyses/<name>/analysis.py` の 1 モジュールで、データと並んで同期ドライブ側に置かれます（リポジトリには含まれません）。標準パターン:

```python
NAME = "my_analysis"      # モジュール識別子
DATASET = "my_dataset"    # 使用するデータセットキー

def load() -> dict:                         # データ読み込み（GUI 副作用なし）
    ...
def build_export_figs(data) -> dict:        # ヘッドレス PNG 用 {ファイル名: Figure}
    ...
def build_tab(parent, data) -> AnalysisTab: # GUI タブ構築
    ...
```

既存例: `dataset_d`（.h5 カメラ画像）、`analysis_c`（雛形）、`example_analysis`、`dataset_b`。

### 雛形生成

```bash
python -m newanalysis <name> --dataset <key>
```

`<dataset_dir>/analyses/<name>/` に標準パターン入りの `analysis.py` + `README.md` を生成します。`--dataset` は必須です（雛形は当該データセットのディレクトリに書き込まれます）。`register-dataset --with-analysis [NAME]` からも同じ生成器が呼ばれます（NAME 省略時はデータセット名）。

### ヘッドレス PNG エクスポート

```bash
python -m export <dataset> <name>
```

`build_export_figs()` を Agg バックエンドで実行し、PNG を `<work_dir>/analyses/<name>/batch/` に書き出します（GUI 不要）。

---

## GUI の機能概要

- **マルチタブ** — 解析タブをドラッグ&ドロップで並べ替え。
- **LLM チャットドック**（右側、フロート可）— 複数セッションタブ、Ctrl+Enter で送信、ストリーミング表示、ツール呼び出し（タブ操作・スナップショット等）、フォントズーム。
- **メニュー** — ファイル / 表示 / ヘルプ / 開発(&D)。
- **セッション保存/復元** — データセット単位でタブ構成・チャットを `work_dir` 下に保存・復元（ファイル → セッションを保存 / 終了時ダイアログ）。保存ファイルの内訳は [CLAUDE.md](CLAUDE.md) / [llm_bridge/session.py](llm_bridge/session.py) を参照。

---

## LLM チャット & バックエンド

チャットドックの LLM アクセスは [llm_backend/](llm_backend/) を通ります。4 種類:

| バックエンド | 説明 |
|---|---|
| `claude` | VS Code Claude Code 拡張のエンジンを再利用（stream-json モード） |
| `openai` | OpenAI 互換 HTTP（Ollama 等のローカルエンドポイント含む） |
| `pi` | pi-coding-agent サブプロセス |
| `mock` | オフラインのスモークテスト用 |

**選択順**: 環境変数 `LLM_BACKEND` → `llm_backend/config.toml` の `[backend].name` → `OPENAI_BASE_URL` 後方互換。

**設定の置き場所**:
- `.env` — 接続・秘密（API キー等）。
- `llm_backend/config.toml` — バックエンド選択と運用設定（cwd / bin / tools 等）。
- `models.toml` — モデル knob（`model` / `thinking` / `effort` / `provider`）。

---

## llm_bridge — GUI↔CLI ブリッジ

GUI と CLI をファイルシステム経由でつなぎ、外部から走行中の GUI を操作します（Windows / POSIX 両対応、PySide6 不要で動く verb もあり）。

```bash
python -m llm_bridge <verb> ...
```

主要 verb:

| verb | 用途 |
|---|---|
| `list-datasets [--json]` | 登録済みデータセット一覧 |
| `register-dataset <name> <path> ...` | データセット登録（+ 任意で雛形生成・自動オープン） |
| `list-analyses` | 解析一覧 |
| `active` | アクティブタブと現在開いているデータセット |
| `state [name]` | 解析の `state.json`（省略時はアクティブタブ） |
| `window <verb> [k=v] [--wait]` | ウィンドウ操作（`open-dataset` / `add-tab` / `reload` 等） |
| `tab <target> <verb> [k=v]` | タブ操作（`set-split` / `snapshot` / `refresh-state` 等） |
| `annotate` / `clear-annotations` | 注釈（marker / note）の追加・削除 |

例:

```bash
python -m llm_bridge list-datasets --json
python -m llm_bridge window open-dataset name=my_dataset --wait 30
```

---

## ホットリロード

走行中の GUI に修正コードを注入し、開いていたタブ・プロット・チャット文脈を保ったまま新コードを有効化します（[devtools/](devtools/)）。**トリガーは手動のみ**（CLI verb + 開発(&D) メニュー、自動 file-watch なし）。

```bash
python -m llm_bridge window reload [scope=app] --wait
```

4 段階エスカレーション:

| scope | 対象 | 機構 |
|---|---|---|
| `patch`（既定） | sys.modules 内の repo モジュール | in-place パッチ（表示状態は無傷） |
| `tab` | `<dataset_dir>/analyses/<name>/analysis.py` | 単一タブの sandbox 再ビルド → 差し替え |
| `app` | 構造変更（`__init__` / Signal / `__bases__` 等） | blue-green でウィンドウ再構築 |
| `restart` | tool.py 自体・PySide6 更新 | プロセス再起動 + セッション自動復元 |

詳細・既知の限界は [CLAUDE.md](CLAUDE.md) を参照。

---

## テスト

```bash
python -m pytest tests/
```

GUI テストは `QT_QPA_PLATFORM=offscreen` を自己設定するため、ヘッドレスで動きます。

---

## 開発規約（抜粋）

- **`main` ブランチに直接コミットする**（feature ブランチを切らない）。
- `data/` は gitignore — ローカル作業領域。中身はコミットしない。
- `.bat` / `.cmd` は **CRLF 改行**（LF だと `cmd.exe` が壊れる。`.gitattributes` で固定済み）。
- リポジトリは GitLab の private（`git@gitlab.com:atakalive/MyAnalysis.git`）。

より詳しい規約・各サブシステムの設計は [CLAUDE.md](CLAUDE.md) を参照してください。
