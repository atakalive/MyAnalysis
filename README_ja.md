# MyAnalysis

実験計測データの解析プロジェクト（Python）。**PySide6 GUI** + **LLM チャットドック** + **ホットリロード**開発環境を備える。計測データはリポジトリ外（同期ドライブ）に置き、このリポジトリにはコードのみをバージョン管理する。

> このファイルは人間の利用者・新規参加者向けの概要です。開発者・LLM エージェント向けの詳細な規約は [CLAUDE.md](CLAUDE.md) を参照してください。英語版は [README.md](README.md) にあります。

---

## 思想と設計 — 想定される使い方

MyAnalysis の根っこにある考え方は 1 つ: **計測データの解析は、チャットドック内の AI エージェントに「何が知りたいか」を普通の言葉で伝えると、エージェントが解析を実行し GUI を操作してくれる**、というもの。プロット用コードを毎回手書きしたりコマンドを覚えたりする必要はない。ユーザーは *問いと目的* のレベルで指示し、段取り（データの実構成の把握・ロード・作図・保存・タブ配置）はエージェントが担う。

**設計原則**

1. **すべてはデータと一緒に同期ドライブ側に置く。** リポジトリにはアプリのコードだけがある。各データセットは自己完結し、その設定（`myanalysis.toml`）・解析コード（`analyses/<name>/`）・出力（`_work/`）はすべてデータセットディレクトリ配下（同期ドライブ）に収まる。狙いは *同じ解析がどの PC でも同一に再開できる* こと。
2. **チャットエージェントはデータ解析役であり、アプリ開発者ではない。** 役目は登録済みデータセットの解析と GUI 操作であって、MyAnalysis 自体を書き換えることではない。
3. **まず実物を見る（inspect-first）。** データ配置を決め打ちしない。読み込む前にデータセットの実構成を確認するので、まっさらなデータセットを渡しても形を自分で把握する。
4. **ローカルには何も残さない。** 図・コード・状態はデータセットの `work_dir`（同期側）にだけ書き、ローカルマシンには一切退避しない。「どの PC でも再開」が成り立つのはこのため。

**指示の出し方** — やりたいことを普通の言葉で言えばよい。どこから読むか・どこに保存するか（`work_dir` は既定動作で、ユーザーは指定しない）・ラベルの付け方・どの GUI 操作を走らせるか、はエージェントが判断する。例:

- 「`<Dataset Name>` を開いて概要を説明して」
- 「`<量A>` と `<量B>` の関係を可視化して」
- 「現在の `<タブA>` と `<タブB>` の結果をパネル縦分割で並べて表示して」
- 「`<Dataset A>` と `<Dataset B>` に同等の解析を適用して比較して」
- 「全体の結論を一枚のプレゼンスライド形式でまとめておいて」
- 「今回の解析の内容を正確に説明して」

内部的にはエージェントが Python（`common/explore.py`）で解析し `python -m llm_bridge` の verb で GUI を駆動するが、ユーザーはそれを意識しなくてよい。エージェントの正確な契約はチャットのシステムプロンプト（[llm_backend/claude_code.py](llm_backend/claude_code.py)）、規約は [CLAUDE.md](CLAUDE.md) を参照。

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
- `tifffile` + `Pillow` — ImageJ 風 画像ビューア（Issue #60, `show_image`）でのみ必要。`tifffile` は 16bit / 多ページ / N 次元 TIFF を、`Pillow` は PNG/JPG/BMP を開く。どちらも `common/image_io.py` 内で遅延 import され、無ければ TIFF / ラスタ読込が `pip install` を案内する graceful な `ImportError` を出す。

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
# Windows: ダブルクリックでも可。.venv を activate し、pythonw で windowless 起動
# （コンソールを残さない）。エラーを見たいときは `python tool.py` を直接実行。
run.bat

# 直接起動（コンソール付き。起動エラー/トレースバックを見たいときはこちら）
python tool.py
python tool.py --demo            # 合成データで全パネル種別を表示する検証用タブ付き
python tool.py --resume-session  # リロード manifest からウィンドウ/タブ/チャットを復元（主にホットリロード用）
```

GUI 本体は [tool.py](tool.py)（PySide6）。

windowless（`run.bat` / pythonw）起動時の未捕捉例外は `data/logs/gui-crash-*.log` に traceback が残る（同一箇所のクラッシュはメッセージが変わっても初回のみ・1 プロセス最大 200 件・意図的終了は記録しない）。起動時 import エラー等はコンソール付きの `python tool.py` で確認する。

---

## ディレクトリ構成

| パス | 役割 |
|---|---|
| [tool.py](tool.py) | GUI エントリポイント（`QApplication` + `ToolWindow` 起動） |
| [run.bat](run.bat) | Windows 用ランチャ（`.venv` activate → windowless `pythonw tool.py`） |
| [config.py](config.py) | データセット登録簿。`DATASETS`（dataset 名 → {ホスト名: フルパス}） |
| [dataset_config.py](dataset_config.py) | データセット個別設定（`myanalysis.toml`）の読み書き |
| [common/](common/) | 共有ユーティリティ（`explore.py`, `loaders.py`, `paths.py`, `filelock.py`, `env.py`） |
| [core/](core/) | 低レベルモジュール（`figures.py` = matplotlib ヘルパ、import 時に Agg 確定） |
| [gui/](gui/) | PySide6 GUI（`window.py`, `tab.py`, `chat.py`, `panels.py`, `tools.py`） |
| [llm_backend/](llm_backend/) | LLM バックエンド抽象（claude / openai / pi / codex / mock） |
| [llm_bridge/](llm_bridge/) | GUI↔CLI ブリッジ（ファイルシステム経由・クロスプラットフォーム） |
| [devtools/](devtools/) | ホットリロード（`hotreload.py`, `qt_integration.py`） |
| [meeting/](meeting/) | ミーティング共有リレー（ローカル・インメモリ・リレー + cloudflared トンネル） |
| [relay-worker/](relay-worker/) | ミーティング共有のゲストページ（`chatdock.html`。GitLab Pages に配信）＋ セットアップ README |
| [config_share.py](config_share.py) | `config.py` の Cloudflare R2 同期（push / pull / sync・任意） |
| [i18n/](i18n/) | UI 文言カタログ（`en.toml` / `ja.toml`） |
| [newanalysis/](newanalysis/) | 解析モジュールの雛形生成器 |
| [export/](export/) | ヘッドレス PNG エクスポート driver |
| [tests/](tests/) | pytest テスト群 |
| `data/` | gitignore のローカル作業領域（出力・キャッシュ）。中身はコミットしない |

解析モジュールはリポジトリには**ありません** — 各解析はデータと一緒に同期ドライブ上の `<dataset_dir>/analyses/<name>/analysis.py` に置かれます（後述「解析モジュールの追加と export」参照）。

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
python -m llm_bridge register-dataset <name> <path> [--host H] [--no-open]

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
from common.explore import load_dataset, save_fig, save_code, save_text, dataset_summary

summary = dataset_summary("my_dataset")   # まず INSPECT: 実在の subdirs + 代表 CSV の columns/rows
# 既定パターンは仮定しない — 上で見た実構成を使ってロードする:
sessions = load_dataset("my_dataset", subdir_pattern="<real_folder_*>", csv_name="<real>.csv")
# → list[dict]: 各 {"name": str, "dir": Path, "df": DataFrame}

save_fig("my_dataset", fig, "overview")    # → <work_dir>/figures/overview.png（保存後 fig は close）
save_code("my_dataset", "helper", code)    # → <work_dir>/code/helper.py（.py 専用。label に拡張子は付けない）
save_text("my_dataset", "reports/summary.md", md)   # → <work_dir>/reports/summary.md（任意の拡張子・サブディレクトリ可）
```

`save_text` は汎用のテキスト書込です（メモ・レポート・派生 CSV/JSON）。相対パスは検証され（`..`・絶対パス・ドライブ相対・UNC・Windows 禁止文字・予約デバイス名 — `nul.txt` を含む — を拒否）、`work_dir` 配下に収まることを再確認します。`session.json`・`chat_sessions/`・任意の `*.bak` は GUI の live 状態なので拒否されます。`save_fig` / `save_code` の label にドットを含めることはできません（拡張子は自動付与）。3 つとも絶対 `Path` を返します。

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

`<dataset_dir>/analyses/<name>/` に標準パターン入りの `analysis.py` + `README.md` を生成します。`--dataset` は必須です（雛形は当該データセットのディレクトリに書き込まれます）。

### ヘッドレス PNG エクスポート

```bash
python -m export <dataset> <name>
```

`build_export_figs()` を Agg バックエンドで実行し、PNG を `<work_dir>/analyses/<name>/batch/` に書き出します（GUI 不要）。

---

## GUI の機能概要

- **マルチタブ** — 解析タブをドラッグ&ドロップで並べ替え。
- **LLM チャットドック**（右側、フロート可）— 複数セッションタブ、Ctrl+Enter で送信、ストリーミング表示、ツール呼び出し（タブ操作・スナップショット等）、フォントズーム。
- **メニュー** — ファイル / 表示 / 設定 / ヘルプ / 開発(&D)。
- **セッション保存/復元** — データセット単位でタブ構成・チャットを `work_dir` 下に保存・復元（ファイル → セッションを保存 / 終了時ダイアログ）。保存ファイルの内訳は [CLAUDE.md](CLAUDE.md) / [llm_bridge/session.py](llm_bridge/session.py) を参照。

---

## LLM チャット & バックエンド

チャットドックの LLM アクセスは [llm_backend/](llm_backend/) を通ります。エンジンは 6 種類ありますが、**実運用は Claude（VS Code 同梱エンジン）**です。設定値（環境変数 `LLM_BACKEND` / `config.toml` の `[backend].name`）に入るのは**バックエンド名**（表の 2 列目）で、Claude の 2 経路は `[claude_code].bin` が空かどうかで切り替わります（空 = 同梱バイナリを自動検出、非空 = そのパスの CLI）:

| エンジン（GUI 表記） | `[backend].name` | 説明 |
|---|---|---|
| Claude（VS Code 同梱エンジン） | `claude`（`bin` 空） | **実運用の既定。** VS Code Claude Code 拡張の同梱エンジン（stream-json モード）と VS Code のログインを再利用 |
| Claude（PATH の CLI） | `claude`（`bin` 指定） | PATH 等にある `claude` CLI を使う別経路 |
| pi コーディングエージェント | `pi` | pi-coding-agent サブプロセス |
| Codex CLI（OpenAI） | `codex` | OpenAI Codex CLI サブプロセス |
| OpenAI 互換 HTTP | `openai` | OpenAI 互換 HTTP（Ollama 等のローカルエンドポイント含む） |
| モック（動作確認） | `mock` | オフラインのスモークテスト用（LLM 不要） |

### `claude` バックエンドの設定（推奨）

1. **VS Code Claude Code 拡張をインストールしてサインイン** — 同梱エンジンと `~/.claude` のログインを再利用するので、`.env` に API キーを置く必要はありません。
2. `llm_backend/config.toml` で `[backend].name = "claude"`（`bin` は空にすると同梱バイナリを自動検出。`permission_mode = "bypassPermissions"` が既定で、これによりエージェントが無人で GUI を駆動できます）。
3. `models.toml` の `[claude_code]` を設定 — 例 `model = "claude-opus-4-8"`, `thinking = "enabled"`, `effort = "xhigh"`。
4. 反映 — ファイルを手で編集した場合は**アプリを再起動**します（起動時に一度だけ読まれるため）。GUI の **設定 → バックエンド/モデル設定…** から変更した場合は再起動不要で、適用すると全チャットセッションに次の送信から反映されます。

> 同梱の `llm_backend/config.example.toml` は `[backend].name` の既定が `pi`（開発用プレースホルダ）です。通常利用では `claude` に書き換えてください。

### その他のバックエンド（任意）

- `openai` — `.env` の `OPENAI_BASE_URL` / `OPENAI_MODEL` / `OPENAI_API_KEY` を任意の OpenAI 互換エンドポイント（例: ローカル Ollama）に向ける。
- `pi` — `npm i -g @earendil-works/pi-coding-agent`（旧名 `@mariozechner/…` は deprecated）。運用設定は `config.toml`、秘密は `.env`。Windows では **Windows 側にインストール**すること（WSL 側だけのインストールは参照できない）。
- `codex` — `npm i -g @openai/codex`、ログインは `codex login`。モデル設定は `models.toml` の `[codex]`。
- `mock` — オフライン・設定不要。GUI のスモークテストに便利。

### バックエンドの状況ウィンドウ

**設定 → バックエンドの状況…** で、対応ドライバ全部の導入状況を一覧できます（現在どのエンジンを選択しているかとは無関係の棚卸しです）。非モーダルなので、npm インストールの進行中も本体を操作できます。CLI 同等品は `python -m llm_bridge engines`。

- **表示** — 1 行 = 1 エンジン。列は ドライバ / 前提（node+npm）/ 導入（バージョン）/ 認証（ログイン済みプロバイダ）。記号は ✓ = OK、✗ = 無い、? = 確認できなかった、— = 対象外。`?` は「入っていない」ではありません（タイムアウト等で判定できなかっただけの状態に、再インストールを勧めない設計です）。
- **インストール / 更新** — どちらも同じ `npm i -g <パッケージ>` を実行します。未導入の行では「インストール」、導入済みの行では「更新」と表示されるだけの違いです。前提が欠けている行と `?` の行にはボタンが出ません。Claude（VS Code 同梱）は拡張のアップデートが入手経路なのでボタンがありません。
- **ログイン** — 認証済みでも表示されます（アカウント切替・再ログイン用）。Windows では新しいコンソールを開いて対話ログインします。それ以外の OS では実行すべきコマンドをログ欄に表示するだけなので、手元の端末に貼り付けてください。
- **疎通確認** — 実際にバックエンドへ 1 ターン投げる、このウィンドウで**唯一課金される操作**です（押したときだけ実行）。ウィンドウを開く・「再確認」を押すのはローカル判定だけで課金されません。自動更新タイマーは意図的にありません。CLI の `engines` に疎通確認はないので完全に無課金です。
- **安全性** — 判定に使うのは auth ファイルを変更しない読み取り専用のコマンド（`--version` / `codex login status` / `pi --list-models`）だけです。状況を見たことが原因で他 PC・他 CLI のログインが失効することはありません。
- **注意** — インストール完了後も ✗ のままの場合は GUI を再起動してください（PATH は起動時のスナップショットのため、npm の global bin が後から PATH に載る構成では検出されません）。

**バックエンド/モデル設定ダイアログ**（設定 → バックエンド/モデル設定…）では、エンジン・モデル・プロバイダを選んで適用できます。モデル/プロバイダの候補リストはコンボ横の ＋/− で編集でき（`models.toml` に保存）、適用前に任意の疎通チェックも実行できます。チャットタブを右クリック → **このチャットのモデル…** で、そのセッションだけ別エンジンに切り替えることもできます（未設定のセッションは全体設定に追従します）。

**AIペルソナ**（設定 → AIペルソナ…）では、チャットエージェントのシステムプロンプト末尾に追記する応答スタイルを選べます — 例: 既定の丁寧な応対の代わりに単刀直入で批判的な文体。ペルソナ（名前＋本文）は同じダイアログで追加・編集・削除できます（定義は PC ローカルの `data/llm_state/personas.json` で、PC 間では同期されません）。チャットタブを右クリック → **このチャットのペルソナ…** でセッション単位の上書きもできます — 全体にペルソナを設定したまま特定のチャットだけ「なし」にすることも可能です。ペルソナが変えるのは口調・文体のみで、運用ルールが常に優先されます。既定は「ペルソナなし」で、プロンプトは従来と完全に同一です。

**選択順**: 環境変数 `LLM_BACKEND` → `llm_backend/config.toml` の `[backend].name` → `OPENAI_BASE_URL` 後方互換。

**設定の置き場所**:
- `.env` — 接続・秘密。注意: 実 shell の環境変数が `.env` より優先され、行内 `# コメント` は**使えません**（`KEY=value # x` は値が `value # x` になる）。
- `llm_backend/config.toml` — バックエンド選択と運用設定（bin / cwd / tools 等）。
- `models.toml` — モデル knob（`model` / `thinking` / `effort` / `provider`）。

`config.toml` と `models.toml` は起動時に読み込まれます — 手編集の反映は再起動、GUI の設定ダイアログ経由なら即時反映です。

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
| `list-analyses [--dataset <ds>] [--json]` | 開いている全データセット横断の解析一覧（`--json` で `{dataset: [names]}` マップ） |
| `list-open-datasets` | 開いているデータセット一覧＋アクティブ |
| `active` | アクティブタブ・アクティブデータセット・`open_datasets` |
| `state [name]` | 解析の `state.json`（省略時はアクティブタブ） |
| `window <verb> [k=v] [--wait]` | ウィンドウ操作（`open-dataset` / `add-tab` / `set-active-dataset` / `close-dataset` / `reload` 等） |
| `tab <target> <verb> [k=v]` | タブ操作（`set-split` / `snapshot` / `refresh-state` 等） |
| `annotate` / `clear-annotations` | 注釈（marker / note）の追加・削除 |
| `meeting-start [lan=true]` / `meeting-lan-link` | ミーティング共有を開始 / LAN リンクを取得（「ミーティング共有（ホスト側セットアップ）」参照） |

### 複数データセット

1 プロセスに複数データセットを開ける。トップのデータセット・スイッチャーで、各データセットのタブ群とチャットが入れ替わる。`open-dataset` はデータセットをワークスペースに追加（他は開いたまま）、`set-active-dataset name=<ds>`（別名 `switch-dataset`）で前面化、`close-dataset name=<ds>` はレイアウトを `session.json` へ退避してからグループを撤去する。同名タブが 2 データセットにある場合は、タブ系 verb（`add-tab` / `show` / `set-active-tab` / `close-tab` / `tab <name> <verb>`）に `dataset=<ds>` を付けて曖昧性を解消する。

**ワークスペース復元**：File →「前回のセッションを復元」で、一緒に開いていたデータセット群（gitignore された `data/llm_state/last_window.json` に記録）を再オープンする。復元は ADDITIVE（既に開いているデータセットは閉じない）。起動時自動復元は無い。

**会議共有**は開いている全データセットのタブとチャットを既定でゲストへ配信する（非公開にするには明示的なオプトアウトが必要。Issue #78）。同名タブはデータセットごとに別物として扱われる（ワイヤも DS 単位のタブ名前空間）。ゲスト HTML はホストの「DS レイヤー → タブレイヤー」構造をミラーし、DS バーは共有中の全データセットをクリック可能なチップとして並べる。各ゲストは自分の意思でナビゲートする（`curDs` はゲストローカル。ホストのアクティブ DS は初回ロードの既定値を決めるだけ）。DS バー右端の**「ホストに追従」トグル**（既定 OFF・非永続）を ON にするとホストのアクティブ DS へ即スナップし以降も同期、DS チップを手動クリックすると OFF に戻る（Issue #80）。DS を切り替えると未送信の入力欄はデータセットごとに退避されるので、ホスト由来の切替で打鍵中の本文が別データセットのチャットへ飛ぶことはない。ゲストにも独自の「新規チャットセッション」ボタンがあり、チャット送信キーはホストに追従する（Issue #81, #82）。また、トンネルのホスト名を解決できないネットワークのゲスト向けに、公開リンクと並べてLAN 直結リンクを併発できる（Issue #85）。リレー本体は標準ライブラリのみで動く — 立ち上げ方は「ミーティング共有（ホスト側セットアップ）」節と [relay-worker/README.md](relay-worker/README.md) を参照。

**メモリ注意（v1）**：各解析タブは開いた時点で DataFrame を eager load し、開いている限り常駐する。大きな測定データを多数同時に開くとメモリを圧迫し得る（遅延ロード/アンロードは将来課題）。

例:

```bash
python -m llm_bridge list-datasets --json
python -m llm_bridge window open-dataset name=my_dataset --wait 30
python -m llm_bridge window set-active-dataset name=other_dataset --wait
python -m llm_bridge tab summary snapshot dataset=other_dataset --wait
```

---

## ミーティング共有（ホスト側セットアップ）

ミーティング共有（表示 → ミーティング共有…）は、自分のチャットドックと解析ビューを、離れたゲストのブラウザへライブ共有します。既定では開いている全データセットのタブとチャットを配信します（Issue #78。非公開にするのは明示的なオプトアウト）。ゲストはリンクを開くだけで参加でき、サーバ側には何も永続化されません。

**構成**。ローカル・インメモリ・リレー（既定は `127.0.0.1` に bind）＋ 公開トンネル 1 本（**cloudflared named tunnel**）。$0 で、外部の key/value ストアは使いません。詳細と真実ソースは [relay-worker/README.md](relay-worker/README.md)、`.env` 変数は [.env.example](.env.example) にあります。

**共有をホストするのに必要なもの** — すべて `.env`（起動時に一度だけ読込。編集後は GUI を再起動）:

1. `RELAY_ADMIN_KEY` — **必須**の非空の秘密鍵（`python -c "import secrets; print(secrets.token_urlsafe(32))"` で生成）。空なら共有機能が無効になるだけで、他には影響しません。
2. 公開（外部）リンクには cloudflared 用に `CLOUDFLARE_TUNNEL_NAME` と `CLOUDFLARE_TUNNEL_HOSTNAME`、および cloudflared のワンタイム設定（ドメインを Cloudflare に委譲 → `cloudflared tunnel login` / `create` / `route dns`）が要ります。詳細は [relay-worker/README.md](relay-worker/README.md)。

**LAN 直結リンク（Issue #85・任意）**。会場ネットワークの DNS がトンネルのホスト名を解決できないゲスト向けに、ホスト PC へ LAN で直結する *第2* リンクを併発できます。共有ダイアログの「LAN リンクも出す」を ON（既定 OFF）、または `python -m llm_bridge meeting-start lan=true`（＋ `meeting-lan-link`）。関連 env: `RELAY_LAN_HOST` / `RELAY_LAN_PORT`。外部（トンネル・https）とLAN 内（LAN・http）のリンクは同時に有効なので、内外のゲストが同一会議に参加できます。LAN リンクはフルのコピーリンクとしてのみ配布します（http なので、https ページに素トークンを貼ると mixed-content でブロックされる）。またリレーが `0.0.0.0` に bind するのは共有中のみです — セキュリティ上の注意は [relay-worker/README.md](relay-worker/README.md) を参照。

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
