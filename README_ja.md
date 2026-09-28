# MyAnalysis

[English](README.md) | [日本語](README_ja.md)

計測データを、AI エージェントとチャットしながら解析するデスクトップアプリ（Python / PySide6）。

知りたいことを普通の言葉で伝えると、エージェントがデータの構成を確かめ、Python で解析・作図し、結果の図を GUI のタブに表示する。保存を頼んだ解析コード・図・レポートと、チャット履歴はデータと同じフォルダに保存されるので、同期ドライブに置けば別の PC で続きから再開できる。

**リポジトリ:**
- **GitHub（安定版）:** <https://github.com/atakalive/MyAnalysis>
- **GitLab（開発版）:** <https://gitlab.com/atakalive/MyAnalysis>

---

## Features

- **チャットで解析を依頼** — 「A と B の関係を図にして」のように頼むと、エージェントがデータを読み込んで解析・作図し、図をタブに表示する
- **解析一式がデータと一緒に動く** — チャット履歴と、エージェントが保存した解析コード・図・レポートは、データセットのフォルダ内に置かれる。同期ドライブに置けば、別の PC でも同じ状態から再開できる（PC ごとにデータセットの登録と AI エンジンのログインは必要）
- **複数データセットの同時表示** — データセットごとに解析タブとチャットをまとめて切り替えられる
- **AI エンジンを選べる** — Claude Code / Codex CLI / pi / OpenAI 互換 HTTP。チャットごとにエンジン・モデル・応答スタイル（ペルソナ）を変えられる
- **自作の解析タブ** — Python 1 ファイルで対話的な解析タブを作れる。GUI なしで PNG を一括出力することもできる
- **画像ビューア** — 16bit・多ページ・多次元 TIFF に対応（LUT・コントラスト・チャンネル合成・Z/T スライダー）。開き方は [図ビューアと画像ビューア](#図ビューアと画像ビューア) を参照
- **ミーティング共有** — 解析画面とチャットを、離れた相手のブラウザへライブで共有できる

**→ [セットアップ](#セットアップ)**

---

## 目次

- [概要](#概要)
- [動作環境](#動作環境)
- [セットアップ](#セットアップ)
- [基本的な使い方](#基本的な使い方)
- [データセット](#データセット)
- [GUI の機能](#gui-の機能)
- [AI エンジンの設定](#ai-エンジンの設定)
- [自作の解析モジュール](#自作の解析モジュール)
- [CLI リファレンス](#cli-リファレンス)
- [複数 PC での設定同期](#複数-pc-での設定同期)
- [ミーティング共有](#ミーティング共有)
- [保存データと再開](#保存データと再開)
- [同期ドライブとトラブルシューティング](#同期ドライブとトラブルシューティング)
- [セキュリティとプライバシー](#セキュリティとプライバシー)
- [アンインストール](#アンインストール)
- [Limitations](#limitations)
- [開発者向け情報](#開発者向け情報)
- [ライセンス](#ライセンス)

---

## 概要

MyAnalysis の使い方は、チャットドックの AI エージェントに「何を知りたいか」を伝えることに尽きる。プロット用のコードを毎回書いたり、コマンドを覚えたりする必要はない。データの構成の把握・読み込み・作図・保存・タブの配置はエージェントが受け持ち、ユーザーは問いと目的のレベルで指示する。

**設計方針**

1. **解析一式はデータセットのフォルダに置く。** Git で管理しているのはアプリのコードだけ（PC ごとの登録簿・設定・状態は、リポジトリ内の Git 管理外のファイル（`datasets.local.json`・`models.toml`・`llm_backend/config.toml`・`.env`・`data/`）と、ホームの `~/.myanalysis/` に置く → [保存データと再開](#保存データと再開)）。各データセットの設定（`myanalysis.toml`）・解析コード（`analyses/`）・出力（既定 `_work/`）はデータセットのフォルダ内に収まる。同期ドライブに置けば、同じ解析をどの PC でも再開できる（PC ごとのデータセット登録と AI エンジンのログインは必要）。
2. **エージェントは「データ解析担当」。** エージェントの役目は、登録済みデータセットの解析と GUI の操作。アプリ自体を書き換えることではない（Claude Code にはシステムプロンプトで明示している）。
3. **まず実物を見る。** データの配置を決め打ちせず、読み込む前にデータセットの実際の構成を確認する。初めて渡したデータセットでも、エージェントが自分で形を把握する。
4. **成果物は同期側に残す。** エージェントには、図・コード・メモをデータセットの出力フォルダ（`work_dir`）にだけ保存するよう指示している。ただし、アプリの表示設定やエンジンのログイン情報など PC ごとの状態はローカルに残る（→ [保存データと再開](#保存データと再開)）。

**指示の例** — やりたいことを普通の言葉で書けばよい。どこから読むか・どこに保存するか・どの GUI 操作をするかはエージェントが判断する。

- 「`<データセット名>` を開いて、中身の概要を説明して」
- 「`<量A>` と `<量B>` の関係を図にして」
- 「いま作った 2 枚の図を上下に並べて表示して」
- 「`<データセットA>` と `<データセットB>` に同じ解析をして、比較図を A 側に保存して」
- 「結論を 1 枚の図と Markdown のレポートにまとめて保存して」
- 「今回の解析で使ったコードを保存して、手順を説明して」

**注意**

- 解析（Python の実行）ができるのは、エージェント型のエンジン（Claude Code / Codex CLI / pi）だけ。OpenAI 互換 HTTP は GUI 操作（タブを開く・画像を表示する等）しかできない。モック（動作確認用のエンジン）は入力に関係なく定型文を返す。
- エージェントがその場で書いて実行したコードは、頼まない限り保存されない。後で再現・説明したい解析は「コードも保存して」と頼む。
- 依頼内容と、エージェントが読んだデータの中身は、選んだ AI エンジンの提供元へ送信される。また既定では、エージェントは確認なしでコマンドを実行する。必ず [セキュリティとプライバシー](#セキュリティとプライバシー) を読むこと。

---

## 動作環境

- **OS**: Windows 11 で開発・動作確認。ランチャー（`run.bat`）は Windows 用。macOS / Linux でも `python tool.py` で起動できるよう作られているが、動作は未検証（macOS では同期ドライブの自動判定が効かない → [同期ドライブでの書き込み](#同期ドライブでの書き込み)）。
- **Python**: 3.11 以上（動作確認は 3.12）。
- **Python パッケージ**:

  | 区分 | パッケージ | 用途 |
  |---|---|---|
  | 必須 | `PySide6`, `pyqtgraph`（`numpy` は依存で入る） | GUI |
  | 通常必要 | `pandas`, `matplotlib` | データ読み込み・作図・図の保存・PNG 出力 |
  | 任意 | `Pillow` | 画像ビューアで PNG / JPEG 等を開く（matplotlib と一緒に入る） |
  | 任意 | `tifffile` | TIFF の画像ビューア |
  | 任意 | `boto3` | [複数 PC での設定同期](#複数-pc-での設定同期) |
  | 解析次第 | `h5py` など | 各データセットの解析コードが使うもの |

  `pandas` と `matplotlib` が無くても GUI は起動するが、データの読み込みと図の保存が失敗する。依存定義ファイル（`requirements.txt` 等）は現状なく、バージョンも固定していない。動作確認済みバージョン: PySide6 6.10.1 / pyqtgraph 0.14.0 / numpy 1.26.4 / pandas 2.2.0 / matplotlib 3.8.2 / Pillow 10.3.0 / tifffile 2023.2.28 / boto3 1.43.36。

- **AI エンジン**（解析をさせるにはいずれか 1 つ）:
  - **Claude Code** — VS Code（または Cursor / Windsurf）の Claude Code 拡張にサインイン、または `claude` CLI（npm で入れる場合は Node.js と npm が必要）。Windows では Git for Windows（Git Bash）を推奨（MyAnalysis のエージェントへの指示は Bash ツールを前提にしている）。
  - **Codex CLI** — Node.js と npm が必要。
  - **pi (pi-coding-agent)** — Node.js 22.19 以上と npm が必要。Windows では bash（Git Bash 推奨）が必要で、pi は Windows 側に入れる（WSL の中だけに入れたものは認識されない）。
  - 各エンジンの利用料・契約はそれぞれの提供元による。
- **任意**: ミーティング共有で外部の相手とつなぐには [cloudflared](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/) と、Cloudflare で管理する自分のドメインが必要（同じ LAN 内だけなら不要）。設定同期には Cloudflare R2。
- **データの置き場所**: 任意のフォルダ（同期ドライブ推奨）。アプリがデータセットのフォルダに設定と出力を書くので、書き込み権限が必要。

---

## セットアップ

### 1. インストール

リポジトリは**ローカルディスクの書き込みできる場所**に置く（アプリが `data/` などに PC 固有の状態を保存するため。同期ドライブに置くのはデータセットのほう）。

```bash
git clone https://github.com/atakalive/MyAnalysis.git
cd MyAnalysis
```

Windows（Python 3.11 以上がインストール済みであること。コマンドプロンプトで実行する）:

```bat
py -3 -m venv .venv
.venv\Scripts\activate
python -m pip install PySide6 pyqtgraph pandas matplotlib
rem 任意（TIFF の画像ビューア）
python -m pip install tifffile
```

PowerShell では、スクリプトの実行が禁止されていると `activate` が失敗する（そのまま進むと venv の外にインストールされる）。その場合は `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` を実行するか、コマンドプロンプトを使う。

macOS / Linux:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install PySide6 pyqtgraph pandas matplotlib
pip install tifffile                                    # 任意（TIFF の画像ビューア）
```

- venv の名前は `.venv` にする（`run.bat` はこの名前だけを認識する）。
- **この README の `python` コマンドは、venv を有効にした（activate した）ターミナルで、リポジトリ直下から実行する前提**。新しいターミナルを開いたら、もう一度 activate する。
- エージェントは `python` コマンドで本アプリの CLI を呼び出す。venv の Python で GUI を起動すれば、エージェントも同じ Python を使う。
- バージョンは固定していない。問題が出たら [動作環境](#動作環境) の動作確認済みバージョンに合わせる（例 `pip install "numpy<2"`）。

### 2. 起動

| 方法 | 説明 |
|---|---|
| `run.bat`（Windows・ダブルクリック可） | コンソールを出さずに起動する。`.venv` があればその Python、無ければ PATH 上の `pythonw` を使う |
| `python tool.py` | コンソール付きで起動する（macOS / Linux はこちら）。エラーを見たいときもこちら |
| `python tool.py --demo` | 合成データのデモタブ付きで起動する（データなしで GUI を試せる） |

- `run.bat` で起動してウィンドウが出ない場合は、コンソール付きで起動してエラーを確認する。PySide6 / pyqtgraph の不足・古い Python・登録簿（`datasets.local.json`）の破損は、`run.bat` ではログも残らない。
- 起動後の未捕捉例外は `data/logs/gui-crash-*.log` に記録される。
- **UI は英語で起動する。** メニューの **Settings → Language / 言語 → 日本語** で切り替える（即時反映・次回以降も保持）。以降の説明は日本語表記で書く。

### 3. AI エンジンを設定する

> **重要:** エンジンを設定しないままチャットを送信すると、既定の「OpenAI 互換 HTTP」が OpenAI の API（api.openai.com）へ接続しようとして、HTTP 401 エラーになる。ただし環境変数 `OPENAI_API_KEY` が設定されていると、エラーにならずに OpenAI の `gpt-4o-mini` へ送信され、そのキーに課金される。最初にエンジンを選ぶこと。

推奨は Claude Code。[R2 設定同期](#複数-pc-での設定同期) を使う 2 台目以降の PC では、ここで設定する前に [2 台目以降](#2-台目以降) の手順で `config-pull` する。

1. VS Code に Claude Code 拡張を入れてサインインする。VS Code を使わない場合は、Claude Code の公式手順で CLI を入れ（npm なら Node.js を入れてから `npm i -g @anthropic-ai/claude-code`）、`claude` を起動してログインする。
2. **設定 → バックエンドの状況…** で、該当エンジンの「導入」「認証」列が ✓ になっていることを確認する（CLI 版のエンジンなら、未導入のときここからインストール・ログインもできる。VS Code 拡張は VS Code 側で入れる。表示の注意は [バックエンドの状況ウィンドウ](#バックエンドの状況ウィンドウ)）。CLI では `python -m llm_bridge engines`（`OK` と表示される）。
3. **設定 → バックエンド/モデル設定…** の「利用方法（エンジン）」で **Claude（VS Code 同梱エンジン）**（CLI を入れた場合は **Claude（PATH の CLI）**）を選び、必要ならモデル（例 `opus`, `sonnet`）を入力して **適用** する。設定ファイル（`llm_backend/config.toml`, `models.toml`）は自動で作られ、再起動なしで反映される。

Codex CLI・pi・OpenAI 互換 HTTP（ローカル LLM を含む）の設定は [AI エンジンの設定](#ai-エンジンの設定) を参照。

> ⚠ 既定では、エージェントはあなたのユーザー権限で、確認なしにコマンドを実行する。[セキュリティとプライバシー](#セキュリティとプライバシー) を必ず読むこと。

---

## 基本的な使い方

### 1. データセットを登録する

データセットは「計測データを入れたフォルダ 1 つ」と「その名前」の組。

- **GUI**: **ファイル → データセットを新規登録** → フォルダを選択 → 名前を入力（既定はフォルダ名）して **OK** →「登録完了」と表示される。フォルダ名に空白が入っている場合などは、名前を変える必要がある（→ [登録](#登録)）。GUI で登録しただけではデータセットは開かない。
- **CLI**: `python -m llm_bridge register-dataset <名前> <絶対パス>`。GUI が起動中なら自動で開く。

詳しくは [データセット](#データセット)。

### 2. データセットを開く

**ファイル → データセットを開く…** で一覧から選んで **Open**（英語表示）を押す（ダブルクリックでも可）。前回保存したタブとチャットが復元される。この PC にパスが無いデータセットは開けない。

### 3. チャットで依頼する

右側のチャット欄に書いて **Ctrl+Enter**（または **Send**）で送信する（Enter は改行）。エージェントの作業中は **Stop** で止められる。図はタブに表示され、エージェントが保存したファイルはデータセットの出力フォルダ（既定 `_work/`）に置かれる。

以前に作った解析モジュール（→ [自作の解析モジュール](#自作の解析モジュール)）を開くメニューはない。「`<解析名>` を開いて」とチャットで頼むか、`python -m llm_bridge window add-tab name=<解析名> dataset=<データセット名> --wait` を使う（先にそのデータセットを開いておく）。

### 4. 保存して終了する

**タブ構成とチャットは自動保存されない。** **ファイル → セッションを保存** / **保存して終了** で保存する。未保存のまま閉じると「セッションを保存しますか？」と聞かれる（ボタンは英語表示の Yes / No / Cancel）。

次回は **ファイル → データセットを開く…**、または **ファイル → 前回のセッションを復元**（最後にセッションを保存したときに開いていたデータセットをまとめて開く。既に開いているものは閉じない）で再開する。詳しくは [保存データと再開](#保存データと再開)。

---

## データセット

### 登録簿 `datasets.local.json`

データセットの登録はリポジトリ直下の `datasets.local.json` に保存される（Git 管理外・PC ごと）。同期ドライブのマウント先は PC ごとに違うので、登録簿は「ホスト名 → その PC でのフルパス」を持つ。

```json
{
  "my_dataset": {
    "PC-A": "D:/Data/my_dataset",
    "PC-B": "E:/Sync/Data/my_dataset"
  }
}
```

- ホスト名のキーは **大文字**。確認は `python -c "import socket; print(socket.gethostname().upper())"`。手で書いた小文字のキーは一致しない。
- パスは絶対パス。JSON では `\` を `\\` と書くか `/` を使う。
- 新しく clone した直後は登録簿が無く、最初の登録でファイルが作られる。
- 手で編集するときは GUI を終了してから行う。**登録簿が壊れていると（空のファイルも含む）GUI も全 CLI も起動しない**（→ [トラブルシューティング](#トラブルシューティング)）。

### 登録

- **GUI**: **ファイル → データセットを新規登録**。この PC のパスだけを登録する。`myanalysis.toml` は作らず、データセットのタブも開かない（チャット欄はそのデータセットに切り替わる）。同じ名前で登録すると、確認なしにこの PC のパスが追加・上書きされる。
- **CLI**:

  ```bash
  python -m llm_bridge register-dataset <名前> <絶対パス> [--host HOST] [--format csv_per_subdir|custom] [--no-open]
  ```

  - この PC 向けでフォルダが存在すれば、`myanalysis.toml` を作り、`format` を書き込む（省略時は `csv_per_subdir`）。**既存の `format = "custom"` も上書きされる**ので、再登録時は `--format` を今の値に合わせる。
  - GUI が起動中なら自動で開く（最大約 10 秒待つ。GUI が起動していなくても約 10 秒待ってから終わる。`--no-open` で抑止）。`--host` で別の PC 向けに登録したときは開かない。
  - [R2 設定同期](#複数-pc-での設定同期) を設定していれば同期も行う。
- **名前の規則**: 空白・`/`・`\`・先頭の `.` は使えない。英数字・`_`・`-`・日本語を推奨し、大文字と小文字は区別される。次の名前は避ける。
  - 引用符や記号（`"` `'` `&` `$` など）を含む名前（雛形の生成やエージェントのコマンドが壊れる）。
  - 数値として読める名前（`000000`, `1.5`, `1e3` など。`window` / `tab` の `name=` などで数値と解釈され、指定できなくなる）。
  - `-` で始まる名前（CLI でオプションと解釈される）。
- **登録の削除**: **ファイル → データセットを開く…** で選んで **登録を削除**（この PC で開けない行も削除できる）。
  - 登録簿から外すだけで、フォルダ内のファイル（計測データ・解析・出力）は消えない。
  - 全 PC 分の登録が消える。[R2 設定同期](#複数-pc-での設定同期) を使っていれば、次回の同期（この PC の次回起動時か `config-sync`、その後に各 PC が同期したとき）で他の PC の登録からも削除される。
  - 元に戻すには、各 PC でそれぞれ登録し直す。
  - 開いているデータセットは先に閉じられる。タブ構成は保存されるが、**未保存のチャットは失われる**ので、先に **セッションを保存** する。
- **名前の変更**: 専用の機能は無い。登録を削除して、各 PC で新しい名前で登録し直し、各 `analyses/*/analysis.py` の `DATASET = "..."` も直す。

### 別の PC で使う

1. その PC でアプリをセットアップし、AI エンジンにログインする。
2. 同じ名前で、その PC でのパスを登録する。**GUI の「データセットを新規登録」が安全**（`myanalysis.toml` に触れない）。CLI で登録するなら `--format` を現在の値に合わせる。
3. **ファイル → データセットを開く…** で開く。

- データセットを開く一覧の「可用性」列が「このホストにパス無し」「ディレクトリ不在」の行は開けない（「ディレクトリ不在」は同期ドライブがマウントされていないことが多い）。
- **同じデータセットを 2 台で同時に開かない。** PC 間の排他制御は無く、後から保存した方が勝つ。「保存して終了」→ 同期の完了を待つ → 別の PC で開く、の順にする。
- 別の PC でチャットを続けると、最初の送信で会話履歴の全文を送り直す（エンジンの会話 ID は PC ローカルのため）。長いチャットではトークン消費が大きい。

### データセットの設定 `myanalysis.toml`

データセットのフォルダ直下に置かれる設定ファイル。データと一緒に同期され、全 PC で共通。CLI 登録時か、そのデータセットに初めて書き込むとき（解析タブを開く・エージェントが図などを保存する・セッションを保存する）に自動で作られる。手で編集してもよい。

| キー | 既定値 | 意味 |
|---|---|---|
| `work_dir` | `"_work"` | 出力先。相対パスはデータセットのフォルダ基準。絶対パスも書けるが全 PC 共通なので、ドライブ構成が違う PC では壊れる。`..`・ドライブ相対・ルート相対は不可。**後から変えると既存のセッションとチャットが見えなくなる** |
| `format` | `"csv_per_subdir"` | `csv_per_subdir` = 各サブフォルダに同名の CSV が 1 つずつある構成（エージェントの標準の読み込み関数が使える）。`custom` = それ以外の構成（読み込みは解析モジュールの `load()` に書く） |

- 未知のキーは無視される。`format` が上記以外の値のとき、ファイルが空のとき、TOML の構文が壊れているときは、そのデータセットを開けない（一覧では「利用可」と表示され、開くと「データセット … を開けません」になる → [トラブルシューティング](#トラブルシューティング)）。
- フォルダの構成は自由。エージェント（Claude / Codex / pi）はデータを読む前に実際の構成を調べる。
- 計測ファイル（CSV 等）をアプリが書き換えることはない。

### フォルダ構成（アプリが作るもの）

```
<データセット>/
├── (計測データ)                        読むだけ
├── myanalysis.toml                     データセットの設定
├── meta.json, meta.json.bak            概要・完了フラグ・一覧表示用の情報
├── analyses/<解析名>/
│   ├── analysis.py                     解析モジュール
│   ├── analysis.py.bak                 最後に正常に開けた版（自動）
│   └── README.md
└── _work/                              出力先（work_dir）
    ├── session.json(.bak)              タブ構成
    ├── chat_sessions/<id>.json(.bak)   チャット履歴
    ├── figures/  code/  ほか            エージェントが保存した図・コード・レポート
    └── analyses/<解析名>/
        ├── state/                      表示状態・スナップショット・注釈
        ├── batch/                      python -m export の出力
        └── analysis.draft.py           安全に編集するための下書き
```

- 「データセットを開く…」の一覧を開くと、`meta.json` が無い・古いデータセットには作成・更新される（開いたことのないデータセットも含む）。
- ローカルディスク上のデータセットでは `meta.json.lock` などのロックファイルもできる。
- `analyses/` の下で `_` から始まるフォルダは一覧に出ない。

---

## GUI の機能

### 画面構成

- **左**: 解析エリア。データセットを 2 つ以上開くと、上部に `◆ データセット名` の切替タブが出る。その下に解析タブが並ぶ。
- **右**: チャットドック（Chat）。左側にドッキング・フローティングもできる（閉じることはできない）。
- **下**: ステータスバー。
- 起動直後は `(empty)` タブだけで、前回の状態は自動では復元しない（`(empty)` タブの英語の案内文は開発者向けなので無視してよい）。
- ウィンドウの大きさ・チャットドックの位置は次回起動時に引き継がれない。チャットの文字サイズと、履歴欄・入力欄の境界は PC ごとに保存される。

### メニュー

| メニュー | 項目 |
|---|---|
| ファイル | 「データセットを新規登録」「データセットを開く…」「前回のセッションを復元」「セッションを保存」「保存して終了」「終了」 |
| 表示 | 「チャットを切り離す/格納」「ミーティング共有…」 |
| 設定 | 「言語 / Language」「ツール呼び出し表示」（通常・簡略・非表示）「プロバイダ既定のシステムプロンプト」「AIペルソナ…」「バックエンド/モデル設定…」「バックエンドの状況…」 |
| ヘルプ | 「バージョン情報」 |
| 開発 | 開発者向け（コードの再読み込み等）。通常は使わない |

### データセットを開く

**ファイル → データセットを開く…** の一覧ダイアログ。

- 列は 名前 / 概要 / 解析数 / 最終更新 / 可用性。「フィルタ（名前・概要）…」で絞り込める。開いているデータセットには ● が付く。
- 右側に詳細（形式・パス・解析名・前回タブ・注釈数・チャット数・ディスク使用量・サムネイル等）が出る。
- **概要を編集** / **完了にする**（完了済みは下の「完了済み (n)」に移るだけで、他は変わらない）/ **更新** / **登録を削除**（→ [登録](#登録)）。概要と完了フラグはデータセットの `meta.json` に保存され、全 PC で共有される。
- **Open**（英語表示）かダブルクリックで開く。この PC で利用できない行は開けない。

### データセットの切り替えと閉じ方

- 上部の `◆` タブで切り替える。ドラッグで並べ替えられる。チャット生成中のデータセットには ● が付く。
- `◆` タブを右クリック → **データセットを閉じる**。タブ構成を保存して閉じるだけで、ファイルは消さない（そのデータセットのチャットは次の「セッションを保存」で保存される）。
- データセットが 1 つだけのときは切替タブが出ないので、GUI からは閉じられない（CLI の `window close-dataset name=<ds>` なら閉じられる）。

### 解析タブ

- ドラッグで並べ替え。タブが多いと複数段になる。
- タブを右クリック → **タブ名をコピー** / **このタブにコメント**（チャット入力欄に `[タブ「…」へのコメント]` を差し込む）/ **フロート（切り離し）** / **タブを閉じる**。タブに × ボタンは無い。
- タブをバーから上下にドラッグして離すと、別ウィンドウになる（元の位置には薄い `↗ 名前` が残る）。別ウィンドウを閉じると元の位置に戻る。切り離した状態は保存されない。
- パネル間の境界はドラッグで調整でき、その比率は保存される。

### チャット

- **送信**: Ctrl+Enter または **Send**（Enter は改行）。作業中は **Stop** で止められる。複数のチャットタブで同時に生成でき、生成中のタブには ● が付く。
- **+** で新しいチャット。チャットはデータセットに紐づき、データセットを切り替えるとチャットタブも切り替わる。データセットに紐づいていないチャットは、送信時に前面にデータセットがあればそれに、無ければ表示中に次に開いた・切り替えたデータセットに紐づく（生成中は除く）。**どのデータセットにも紐づかないまま終了すると保存されない。**
- **先頭の灰色の行**: `backend: … / model: … [既定]` または `[個別]`（そのチャットが全体設定に従っているか、個別設定か）。ペルソナを使っていると `persona: … [既定]` / `[個別]` の行も出る。
- **使用量**: 応答が終わるたびに、Stop / Send の左に `🪙 in … · out … · ctx …/… (…%) · $…` を表示する（→ [使用量の表示](#使用量の表示)）。
- **ツール呼び出し表示**: Claude / Codex のツール実行を `🔧 名前 要約` / `↳ 結果` / `✗ エラー` の行で表示する。**設定 → ツール呼び出し表示** で 通常 / 簡略（1 行にまとめる）/ 非表示 を切り替えられる（表示だけの切替）。pi は `[ツール名 …]` の形で表示し、この設定の対象外。OpenAI 互換はツールの行を表示しない。
- **✎ 編集**（自分の発言の見出し）: その発言の直前までの履歴で新しいチャットを作り、発言文を入力欄に入れる（自動では送信しない）。**⑂ 分岐**（AI の応答の見出し）: その応答までの履歴で新しいチャットを作る。どちらも元のチャットは残る。
- **チャットタブの右クリック**: 名前変更 / このチャットのモデル… / このチャットのペルソナ… / ツール呼び出し表示 / アーカイブ / アーカイブ済みを表示… / 閉じる。
  - **閉じる = 削除**（確認あり。次の「セッションを保存」で確定する）。残しておきたいなら **アーカイブ**（一覧から隠すだけで、「アーカイブ済みを表示…」から戻せる）。
- エラーは本文の末尾に `[error: …]`、停止すると `[stopped]` と出る。多くのエラーはそのまま再送すれば回復する。
- 文字サイズは Ctrl + `+` / `-` / `0`（チャット欄にフォーカスがあるとき）。
- 画像の添付・貼り付けはできない。入力途中の文はアプリ終了で消える。

### 図ビューアと画像ビューア

エージェントが作った図は **図ビューア** のタブに表示される。

- ホイールで拡大縮小、ドラッグで移動。右クリック → **画像をコピー** / **50% に縮小**。
- 1 つのタブに 2 枚まで上下または左右に並べられる（チャットで頼むか、CLI の `window show` で `slot=` を指定する）。

**画像ビューア**（ImageJ 風）は顕微鏡画像などの表示用。

- 対応形式: TIFF（16bit・多ページ・最大 5 次元 TZCYX。複数シリーズの TIFF は最初のシリーズだけ。`tifffile` が必要）、PNG / JPEG / BMP / GIF / WEBP など（`Pillow` が必要）。
- チャンネルごとに表示 ON/OFF・LUT（10 種）・反転・最小/最大・Auto（自動コントラスト）・Reset。single / composite 表示、Z/T スライダー、マウス位置の画素値表示。ホイールで拡大縮小、ドラッグで移動。
- 開き方: `python -m llm_bridge window show-image path=<絶対パス> name=image dataset=<データセット名> --wait`（`name=` は図ビューアのタブ名 `viewer` と別にする。**先にそのデータセットを開いておく**。開いていないデータセットを指定すると、保存時にそのデータセットの保存済みタブ構成がこのタブだけで上書きされる）。OpenAI 互換エンジンのチャットでも開ける。Claude などのエージェントにはこの機能が案内されていないので、使うときは上のコマンドを示して依頼する。
- `dataset=` を付けないと、どのデータセットの出力フォルダ（work_dir）にも入っていない画像のタブは、どのデータセットにも属さずセッションに保存されない。`dataset=` を付けても、出力フォルダの外の画像は絶対パスで保存されるので、パスの違う別の PC では復元されない。
- 表示設定（LUT・レンジ・Z/T の位置・表示モード）はセッションに保存されない。ラベルや右クリックメニューは英語表記。

### キーボードショートカット

| キー | 動作 |
|---|---|
| Ctrl+Enter | チャットを送信 |
| Ctrl + `+` / `-` / `0` | チャットの文字サイズ（チャット欄にフォーカスがあるとき） |
| Alt+F / V / S / H | 各メニューを開く |
| Ctrl+F5 | 開発メニューの「コード再読み込み」（開発者向け。通常は押さない） |

---

## AI エンジンの設定

### エンジン一覧

| 利用方法（エンジン） | `[backend].name` | `models.toml` のセクション | 解析 | 必要なもの | 使用量の表示 |
|---|---|---|---|---|---|
| Claude（VS Code 同梱エンジン）**推奨** | `claude`（`bin` 空） | `[claude_code]` | ○ | VS Code / Cursor / Windsurf の Claude Code 拡張にサインイン（見つからなければ PATH 上の `claude` を使う） | 金額・トークン |
| Claude（PATH の CLI） | `claude`（`bin` 指定） | `[claude_code]` | ○ | `npm i -g @anthropic-ai/claude-code` → `claude` でログイン | 金額・トークン |
| Codex CLI（OpenAI） | `codex` | `[codex]` | ○ | `npm i -g @openai/codex` → `codex login` | トークンのみ |
| pi コーディングエージェント | `pi` | `[pi]` | ○ | Node.js 22.19+、`npm i -g @earendil-works/pi-coding-agent` → `pi` を起動して `/login`。Windows では Git Bash | なし |
| OpenAI 互換 HTTP | `openai` | `[openai-compat]` | ×（GUI 操作のみ） | `.env` の `OPENAI_BASE_URL` 等 | なし |
| モック（動作確認） | `mock` | — | × | 不要 | なし |

### 設定のしかた

- **設定 → バックエンド/モデル設定…**: 「利用方法（エンジン）」「モデル」「プロバイダ」（pi のみ）を選んで **適用**。全チャット（「このチャットのモデル…」で個別に設定したチャットを除く）の次の送信から反映される（生成中の応答は古い設定のまま完了する）。
  - モデル欄は自由入力。＋ / － で候補リストに追加・削除できる（`models.toml` に即保存）。空欄にするとエンジン側の既定モデルになる（OpenAI 互換では `OPENAI_MODEL`、それも無ければ `gpt-4o-mini`）。
  - **疎通確認** は実際に 1 ターン分を送るので課金される。**適用** は疎通確認をしない。
  - thinking / effort は GUI に項目が無い。`models.toml` を手で編集し、再起動するか GUI で **適用** し直す（→ [設定ファイル](#設定ファイル手動設定)）。
- **チャットごとの設定**: チャットタブを右クリック → **このチャットのモデル…**。「全体設定に従う」を外すと、そのチャットだけ別のエンジン・モデルにできる（チャットと一緒に保存・同期される）。

### バックエンドの状況ウィンドウ

**設定 → バックエンドの状況…** で、対応エンジン全部の導入状況を一覧できる（いま選んでいるエンジンとは無関係）。CLI では `python -m llm_bridge engines`。

- 列: ドライバ / 前提（node と npm）/ 導入（バージョン）/ 認証 / 操作。記号は ✓ = OK、✗ = 無い、? = 確認できなかった（タイムアウト等。未導入という意味ではない）、— = 対象外。
- **インストール / 更新**: どちらも `npm i -g <パッケージ>` を実行する。VS Code 同梱エンジンの行にはボタンが無い（拡張の更新で入手する）。「前提」が ✗（Node.js / npm が無い、pi には Node.js が古い）の行には出ないので、先に Node.js を入れる。「導入」が ? の行にも出ない（**再確認** するか、コマンドを手で実行して確かめる）。
- **ログイン**: 導入済みの Claude（PATH の CLI）/ pi / Codex の行に出る。Windows では新しいコンソールを開く。他の OS では実行すべきコマンドをログ欄に表示する。
- **疎通確認**: 実際にエンジンへ 1 ターン送る。このウィンドウで**唯一課金される操作**。ウィンドウを開く・**再確認** は導入状況を調べるだけで、推論は走らないので課金されない。
- 判定には認証ファイルを変更しない読み取り専用のコマンドだけを使う。
- インストール後も ✗ のままなら、アプリを再起動する（PATH は起動時のものを使うため）。
- Claude の「認証」列は `~/.claude/.credentials.json` の有無で推定しているだけ。API キーで使っている場合や macOS（Keychain 保存）では、ログイン済みでも ✗ になることがある。

### 会話の引き継ぎとコスト

- Claude / Codex / pi では、エンジンの会話 ID（再開用トークン）が PC ローカルに保存され、通常は新しい発言だけを送って会話を続ける。OpenAI 互換は毎回、履歴全体を送る。
- 次の場合は会話履歴の本文を全部送り直す（ツールの実行結果は含まれない）: エンジンを変えた / 「このチャットのモデル…」で **適用** した（モデルだけの変更でも）/ 別の PC で続けた / ✎ 編集・⑂ 分岐した / エラーの後 / そのチャットの最後の応答から 90 日以上たった。送り直す量に上限は無いので、長いチャットではトークン消費が大きい。
- 全体設定（バックエンド/モデル設定…）での同じエンジン内のモデル変更、ペルソナの変更、Stop では会話が維持される。

### 使用量の表示

応答が終わるたびにチャット下部へ `🪙 in … · out … · ctx …/… (…%) · $… (total $…)` を表示する。

| エンジン | 表示 |
|---|---|
| Claude | 入出力トークン・コンテキスト使用率・金額（Claude Code が報告する API 料金換算の値。サブスクリプションでログインしている場合は実際の請求額ではない） |
| Codex | 入出力トークン・コンテキストのみ |
| pi / OpenAI 互換 / モック | 表示なし |

`total` はそのチャットの累計で、設定の適用や再起動でリセットされる。`models.example.toml` をコピーした場合の設定（Claude の `opus` + `thinking = "enabled"` + `effort = "xhigh"`）は消費が大きい。

### 停止と同時実行

- 1 ターンの時間制限は無い。止めるには **Stop**（途中までの応答は残る）。
- 複数のチャットで同時に生成できる。それぞれ別プロセスで動き、別々に課金される。

### AIペルソナ（応答スタイル）

**設定 → AIペルソナ…** で、エージェントの口調・文体を選ぶ。

- 既定は「無し（お客様対応窓口）」（標準の丁寧な応対）。サンプルとして 2 つのペルソナが入っている。**追加…** / **削除** で編集できる。本文の変更は、ペルソナの切り替え・追加・削除・**適用** の時点で保存される（キャンセルしても、それまでに保存された分は残る）。名前の変更はできない。
- チャットタブを右クリック → **このチャットのペルソナ…** で、チャットごとに上書きできる。
- 変わるのは口調だけで、運用ルールが常に優先される。
- 定義は PC ローカル（`data/llm_state/personas.json`）で、他の PC には同期されない。定義が無い PC では「無し（お客様対応窓口）」として扱われる。

### プロバイダ既定のシステムプロンプト

**設定 → プロバイダ既定のシステムプロンプト**（チェック式・Claude のみ）。ON（既定）は Claude Code 標準のシステムプロンプトに MyAnalysis の指示を追記する。OFF は MyAnalysis の指示だけにする。既に使ったチャットには、再起動するか、バックエンド/ペルソナを適用するまで反映されない。このメニューで一度切り替えると、その PC では `config.toml` の `use_provider_system_prompt` より優先される。

### エンジン別の補足

- **Claude**: 実行ファイルは `[claude_code].bin` → 環境変数 `CLAUDE_CODE_BIN` → エディタ拡張の同梱版 → PATH 上の `claude` の順に探す。ログイン情報・設定は `~/.claude` を Claude Code と共有する。環境変数 `ANTHROPIC_API_KEY` があると、Claude Code 側の仕様で API キー課金になる場合がある。
- **Codex**: ツールの実行は逐次表示されるが、応答の本文は一度にまとめて表示される。
- **pi**: プロバイダの候補は `openai-codex` / `github-copilot` / `llama.cpp`（ローカル）。`thinking` / `effort` は使わない。Windows では bash（Git Bash 推奨）が無いとツール実行が失敗する。図を見て判断させるには、画像入力対応のモデルが必要。
- **OpenAI 互換 HTTP**（Ollama / LM Studio / llama.cpp server 等）:
  - `.env` に `OPENAI_BASE_URL`（`/v1` まで含める。例 `http://localhost:11434/v1`）、必要なら `OPENAI_API_KEY`。モデル名は `models.toml` の `[openai-compat].model`（無ければ `OPENAI_MODEL`）。
  - 毎回 GUI 操作用のツール定義を付けてストリーミングで送るので、**function calling に対応したサーバーとモデルが必要**。小さいモデルではツールをうまく使えない。
  - 通信のタイムアウトは 30 秒（応答の途中で 30 秒以上止まった場合も含む）。モデルの読み込みが遅いと失敗する。
  - このエンジンでは Python の実行・データの読み込み・図の保存はできない。ローカル LLM で解析まで行いたい場合は、pi + `llama.cpp` を使う。
- **モック**: 入力に関係なく定型文を返す動作確認用。応答のたびに stooq.com へ接続する。

### 設定ファイル（手動設定）

GUI で設定すれば手で書く必要はない。手で書く場合は、雛形をコピーして編集する。

| ファイル | 雛形 | 内容 |
|---|---|---|
| `llm_backend/config.toml` | [llm_backend/config.example.toml](llm_backend/config.example.toml) | エンジンの選択（`[backend].name`）と運用設定（`bin`, `cwd`, `permission_mode`, `allowed_tools`, `sandbox_mode`, `tools` 等） |
| `models.toml` | [models.example.toml](models.example.toml) | モデル設定（`model` / `thinking` / `effort` / `provider`） |
| `.env` | [.env.example](.env.example) | 秘密情報と環境変数（`OPENAI_*`, `LLM_BACKEND`, R2 同期、ミーティング共有、同期ドライブ用の設定） |

- **エンジンの選択順**: 環境変数 `LLM_BACKEND` → `config.toml` の `[backend].name` → `OPENAI_BASE_URL`（`mock` ならモック、それ以外は OpenAI 互換）。どれも無ければ OpenAI 互換（api.openai.com）になる。
- `[backend].name`（と環境変数 `LLM_BACKEND`）に書けるのは `claude` / `codex` / `pi` / `openai` / `mock` だけ。**それ以外の値だとアプリが起動しない**（`claude_code` や `openai-compat` はセクション名で、エンジン名ではない。原因は `data/logs/gui-crash-*.log` に残る）。
- `.env` に `LLM_BACKEND` を書くと、起動のたびに GUI での選択より優先される。
- `models.toml` の Claude 用の値: `thinking` = `enabled` / `adaptive` / `disabled`。`effort` = `low` / `medium` / `high` / `xhigh` / `max` / `ultracode`（`ultracode` は消費が大きい）。Codex の `effort` = `minimal` / `low` / `medium` / `high`。
- 手で編集したら、再起動するか GUI で **適用** し直す（起動時に一度だけ読む）。ただし `[backend].name` / `model` / `provider` を手で変えた場合は再起動する（ダイアログは古い値を表示するので、そのまま適用すると元に戻る）。
- TOML の構文が壊れていると黙って無視される。`config.toml` なら既定のエンジン（OpenAI 互換）に、`models.toml` ならエンジンの既定モデルに戻る。**適用** では壊れた `config.toml`（モック以外を選んだときは `models.toml` も）が、＋ / － では `models.toml` だけが `.bak` に退避して作り直される。**作り直したファイルには GUI が書くキーしか残らない**ので、手で書いた設定（`permission_mode` 等）は `.bak` から戻す。
- [R2 設定同期](#複数-pc-での設定同期) を使っていると、`config.toml` と `models.toml` の変更は他の PC にも配られる。
- Windows のパスは `"C:/Users/..."` の形で書く。GUI が書き換えるキー（`name`, `bin`, `model`, `provider`）に `'...'`（リテラル文字列）を使うと、GUI の **適用** が失敗する。
- `.env` の注意:
  - 読むのはリポジトリ直下の `.env` だけで、起動時に一度だけ読む。シェルの環境変数の方が優先される。
  - **行末コメントは使えない**（`KEY=value # メモ` は値が `value # メモ` になる）。
  - 前後の引用符は外される。`export ` 付きの行も読める。
  - `.env` の値はエージェントのプロセスにも渡る（→ [セキュリティとプライバシー](#セキュリティとプライバシー)）。

---

## 自作の解析モジュール

解析モジュールは `<データセット>/analyses/<解析名>/analysis.py` の Python 1 ファイルで、データと一緒に同期される。エージェントに作らせることも、自分で書くこともできる。

> 以下のコマンドはリポジトリ直下で、GUI と同じ venv の Python で実行する。

### 雛形の作成

```bash
python -m newanalysis <解析名> --dataset <データセット名> [--format csv_per_subdir|custom]
```

- `analysis.py` と `README.md` を作る。同名のフォルダがあると失敗する。
- `--format` はデータセットの設定を自動では読まない。`custom` のデータセットでは明示する。
- 雛形は編集しなくても開ける（中身は TODO 表示）。`load()` → `build_tab()` 内のパネル → 状態を返す関数 → 注釈ハンドラ → `build_export_figs()` の順に書き換える。

### 開き方

GUI に解析を開くメニューは無い。

- チャットで「`<解析名>` を開いて」と頼む。
- CLI: `python -m llm_bridge window add-tab name=<解析名> dataset=<データセット名> --wait 60`（先にそのデータセットを開いておく）。
- 保存したセッションに含まれていれば、データセットを開いたときに復元される。

### モジュールの約束

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
- **注釈**: `python -m llm_bridge annotate <解析名> marker|note k=v … [--dataset DS]`（またはエージェント）で追加した注釈が、`{"markers": [...], "notes": [...]}`（各要素は k=v を辞書にしたもの）としてハンドラに渡される（ファイル更新時は内部用の `_seq` キーも含まれるので、`ann.get("markers")` / `ann.get("notes")` で読む）。`tab.connect_annotations(handler)` を `attach_tab` の前に呼び、ハンドラで描画する。雛形のハンドラは空なので、書くまで何も表示されない。
- **使える部品**:
  - `AnalysisTab`（`gui.tab`）: `add_panel(key, widget, "top"|"left"|"right")`、`set_split_orientation("horizontal"|"vertical")`、`set_split_ratio(l, r)`、`register_command(verb, handler)` など。
  - `gui.panels`: `SelectorPanel`（コンボボックス）、`TrajectoryPanel`（散布図）、`ImagePanel`（配列画像）、`FigurePanel`（PNG 表示）。
  - `gui.imageviewer.attach_image_viewer(tab, image, panel="left")`（画像ビューア）。
- **独自コマンド**: `tab.register_command("select", handler)` で登録すると、`python -m llm_bridge tab <解析名> select session=... --wait` やエージェントから呼べる。`k=v` はキーワード引数として渡されるので、ハンドラの引数名をキーに合わせる。値は数値に見えれば数値に変換され、`true` / `false` は文字列のまま。返り値は結果の `result` に入り、例外は `status: error` になる。`set-split` / `snapshot` / `refresh-state` は予約済み。用途は解析の `README.md` に書いておく。
- **import**: `common.*` / `core.*` / `gui.*` / `config` は使える。同じフォルダに置いた補助 `.py` は import できないので、1 ファイルにまとめる。追加パッケージは GUI と同じ venv に入れる（Qt 以外はモジュール先頭で import してよい）。
- **`@dataclass` の注意**: `analysis.py` はモジュールとして登録されずに実行されるため、雛形の `from __future__ import annotations` があると `@dataclass` が `AttributeError` で失敗する。`@dataclass` を使うならこの行を外し、あわせて雛形の関数の型注釈（`QWidget` / `AnalysisTab` / `Figure`）を削除するか文字列（`"AnalysisTab"` 等）にする（外すだけだと `NameError` で開けない）。

### 編集後の反映

- タブを右クリック → **タブを閉じる** → 開き直す。
- または `python -m llm_bridge window reload scope=tab target=<解析名> --wait`。成否は出力の `result`（`reloaded-tab:…` / `reload-tab-error:…`）で確認する。失敗すると古いタブが残る。同じ名前のタブが複数のデータセットにあるときはアクティブなデータセットのものが対象（`dataset=` は使えないので、先に `window set-active-dataset name=<ds>`）。

### 同期ドライブ上で安全に編集する

rclone などのマウント上でエディタが「一時ファイルに書いて置き換える」方式で保存すると、ファイルが 0 バイトになることがある。次の手順を推奨する（ローカルディスクなら直接編集してよい）。

```bash
python -m llm_bridge draft-analysis <解析名> --dataset <データセット名>   # 下書きを作る（パスが表示される）
# 表示された analysis.draft.py を編集
python -m llm_bridge apply-analysis <解析名> --dataset <データセット名>   # 構文と build_tab を確認してから analysis.py に書き戻す
```

書き戻した後、開いているタブは [編集後の反映](#編集後の反映) の手順で読み込み直す。`draft-analysis` を再実行すると未反映の下書きは上書きされる。

`analysis.py` が 0 バイトになってしまったら `python -m llm_bridge recover-analysis <解析名> --dataset <データセット名>`（最後に正常に開けた版 `analysis.py.bak` から戻す。0 バイトか欠損のときだけ動く）。`.bak` はデータと同じドライブにあるので、大事な解析コードは Git 等でも管理する。

### エラーの確認

- CLI の `--wait` の出力（JSON の `status` / `error` / `result`）か、エージェントの返答で確認する。
- 解析が 0 バイト・見つからない・`build_tab` が無い・名前の不一致などは、エラーメッセージにそのまま出る。
- `load()` / `build_tab()` の例外は、型とメッセージだけが返る（トレースバックは出ない）。詳しく調べるときは、自分のスクリプトから `load()` を呼んでみる。
- `apply_state` の例外は、原因の書かれない `could not establish state`（再読み込みでは `apply_state / refresh-state failed`）というエラーになる。雛形のように `build_tab` の中で `refresh-state` を呼ぶ場合、状態を返す関数の失敗（JSON にできない値など）は `build_tab` の例外として型とメッセージが返る。
- 保存したセッションの復元に失敗したタブは、黙って飛ばされる。原因は `python tool.py` で起動したときのコンソールに出る。ボタン操作などで起きた未捕捉の例外は `data/logs/gui-crash-*.log` に残る。
- `load()` は GUI のスレッドで動くので、重い読み込みの間はウィンドウが固まる。CLI では `--wait 120` のように長めに待つ。

### PNG の一括出力（GUI 不要）

```bash
python -B -m export <データセット名> <解析名>
```

`load()` の結果を `build_export_figs()` に渡し、返された図を `<work_dir>/analyses/<解析名>/batch/` に PNG で保存する（`load()` が `None` を返すと失敗する）。`-B` は同期ドライブ上にキャッシュファイルを作らないため。

### Python から直接使う

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

---

## CLI リファレンス

```bash
python -m llm_bridge <verb> ...
```

- リポジトリ直下で、GUI と同じ venv の Python で実行する（別の場所からなら `PYTHONPATH=<リポジトリ>` を設定）。
- `window` / `tab` は、起動中の GUI にコマンドを渡して実行させる。仕組みは `data/llm_state/commands/` にファイルを置くだけなので、次の点に注意する。
  - **GUI が起動していなくてもエラーにならない**（ID を表示して終了コード 0）。そのコマンドは次回起動時に捨てられる。起動確認には `python -m llm_bridge window list-tabs --wait 3`（終了コード 1 なら未起動か無応答）。
  - `--wait [秒]`（既定 30 秒）は**必ず末尾に置く**。結果の JSON を表示する。タイムアウトすると終了コード 1 だが、GUI が動いていればコマンドは後で実行される（未起動なら次回起動時に捨てられる）。`--wait` を付けないと、結果は `data/llm_state/command_log.jsonl` に記録されるだけ。
  - **終了コード 0 でも成功とは限らない。** JSON の `status` が `ok` 以外（`error` / `rejected` / `stale` など）なら理由は `error` にある。`ok` でも `result` が `error:…` や `false` なら失敗。
  - `path=` は絶対パスで渡す。`k=v` の値は数値に見えれば（`1e3` などを含む）数値に変換される。
- 出力をファイルやパイプに流すときに日本語が化ける場合は、環境変数 `PYTHONUTF8=1` を設定する。

### 主な verb

| verb | 引数 | GUI | 用途 |
|---|---|---|---|
| `list-datasets` | `[--json]` | 不要 | 登録済みデータセットの一覧 |
| `register-dataset` | `<名前> <絶対パス> [--host H] [--format csv_per_subdir\|custom] [--no-open]` | 不要 | データセットの登録（→ [登録](#登録)） |
| `engines` | — | 不要 | AI エンジンの導入状況 |
| `doctor` | `[--dataset DS] [--repair] [--rescue] [--cache DIR] [--log FILE]` | 不要 | 同期ドライブ上のファイルの点検（→ [doctor](#doctor点検と修復)） |
| `recover-analysis` | `<解析名> [--dataset DS]` | 不要※ | 0 バイトになった `analysis.py` を `.bak` から戻す |
| `draft-analysis` / `apply-analysis` | `<解析名> [--dataset DS]` | 不要※ | 解析コードを安全に編集する（→ [同期ドライブ上で安全に編集する](#同期ドライブ上で安全に編集する)） |
| `annotate` | `<解析名> marker\|note [k=v …] [--dataset DS]` | 不要※ | 解析タブに注釈（marker / note）を追加する（表示は解析モジュールの実装次第） |
| `clear-annotations` | `<解析名> [marker\|note] [--dataset DS]` | 不要※ | 注釈を消す |
| `config-sync` / `config-push` / `config-pull` | `[--dry-run] [--key K]`（sync / push は `[--include-env]` も） | 不要 | R2 設定同期（→ [複数 PC での設定同期](#複数-pc-での設定同期)） |
| `list-analyses` | `[--dataset DS] [--json]` | 不要※ | 開いているデータセットの解析一覧 |
| `active` / `list-open-datasets` | — | 不要※ | アクティブなタブ・データセット |
| `state` | `[解析名] [--dataset DS]` | 不要※ | 解析の表示状態（`current.json`） |
| `window` | `<サブコマンド> [k=v …] [--wait [秒]]` | **必要** | ウィンドウ操作（下表） |
| `tab` | `<タブ名> <サブコマンド> [k=v …] [--wait [秒]]` | **必要** | タブ操作（下表） |

※ GUI が最後に書いた状態ファイル（`active.json`）を使う。GUI を閉じた後は前回の状態が出る。`--dataset` を省略すると、GUI で最後に前面（アクティブ）だったデータセットが対象になるので、書き込む verb では明示する。

### `window` のサブコマンド

| サブコマンド | 引数 | 結果の例 |
|---|---|---|
| `open-dataset` | `name=<ds>` | `restored:N` / `no-session:<ds>` / `unreadable-session:<ds>`（session.json が壊れていてタブを復元できない）/ `error:<ds>` |
| `set-active-dataset`（別名 `switch-dataset`） | `name=<ds>` | `active:<ds>` |
| `close-dataset` | `name=<ds>` | `closed:<ds>:<n>` / `error:<ds>`（保存に失敗） |
| `list-open-datasets` | — | 開いているデータセットとアクティブ |
| `add-tab` | `name=<解析名> [dataset=<ds>]` | `added:` / `already-present:`（先に `open-dataset` する） |
| `close-tab` / `set-active-tab` | `name=<タブ名> [dataset=<ds>]` | `true` / `false` |
| `list-tabs` | `[detail=1]` | タブの一覧 |
| `show` | `path=<絶対パス> [name=viewer] [slot=left\|right\|top\|bottom] [dataset=<ds>]` | 図ビューアに表示。`slot=right` / `bottom` で 2 枚目を並べる → `shown:<名前>` / `updated:<名前>` |
| `show-image` | `path=<絶対パス> [name=viewer] [panel=left\|right] [slot=right\|bottom] [dataset=<ds>]` | 画像ビューアに表示。図ビューアと同じ名前のタブは使えないので `name=` を別にする |
| `toggle-chat-float` | — | チャットの切り離し/格納 |
| `meeting-start` / `meeting-token` / `meeting-lan-link` / `meeting-stop` | → [ミーティング共有](#コマンドから操作する) | |
| `reload` | `scope=tab target=<解析名>` | 解析タブの再読み込み → `reloaded-tab:<名前>` / `reload-tab-error:…` / `reload-busy:…`（エージェントの応答中などは実行されない） |

### `tab` のサブコマンド

- 全タブ共通: `set-split left=<n> right=<n>`（左右の比率）、`snapshot`（解析タブの表示を `<work_dir>/analyses/<解析名>/state/current_view.png` に保存。図・画像ビューアのタブでは何もしない）。
- 画像ビューアのタブ: `set-lut lut=<Grays|Red|Green|Blue|Magenta|Cyan|Yellow|Fire|Ice|Spectrum> [invert=true]`、`set-range min= max=`、`auto-contrast [low=0.35] [high=99.65]`、`set-channel index=`、`set-mode mode=single|composite`、`set-z index=`、`set-t index=`、`set-visible channel= visible=true|false`、`load-image path=`。`set-lut` / `set-range` / `auto-contrast` は `channel=<n>` も取る（省略時は選択中のチャンネル）。番号は 0 から。
- 解析タブ: 各 `analysis.py` が登録したコマンド。
- `tab` コマンドは対象のタブを前面に出す。同じ名前のタブが複数のデータセットにあるときは `dataset=<ds>` を付ける。

### 例

```bash
python -m llm_bridge list-datasets --json
python -m llm_bridge register-dataset my_ds "D:/Data/my_ds" --format csv_per_subdir
python -m llm_bridge window open-dataset name=my_ds --wait 120
python -m llm_bridge window add-tab name=my_analysis dataset=my_ds --wait 60
python -m llm_bridge window show path="D:/Data/my_ds/_work/figures/overview.png" dataset=my_ds --wait
python -m llm_bridge window show-image path="D:/Data/my_ds/raw/cell01.tif" name=raw dataset=my_ds --wait
python -m llm_bridge tab raw auto-contrast --wait
python -m llm_bridge tab my_analysis set-split left=2 right=1 dataset=my_ds --wait
```

### その他の CLI

| コマンド | 用途 |
|---|---|
| `python -m newanalysis <解析名> --dataset <ds> [--format …]` | 解析モジュールの雛形を作る |
| `python -B -m export <ds> <解析名>` | PNG の一括出力 |

---

## 複数 PC での設定同期

Cloudflare R2（オブジェクトストレージ）を使って、PC 間で次の設定を同期する任意機能。設定しなければ何も起きない。

- **同期するもの**: `datasets.local.json`（全 PC のパス）、`models.toml`、`llm_backend/config.toml`。`.env` は `--include-env` を付けたときだけ送り、受け取る側では `.env.pulled` に保存する（`.env` は上書きしない）。
- **同期しないもの**: 計測データ・解析コード・`myanalysis.toml`（これらは同期ドライブで共有する）。ペルソナ定義。

### 1 台目のセットアップ

1. venv を有効にして `python -m pip install boto3`（GUI と同じ Python に入れる。別の Python に入れると自動同期が黙って何もしない）。
2. Cloudflare のダッシュボードで R2 のバケットを作る（**必ず非公開**）。
3. R2 の API トークンを作る。権限は「オブジェクトの読み取りと書き込み」（Object Read & Write）を**そのバケットに限定**する。表示される Access Key ID と Secret を控える。
4. `.env` に書く（**行末にコメントを付けない**）。

   ```
   R2_ENDPOINT_URL=https://<アカウントID>.r2.cloudflarestorage.com
   R2_BUCKET=<バケット名>
   R2_ACCESS_KEY_ID=<キー>
   R2_SECRET_ACCESS_KEY=<シークレット>
   ```

   endpoint にバケット名は含めない。任意で `R2_PREFIX`（既定 `config/`）。
5. `python -m llm_bridge config-sync --dry-run` で確認してから、`python -m llm_bridge config-sync`。

### 2 台目以降

1. [セットアップ](#セットアップ) の「1. インストール」だけを行い、`python -m pip install boto3`。
2. `.env` に 1 台目と同じ R2 の 4 行（`R2_PREFIX` を変えた場合はそれも）を書く。
3. **AI エンジンの設定（GUI の「適用」・＋ / －）や雛形のコピーをする前に** `python -m llm_bridge config-pull` を実行する（R2 を設定してから GUI を起動すれば、起動時の同期でも取り込まれる）。先に設定ファイルを作ると、更新時刻が新しい方が勝つ規則により、その最小限の内容が他の PC の設定を上書きしてしまう。
4. 各データセットを、この PC のパスで登録する（→ [別の PC で使う](#別の-pc-で使う)）。

先に設定ファイルを作ってしまった場合は、同期する前に `models.toml` と `llm_backend/config.toml` を削除してから `config-pull` する。既に送ってしまった場合は、正しい設定を持つ PC で、**GUI を起動する前に**（起動時の同期で上書きされるため。または `.env` に `R2_AUTOSYNC=0` を入れてから）ファイルを編集して保存し直し、`config-sync` する。その PC が既に同期してしまっていれば正しい内容は失われているので、設定し直す。

### 自動同期と手動コマンド

- GUI の起動時と、CLI の `register-dataset` の後に自動で双方向同期する。GUI でのデータセットの登録・**登録の削除**・設定の変更は、この PC の次回起動時か `config-sync` を実行したときに送られ、他の PC にはその PC の次回起動時（か `config-sync`）に届く。
- 失敗しても起動は止まらない（通常は数秒で打ち切り、何も表示しない）。止めるには `.env` に `R2_AUTOSYNC=0`。
- 手動: `config-sync`（双方向）/ `config-push`（R2 側だけ書く）/ `config-pull`（ローカル側だけ書く）。どれもマージした結果を書くもので、片側で強制的に上書きするものではない。`--dry-run` で予定だけ表示。`--include-env` は `config-sync` / `config-push` だけ。
- 自動同期が効かないときは、まず `python -m llm_bridge config-sync --dry-run` でエラーと警告を確認する。起動時の自動同期の例外を見るには `R2_DEBUG=1` を設定し、`python tool.py` をコンソールから起動する（`run.bat` では表示されない）。
- Cloudflare R2 専用。他の S3 互換ストレージは未検証。

### 競合の規則

- 登録簿は和集合で統合する。自分の PC のパスは常にローカルが優先。
- **登録の削除は「登録を削除」で行ったときだけ他の PC に伝播する。** `datasets.local.json` を手で編集して消したエントリは、次の同期で他の PC や R2 から復活する。削除の記録は `~/.myanalysis/config_share_state.json` にあるので、同期する前にこのファイルを消さない。
- 設定ファイルはファイル単位で、更新時刻が新しい方が勝つ。両方の PC で編集すると、古い方の変更は失われる。PC の時計を合わせておく。
- ホスト名が同じ PC を 2 台使わない（互いのパスを上書きし合う）。
- `config.toml` の PC 固有の値も他の PC に配られる。Claude / Codex の実行ファイルのパスを PC ごとに変えるなら、`[claude_code].bin` / `[codex].bin` を空にし、各 PC の `.env` の `CLAUDE_CODE_BIN` / `CODEX_BIN` で指定する（`bin` が空でないと環境変数は無視される。GUI で「Claude（PATH の CLI）」を選ぶと `bin = "claude"` が書かれる）。各セクションの `cwd` と `[pi].bin` は PC ごとに変えられないので、絶対パスを書かない。
- R2 同期を使う全 PC で、アプリを同じ版にそろえる（古い版は新しい形式の同期データを読めず、その PC では同期が止まる。起動時には何も表示されない）。

### R2 から .env を消すには

`--include-env` で送った `.env`（R2 の鍵や API キーを含む）は、自動では消えない。

1. 全 PC の GUI を終了する。
2. R2 のダッシュボードで `config/bundle.json`（`R2_PREFIX` を変えていればその下）を削除する。
3. 1 台で `config-sync` を実行する（`.env` を含まない bundle が作り直される）。
4. 各 PC の `.env.pulled` を削除し、送ってしまった鍵は再発行する。

bundle を削除しても、登録や設定は各 PC の同期で元に戻る。不要な登録は「登録を削除」で、設定は各 PC で直す。

> バケットには全 PC のホスト名とフルパスが入る。`--include-env` を使うと API キーなどが**平文**で保存される。受け取った `.env.pulled` はアプリからは読まれないので、必要な行を手で `.env` に写す。

---

## ミーティング共有

自分の解析タブ（図）とチャットを、離れた相手のブラウザへライブで共有する。相手もチャットで AI に指示できる。

> [!WARNING]
> - **参加リンク（招待トークン）を持つ人は誰でも、あなたの PC の AI エージェントに承認なしで指示できる。** 既定の権限設定では、あなたのユーザー権限でできること全て（シェルコマンドの実行、ファイルの読み書き・削除、`.env` の API キーの読み取り、別のデータセットを開くことなど）が可能。信頼できる相手にだけ、会議ごとに渡すこと。相手の指示による利用料もあなたに課金される。
> - 既定では、**開いている全タブ**（解析タブ・図ビューア・画像ビューアのタブ。データセット未設定のビューアタブも含む）と、**このアプリの起動中に開いたことのある全データセット（閉じたものも含む）のチャットとデータセット未設定のチャット**が共有される。チャットは**開始前の過去の履歴も含む**。ツール実行の内容（コマンド・パス・出力）も相手に届く。開いているデータセットの名前は常に見え、チェックを外しても参加者はそのデータセットで新しいチャットを作れる。
> - **データセットを閉じても、そのチャットは共有の対象に残る。** 非公開にしたいチャット・タブは、**「開始」を押した後、リンクを渡す前に**チェックを外す（「現在の dataset にありません」と付いたチャットも含む）。現状、開始前のチャット・タブのチェック変更は反映されない。「停止」→「開始」すると公開範囲は全公開に戻るので、外し直す。
> - 通信は Cloudflare を経由する（Cloudflare 上で暗号化が解かれる）。LAN リンクは暗号化されない http で、共有中はリレーが全ネットワークインターフェースで待ち受ける（トークンも平文で流れる）。
> - 相手を個別に退出させることはできない。「停止」→「開始」で新しいリンクになり、古いリンクは全員分が無効になる。参加者名は自己申告。参加者の発言と AI の応答は、あなたのチャット履歴に保存される。
> - 「ミーティング共有」ウィンドウを閉じても共有は止まらない。止めるには **停止** を押すか、期限切れを待つか、アプリを終了する。

### 事前準備（1 回だけ）

1. `.env` に `RELAY_ADMIN_KEY` を設定する（任意の長い秘密文字列。例: `python -c "import secrets; print(secrets.token_urlsafe(32))"` の出力）。空なら共有機能が無効になるだけ。
2. 外部から参加してもらうには、Cloudflare の named tunnel が必要。Cloudflare の無料アカウントと、ネームサーバーを Cloudflare に向けた**自分のドメイン**が要る（trycloudflare の一時トンネルには対応していない）。
   - cloudflared を入れる（Windows: `winget install cloudflare.cloudflared`）。
   - `cloudflared tunnel login` → `cloudflared tunnel create <トンネル名>` → `cloudflared tunnel route dns <トンネル名> <ホスト名>`
   - `.env` に `CLOUDFLARE_TUNNEL_NAME=<トンネル名>` と `CLOUDFLARE_TUNNEL_HOSTNAME=<ホスト名>`（任意で `CLOUDFLARED_BIN`, `CLOUDFLARE_TUNNEL_CRED`）。
3. `.env` は起動時に一度だけ読むので、編集したらアプリを再起動する。

詳しい手順（別の PC でトンネルを使う方法、LAN 公開時の注意など）は [relay-worker/README.md](relay-worker/README.md)。

### 共有を始める

1. **表示 → ミーティング共有…** を開き、「有効期間（最大24h）」（既定 3 時間）と「表示名」を決めて **開始**。
2. 状態が「● トンネル起動中…」→「● 共有中」になったら、「公開チャットセッション（全dataset）」「公開解析タブ」のチェックで公開範囲を調整する。「新規セッションを自動共有」で、開始後に作ったチャットの扱いを決める。
3. **参加リンクコピー**（`https://<ホスト名>/#token=…`）で相手に渡す。**トークンコピー** の場合、相手は `https://<ホスト名>/` を開いてトークンを貼る。
4. 「参加者」「ログ」で様子を確認し、終わったら **停止**。

参加者はリンクを開いて表示名を入れ、**Join / 入室** を押す。できること: データセットの切り替え（「ホストに追従」も可）、タブの図の閲覧（静止画・拡大・コピー・ダウンロード）、共有チャットの閲覧（過去の履歴も取得できる）と送信、新しいチャットの作成。他の参加者の発言も見える。

### LAN リンク（任意）

会場のネットワークがトンネルのホスト名を解決できない場合に、ホスト PC へ LAN で直接つなぐ 2 本目のリンクを出せる。

- 共有ウィンドウで、**開始する前に**「LAN リンクも出す（LAN 直結・http）」を ON にし、「ホスト IP」を確認する（自動検出値は必ず確かめる）。開始後に LAN を追加するには「停止」→「開始」（新しいリンクになる）。
- **LAN 参加リンクコピー** でリンク全体を渡す（トークン単体では使えない）。
- `.env` の `RELAY_LAN_HOST`（IP の初期値）、`RELAY_LAN_PORT`（固定ポート。使用中なら開始に失敗する）。
- Windows ファイアウォールでの許可、参加者側ブラウザの「HTTPS のみ」モードの解除が必要な場合がある。
- 外部リンクが失敗しても、「外部リンク使用不可（LAN リンクのみ有効）」として LAN だけで続けられる。

### コマンドから操作する

```bash
python -m llm_bridge window meeting-start [lan=true] [ttl_sec=3600..86400] --wait   # 多くは "starting" が返る
python -m llm_bridge window meeting-token --wait      # トークン（または starting / tunnel_failed / idle 等の状態）
python -m llm_bridge window meeting-lan-link --wait   # LAN の参加リンク（lan=true には RELAY_LAN_HOST が必要）
python -m llm_bridge window meeting-stop
```

参加リンクは `https://<CLOUDFLARE_TUNNEL_HOSTNAME>/#token=<トークン>`。`ttl_sec` の既定は 10800（3 時間）で、範囲外の値は丸められる。CLI から開始すると全公開になる（公開範囲を絞るには GUI のウィンドウを使う）。

### うまくいかないとき

| 表示 | 対処 |
|---|---|
| 「ミーティング共有を使うには .env に RELAY_ADMIN_KEY を設定してください。」 | `.env` に設定して再起動 |
| 「トンネル起動に失敗しました」 | 画面には原因が出ない。よくある原因は `CLOUDFLARE_TUNNEL_NAME` / `CLOUDFLARE_TUNNEL_HOSTNAME` が未設定（または編集後に再起動していない）、cloudflared が PATH に無い。それ以外は `cloudflared tunnel run <トンネル名>` を手で実行してエラーを見る。LAN リンクを使っていなければ共有は停止されるので、直してから再度 **開始** |
| 参加者に Cloudflare のエラー 1033 | ホストが共有していない（停止・期限切れ・アプリ終了）か、`route dns` とホスト名の不一致 |
| 参加者側「ミーティングが終了しました」 | 新しいリンクが必要 |
| 回線が細い | `.env` の `RELAY_VIEW_MAX_MP`（送る図の最大画素数。既定 8 メガピクセル）を 2〜4 程度に下げて再起動する（数値以外を書くとアプリが起動しない） |

---

## 保存データと再開

### 保存されるタイミング

- **明示的に保存するもの**: タブ構成とチャット。**ファイル → セッションを保存** / **保存して終了**、または終了時の確認で Yes。
  - タブ構成の保存に失敗すると終了しない（もう一度閉じて No を選べば保存せずに終了する）。
  - **チャットの書き込みに失敗しても終了は止まらない**（ステータスバーに「保存に失敗しました（検証NG）」が出るだけ）。同期ドライブでは、先に **ファイル → セッションを保存** してステータスバーにエラーが出ないことを確かめてから終了する（「保存して終了」ではメッセージを見る前にウィンドウが閉じる）。
- **その場で書かれるもの**: 解析タブの表示状態・注釈・データセットの概要と完了フラグ・エージェントが保存した図やコード・表示設定。
- チャットはデータセットに紐づけて保存される。どのデータセットにも紐づいていないチャットは保存されない（終了時の確認も出ない）。紐づいていないチャットは、送信時に前面にデータセットがあればそれに、無ければ表示中に次に開いた・切り替えたデータセットに紐づく（生成中は除く）。
- チャットの **閉じる** は削除（次の保存で確定）。**データセットを閉じる** はタブ構成だけを保存する。
- **復元されないもの**: ウィンドウの位置・大きさ、タブの切り離し状態（タブ自体は元のバーに戻って復元される）、図の拡大状態、画像ビューアの表示設定、チャットの入力途中の文。

### 再開のしかた

| 方法 | 動作 |
|---|---|
| ファイル → データセットを開く… | 選んだデータセットのタブとチャットを復元する |
| ファイル → 前回のセッションを復元 | 前回保存したときに一緒に開いていたデータセットをまとめて開く（既に開いているものは閉じない）。記録は PC ローカル |
| `python -m llm_bridge window open-dataset name=<ds> --wait` | 起動中の GUI で開く |

別の PC で続けるときは [別の PC で使う](#別の-pc-で使う)。引き継がれるのは、タブ構成・チャット（本文・タイトル・アーカイブ状態・チャットごとのエンジンとペルソナの指定）・解析コード・出力・注釈・概要。引き継がれないのは、エンジンの会話 ID（最初の送信で全文を送り直す）・ペルソナの定義・表示設定。

### データの保存場所

**データセット側**（同期される）: [フォルダ構成](#フォルダ構成アプリが作るもの) を参照。

- 大事なもの: `analyses/*/analysis.py`、`work_dir` 全体（特に `chat_sessions/`・図・コード・レポート・注釈）、`meta.json`（概要・完了フラグ）、`myanalysis.toml`。
- 作り直せるもの: `state/current.json`・`current_view.png`・`batch/`・`analysis.draft.py`（apply 済みなら）。
- `*.bak` は同じドライブにあるのでバックアップにはならない。チャットやセッションのファイルを消すときは、本体と `.bak` の両方を消す（本体だけ消すと `.bak` から復活する）。チャットの削除は GUI から行う。

**リポジトリ内**（Git 管理外。R2 同期を使うと、登録簿と `config.toml`・`models.toml` は他の PC と同期される）:

| パス | 内容 |
|---|---|
| `datasets.local.json` | データセットの登録簿 |
| `llm_backend/config.toml`, `models.toml`, `.env` | 設定 |
| `data/llm_state/personas.json` | ペルソナの定義（他にコピーが無いので必要ならバックアップ） |
| `data/llm_state/` のその他 | 表示設定・最近開いたデータセット・前回のワークスペース・エンジンの会話 ID・コマンド履歴（`command_log.jsonl`） |
| `data/logs/` | クラッシュログ |
| `data/locks/`, `data/pycache/` | ロックファイル・Python のキャッシュ（GUI と CLI を止めてから削除してよい） |

**ホームディレクトリ**:

| パス | 内容 |
|---|---|
| `~/.myanalysis/agent_home/` | Claude の作業フォルダ（エージェントの一時ファイルが溜まることがある） |
| `~/.myanalysis/codex_home/` | Codex の作業フォルダ（`AGENTS.md` は自動生成） |
| `~/.myanalysis/config_share_state.json` | R2 同期の状態と、登録の削除の記録（同期する前に消すと、削除した登録が復活する） |
| `~/.claude/`, `~/.codex/`, `~/.pi/` | 各エンジン自身の設定と会話記録（エンジン側の管理） |

---

## 同期ドライブとトラブルシューティング

### 同期ドライブでの書き込み

rclone などのマウントでは、「一時ファイルに書いて置き換える」方式の保存が、エラーも出さずにファイルを 0 バイトにしてしまうことがある。MyAnalysis は保存先のファイルシステムを自動で判定し、壊れやすいもの（fragile）では直接上書きしてから読み戻して確認する（最大 5 回・約 6 秒まで試し、それでも駄目なら保存失敗として扱う）。失敗するとステータスバーに「保存に失敗しました（検証NG）…」と出る。

| 保存先 | 判定 |
|---|---|
| rclone をドライブ文字にマウント（WinFsp） | fragile |
| ネットワークドライブ（SMB / NAS）、UNC パス（`\\server\share`） | fragile |
| Linux の FUSE（rclone / sshfs 等）・CIFS・NFS | fragile |
| OneDrive / Dropbox / Google ドライブのミラー（C: などの下のフォルダ） | local |
| Google ドライブのストリーミング（仮想ドライブ文字） | ドライブが報告するファイルシステム名による（doctor で確認する） |
| **rclone をフォルダにマウント**（例 `C:\mnt\sync`） | **local と誤判定される** → ドライブ文字でマウントするか、下の `MYANALYSIS_FS_OVERRIDE` で指定する |
| macOS（すべて） | 自動では local。必要なら `MYANALYSIS_FORCE_FRAGILE=1` |

- 判定は `python -X utf8 -m llm_bridge doctor --dataset <ds>` の最初の節で確認できる。判定はドライブごとにアプリ起動中は記憶されるので、マウントを変えたらアプリを再起動する。
- 読み戻し確認はキャッシュを読むので、「クラウドへのアップロード完了」を意味しない。PC を終了する前に同期の完了を待つ。
- 同じデータセットを 2 台で同時に開かない。
- rclone 上で `myanalysis.toml` や `analysis.py` をエディタで編集した後は、開けるか確認する（`analysis.py` は [draft → apply](#同期ドライブ上で安全に編集する) の手順を推奨）。

**判定と書き込み方式の上書き**（OS の環境変数として設定する。`.env` に書いた `MYANALYSIS_*` は GUI にしか効かず、ターミナルから実行する CLI には効かない）:

| 環境変数 | 意味 |
|---|---|
| `MYANALYSIS_FS_OVERRIDE` | 判定の上書き。例 `M:=fragile,C:\mnt\sync=fragile`（前方一致） |
| `MYANALYSIS_WRITE_STRATEGY` | `auto`（既定）/ `inplace` / `replace`（旧方式。rclone では危険） |
| `MYANALYSIS_FORCE_FRAGILE` | `1` で全パスを fragile として扱う |
| `PYTHONPYCACHEPREFIX` | Python のキャッシュをローカルに置く。**`.env` では効かない**ので、起動前に OS / シェルで設定する（`run.bat` は自動で設定する）。`python tool.py` や `python -m export` を手で実行するときは自分で設定するか、`PYTHONDONTWRITEBYTECODE=1`（または `python -B`） |

### doctor（点検と修復）

```bash
python -X utf8 -m llm_bridge doctor [--dataset <ds>] [--repair] [--rescue --cache <rcloneのキャッシュ>] [--log <rcloneのログ>]
```

- `-X utf8` を付ける（付けずに出力をファイルやパイプへ流すと、日本語版 Windows では `UnicodeEncodeError` で落ちる）。`--dataset` で対象を絞ると速い。
- 出力は 4 節: ファイルシステムの判定 / データセットごとの点検（0 バイトのファイル、壊れた `myanalysis.toml`、本体と `.bak` の食い違い）/ PC ローカルの状態ファイル / rclone のキャッシュとログ。`!` は要対応（1 つでもあれば終了コード 1）、`-` は情報。空であるべきファイル（空の `__init__.py` 等）も `!` として報告される。
- データセットごとの節に `[<ds>]` の行が出ていなければ、そのデータセットは点検されていない（名前の誤り・フォルダが見えない）。この場合も終了コードは 0 になる。
- `--repair`: 本体と `.bak` の食い違いを新しい方に揃える。PC ローカルの 0 バイトの状態ファイルを削除する（自動で作り直される）。他の PC を閉じ、同期が落ち着いてから実行する。
- `--rescue`: 0 バイトになったファイルを、rclone のキャッシュに残った一時ファイルから復元する（0 バイトのファイルだけが対象）。**必ず `--cache` と一緒に使う**（省略すると開発者の環境の既定パスを探すので、常に「復旧候補なし」になる）。`--rescue` を付けずに実行すると復元候補の一覧だけが出るので、先に確認する。
- `--cache` には `<キャッシュ先>/vfs/<リモート名>` を渡す。キャッシュ先は mount の `--cache-dir`、指定していなければ `rclone config paths` の Cache dir。`vfsMeta` を含む上位のフォルダを渡すと、rclone のメタデータを誤って復元するので渡さない。
- rclone の節は、`--cache` と `--log`（mount の `--log-file` に指定したファイル）を渡したときだけ意味がある。省略時の既定値は開発者の環境のパスなので、「なし」と出ても当てにならない。rclone を使っていなければ、この節は無視してよい。
- 実行後はもう一度 doctor で確認する。

### トラブルシューティング

| 症状 | 主な原因 | 対処 |
|---|---|---|
| `run.bat` でウィンドウが出ない | 依存不足・Python が古い・登録簿の破損 | venv を有効にして `python tool.py` で起動し、エラーを見る |
| 設定を変えたらアプリが起動しない | `[backend].name` / `LLM_BACKEND` の値が不正 | 正しい値（`claude` / `codex` / `pi` / `openai` / `mock`）に直す |
| GUI も全 CLI も `RegistryError` で落ちる | `datasets.local.json` の破損 | ファイルを `datasets.local.json.corrupt` などに退避して手で直す（R2 同期を使っていれば退避後に `config-pull`） |
| 最初の送信が HTTP 401 / 接続拒否 | エンジン未設定、または `llm_backend/config.toml` の構文が壊れている（OpenAI API か `.env` の接続先に送っている） | [エンジンを設定](#3-ai-エンジンを設定する) |
| `[error: Claude Code engine not found…]` | Claude Code が見つからない | 拡張を入れる、`claude` を PATH に通す、または `[claude_code].bin` / `CLAUDE_CODE_BIN` を指定 |
| `[error: … not found in PATH…]`（pi / codex） | CLI 未導入 | `npm i -g …`（バックエンドの状況ウィンドウからも可）。Codex は `codex login` も必要 |
| チャットに `[error: …]` | エンジン側のエラー（認証切れ・レート制限等） | 多くはそのまま再送で回復。認証切れはログインし直す |
| インストールしたのに ✗ のまま | PATH は起動時のものを使う | アプリを再起動 |
| 起動が数秒遅い | R2 同期のネットワーク待ち | `R2_AUTOSYNC=0` |
| 「保存に失敗しました（検証NG）」 | 同期ドライブの不調 | 同期状態を確認 → `doctor` |
| 「データセット … を開けません」（フォルダはある） | `myanalysis.toml` が空か壊れている | `doctor --dataset <ds>` で確認 → 0 バイトなら `--rescue --cache …`、それ以外はクラウドの版履歴で復元（`work_dir` と `format` を既定から変えていなければ、ファイルを削除するだけでもよい） |
| 「セッションを復元できません」 | `session.json` と `.bak` が両方壊れている | **タブを開いて保存する前に**、`doctor` やクラウドの版履歴で復元する（保存すると上書きされる） |
| 「analysis.py が空です」 | 書き込み失敗で 0 バイト | `recover-analysis <解析名> --dataset <ds>` |
| 「概要を保存できませんでした」 | `meta.json` と `.bak` が両方読めない（または書き込み失敗） | 少し待って再試行 → `doctor --dataset <ds>` → 0 バイトなら `--rescue --cache …` か版履歴。戻せなければ `meta.json` と `meta.json.bak` を両方削除する（概要と完了フラグは失われ、他は自動で作り直される） |
| チャットが消えた | チャットのファイルと `.bak` が両方壊れた | `doctor` の 0 バイト一覧 → `--rescue --cache …` か版履歴 |
| 毎回全履歴を送り直している | `data/llm_state/backend_sessions.json` が壊れている | `doctor` で「JSON として読めない」と出たら、GUI を終了してファイルを削除する |
| CLI の出力をファイルやパイプに流すと `UnicodeEncodeError` / 文字化け | Windows の文字コード（cp932） | `python -X utf8 -m …`、または環境変数 `PYTHONUTF8=1` |

エラーダイアログの「詳細はログ」は、`python tool.py`（コンソール付き）で起動したときのコンソール出力を指す。`run.bat` 起動では警告が残らない。ステータスバーの「保存に失敗しました」も 30 秒で消える。

**不具合を報告するときに添えるもの**: `doctor` の出力、`data/logs/gui-crash-*.log`、`python tool.py` で起動したときのコンソール出力、（必要なら）`data/llm_state/command_log.jsonl`。ログにはパスやデータセット名が含まれるので、添える前に確認する。

---

## セキュリティとプライバシー

> [!WARNING]
> **エージェントはあなたのユーザー権限で、確認なしに動く。** Claude は `permission_mode = "bypassPermissions"`、Codex はサンドボックス無し、pi は全ツール許可が既定。「計測ファイルを変更しない」「アプリを書き換えない」はシステムプロンプトでの指示にすぎず、機械的な制限ではない（後者は Claude への指示にだけ含まれる）。

- **権限を絞る**: `llm_backend/config.toml` の `[claude_code].permission_mode` と `allowed_tools`、`[codex].sandbox_mode`、`[pi].tools`。
  - 絞ると GUI 操作や解析が動かなくなる。特に Codex の `workspace-write` / `read-only` では、GUI の操作とデータセットへの保存ができなくなる。
  - `permission_mode` を空にすると `bypassPermissions` に戻るので、`"default"` などを明示する。`models.toml` の同じセクションに同名のキーがあると、そちらが優先される。
  - 確実に隔離したい場合は、専用の OS ユーザーや仮想マシンで動かす。
- **秘密情報**: `.env` とシェルの環境変数（API キー・R2 の鍵・`RELAY_ADMIN_KEY`）はエージェントのプロセスに引き継がれ、エージェントから読める。`PI_API_KEY` はコマンドライン引数で渡るので、pi は `/login` でのログインを推奨。
- **他人のデータセットを開くとコードが実行される**: データセットを開くと、保存されていた解析タブの `analyses/*/analysis.py` が GUI のプロセス内で実行される。前回のセッションの復元、CLI 登録時の自動オープン、エージェントやミーティング参加者によるデータセットのオープンも同じ。エージェントの権限設定では防げない。共有フォルダに書き込める人は、それを開いた人の PC でコードを実行できる。信頼できないデータセットは、開く前に `analyses/` と `<work_dir>/session.json`（既定 `_work/`）を確認する。
- **プロンプトインジェクション**: エージェントはデータセット内のファイル（README や CSV の中身など）を読み、そこに書かれた指示に従ってしまう可能性がある。共有フォルダの `chat_sessions/*.json` も、別の PC で続けるときに会話履歴としてエンジンに送られるので、書き込める人は偽の履歴を仕込める。
- **ミーティング共有**: 参加リンクを持つ人は誰でもエージェントを動かせる（→ [ミーティング共有](#ミーティング共有) の警告）。
- **外部に送られるもの（MyAnalysis 本体の通信）**: 選んだ AI エンジンの提供元（依頼内容と、エージェントが読んだデータ）。設定が無いと api.openai.com へ送ろうとする。Cloudflare（ミーティング共有）。R2（設定した場合のみ。GUI 起動時と CLI の `register-dataset` の後に自動）。npm（インストールボタン）。モックは stooq.com に接続する。**MyAnalysis 本体はテレメトリや更新確認を送らない**（各エンジンのテレメトリはそれぞれの設定による）。
- **エージェント自身の通信**: 上とは別に、エージェントはシェル・Web 検索などのツール・`~/.claude` などに設定済みの MCP サーバーを使って、任意の宛先と通信できる。
- **保存されるもの**: チャットの全文（ツール実行の要約を含む）はデータセットの `<work_dir>/chat_sessions/` に保存・同期される。フォルダを読める人は会話を読める。各エンジン自身も `~/.claude` などに会話を記録する（MyAnalysis でチャットを削除しても消えない）。`data/llm_state/command_log.jsonl` には CLI の引数と結果が残る（ミーティングのトークンを含み得る）。
- **R2**: バケットには全 PC のホスト名とフルパスが入る。バケットは非公開にし、`--include-env` は使わない。R2 の書き込み権を持つ人は、全 PC の `config.toml`・`models.toml`（エンジンの実行ファイルのパスや権限設定を含む）と登録簿を書き換えられる。つまり全 PC で任意のプログラムを実行させられるので、R2 のトークンは自分だけが持つ。
- **ネットワーク待ち受け**: ミーティング共有のリレーだけ。一度共有を始めると、アプリを終了するまで 127.0.0.1 で待ち受ける（LAN リンクを使う間だけ 0.0.0.0）。共有中はトンネル経由で外部から到達できる。

---

## アンインストール

```bash
# 1. アプリで「保存して終了」

# 2. （任意）データセットを元の状態に戻す — 先に myanalysis.toml の work_dir を確認
#    <データセット>/myanalysis.toml, meta.json, meta.json.bak, meta.json.lock
#    <データセット>/analyses/          ← 自分の解析コード。消す前に退避
#    <データセット>/<work_dir>/        ← 既定 _work/（チャット履歴・図・レポート）

# 3. アプリのフォルダを削除（data/, .venv, datasets.local.json, *.toml, .env を含む）
rm -rf /path/to/MyAnalysis

# 4. ホームの作業フォルダを削除（Windows: %USERPROFILE%\.myanalysis）
rm -rf ~/.myanalysis

# 5. （任意）エンジンの会話記録
#    ~/.claude/projects/ の下の agent-home を含むフォルダ
#    ~/.pi/agent/sessions/ の下のアプリのパス（区切りが - になった形）を含むフォルダ
#    ~/.codex/sessions/（日付ごとのフォルダ。各ファイル 1 行目の cwd が codex_home のものが本アプリ分）
#    （config.toml で cwd を変えていればフォルダ名も変わる）

# 6. （任意）エンジンの CLI
npm uninstall -g @anthropic-ai/claude-code @openai/codex @earendil-works/pi-coding-agent
#    VS Code の Claude Code 拡張は VS Code から削除

# 7. （任意）R2 の bundle.json（R2_PREFIX の下。他の PC がまだ同期しているなら消さない）
#    ミーティング共有を使った場合: cloudflared tunnel delete <トンネル名>、DNS レコードの削除、~/.cloudflared
```

---

## Limitations

### 対応プラットフォーム

開発と動作確認は Windows 11 のみ。ランチャーは `run.bat` だけで、macOS / Linux は `python tool.py` で起動する（未検証）。macOS では同期ドライブの自動判定が効かず（常に local 扱い。`MYANALYSIS_FORCE_FRAGILE=1` で強制できる）、Claude の認証状態の表示も誤ることがある。インストーラ・自動更新・依存定義ファイルは無い。

### メモリと規模

解析タブは、開いた時点で解析モジュールの `load()` を実行し、その結果を開いている間メモリに置き続ける。大きなデータを多数のタブ・データセットで同時に開くとメモリを圧迫する。`load_dataset` は `csv_per_subdir` 形式だけに対応し、TIFF は画像全体をメモリに読む。エージェントの構成確認（`dataset_summary`）は 1 階層下までで、サブフォルダ 50・ファイル 50・CSV の見本 20 件まで（50 MiB を超える CSV は行数を数えない）。長いチャットは切り替えのたびに全文を描き直すので遅くなる。

### 複数 PC

PC 間の排他制御は無く、後から保存した方が勝つ。登録簿はホスト名で引くので、PC の名前を変えると登録し直しになる。別の PC で続けるとチャットの最初の送信で全履歴を送り直す。

### コスト

利用料の上限・同時実行数の上限は無い。金額が表示されるのは Claude だけ。ミーティング共有の参加者の依頼もホストに課金される。

### エージェントの機能

解析（Python の実行）ができるのは Claude / Codex / pi だけ。画像ビューアの操作方法は OpenAI 互換エンジンにしか案内されていない。安全上の制限はプロンプトでの指示が中心（→ [セキュリティとプライバシー](#セキュリティとプライバシー)）。

### 表示言語

UI は日本語と英語。ただし一部（画像ビューアのラベル、Qt 標準のボタン、チャットの Stop / Send など）は英語のまま。`doctor` などの一部の CLI メッセージは日本語のみ。

---

## 開発者向け情報

MyAnalysis 本体の開発（構成・テスト・ホットリロード・規約など）は [docs/development_ja.md](docs/development_ja.md) を参照。AI コーディングエージェント向けの詳細な規約は [CLAUDE.md](CLAUDE.md) にある。

---

## ライセンス

MIT License（[LICENSE](LICENSE)）
