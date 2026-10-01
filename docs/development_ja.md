# MyAnalysis 開発ガイド

> 利用者向けの説明は [README_ja.md](../README_ja.md)、AI コーディングエージェント向けの詳細な規約は [CLAUDE.md](../CLAUDE.md) を参照。

## 目次

1. [はじめに](#1-はじめに)
2. [開発環境](#2-開発環境)
3. [アーキテクチャ概要](#3-アーキテクチャ概要)
4. [モジュールマップ](#4-モジュールマップ)
5. [最重要の規律](#5-最重要の規律)
6. [レシピ](#6-レシピ)
7. [ホットリロード](#7-ホットリロード)
8. [テスト](#8-テスト)
9. [開発ツール](#9-開発ツール)
10. [コーディング規約](#10-コーディング規約)
11. [コミットメッセージ](#11-コミットメッセージ)
12. [既知の技術的負債](#12-既知の技術的負債)

---

## 1. はじめに

MyAnalysis 本体（アプリのコード）を変更する開発者・コントリビュータ向けの文書。

| 文書 | 読者 | 内容 |
|---|---|---|
| [README_ja.md](../README_ja.md) と `docs/*_ja.md`（engines / usage / cli / analysis_module / config_sync / meeting_share / troubleshooting） | 利用者 | インストール、使い方、設定、CLI リファレンス、トラブルシューティング |
| 本書 | 本体の開発者 | 構成、守るべき規律、変更の手順、テスト、開発ツール |
| [CLAUDE.md](../CLAUDE.md) | AI コーディングエージェント | 各サブシステムの設計判断と規約。本書より詳しい |

本書は要点と入口だけを書き、詳細は CLAUDE.md と各モジュールの docstring に任せる。文書とコードが食い違うときはコードが正しい（文書を直す）。

---

## 2. 開発環境

- **Python 3.11 以上**（標準ライブラリの `tomllib` を使う）。依存パッケージは [README_ja.md の「インストールと起動」](../README_ja.md#インストールと起動) を参照。実行時の依存は `requirements.txt`、テストは `pip install -r requirements-dev.txt`（pytest と tifffile も入る）。
- `.env` は GUI の起動時に読まれ（R2 同期の前にも毎回読み直す）、既に設定されている環境変数は上書きしない（`common/env.py`）。手で起動した CLI（`python -m llm_bridge engines` 等）は、R2 同期を伴うコマンドを除いて `.env` を読まない。

**起動方法**

| コマンド | 用途 |
|---|---|
| `python tool.py` | 開発時はこれ。コンソールに traceback が出る |
| `python tool.py --demo` | 合成データの `_demo` タブ付きで起動。`SelectorPanel` / `TrajectoryPanel` / `ImagePanel` の 3 種を使う。データセット不要で、タブ verb（`tab _demo set-split ...` 等）を試せる。`_demo` はデータセットに属さないので snapshot / state は保存されない |
| `python tool.py --resume-session` | `data/llm_state/reload_manifest.json` からウィンドウ・タブ・チャットを復元する。ホットリロードの `restart` が内部で使う。manifest は読んだ後に削除され、無ければ通常起動になる |
| `run.bat` | 利用者向けランチャ。リポジトリ直下の `.venv` があれば activate し、`pythonw` でコンソール無しで起動する。起動失敗はダイアログで知らせる（Python の版不足以外は `data/logs/gui-crash-*.log` にも残る） |

**クラッシュログ** — 未捕捉例外（メインスレッド・Python スレッド）は `data/logs/gui-crash-*.log` に traceback が残る。同じ箇所の例外は 1 回だけ、1 プロセス最大 200 件、Ctrl-C / `SystemExit` は記録しない。フックは `tool.py` の冒頭（`__main__` のときだけ）で入る。Python の版不足はダイアログだけ（フックより前に判定して終了するので、ログには残らない）。import 時の失敗と `main()` の中の起動失敗（`app.exec()` より前）は、原因を示すダイアログを出し、`data/logs/gui-crash-*.log` に traceback を残す（書き込みは best-effort）。warning 以上のログは `data/logs/myanalysis.log`（1 MB で回転、3 世代）。

**`PYTHONPYCACHEPREFIX`** — `run.bat` とエージェントの子プロセスは `data/pycache` を設定するが、手元のシェルから起動した python には設定されない（`python -m export` は自分で `sys.pycache_prefix` を `data/pycache` にする）。同期ドライブ上の `.py` を import 機構で読むコードを手で実行するときは、`PYTHONPYCACHEPREFIX=<repo>/data/pycache` を設定しておく（理由は [§5.1](#51-同期ドライブへの書込)）。

---

## 3. アーキテクチャ概要

### 3.1 プロセスとスレッド

```
GUI プロセス（python tool.py）
├─ GUI スレッド: ToolWindow, ChatWidget,
│                QFileSystemWatcher（コマンドキュー / 解析タブごとの state ディレクトリ）
├─ _StreamWorker（QThread）: チャット 1 ターンに 1 本。backend.stream() を回す
│    └─ エージェント子プロセス（claude / codex / pi。1 ターンごとに起動）
│         └─ エージェントが実行する `python -m llm_bridge ...`（= CLI プロセス）
├─ MeetingRelay（会議共有）: _RelayWorker / _TunnelStarter（QThread）
├─ ローカルリレー: ThreadingHTTPServer（daemon スレッド）
└─ cloudflared 子プロセス（会議共有の公開トンネル）

CLI プロセス（python -m llm_bridge ...）: PySide6 なしで動く
```

このほか、疎通確認・バックエンド状況の判定・状況ウィンドウからの npm によるインストール / 更新（`_CmdWorker`）・データセットピッカーの集計がそれぞれ短命の QThread で動く。サブプロセス型バックエンドは子プロセスの stderr を読み捨てる daemon スレッドを、トンネルは cloudflared の出力を読む daemon スレッドを持つ。

### 3.2 GUI と CLI の連携

GUI と CLI はファイルシステムで連携する（Windows / POSIX 両対応。ロックは `common/filelock.py`）。

1. CLI の `window <verb> k=v ...` / `tab <name> <verb> k=v ...` が `data/llm_state/commands/*.json` を書く。
2. GUI の QFileSystemWatcher がそれを拾い、GUI スレッドで `dispatch_command(verb, **args)` を実行し、結果を `data/llm_state/command_log.jsonl` に 1 行追記する。`status` は `ok` / `error` / `rejected`（未知の verb）/ `stale` / `malformed`。
3. `--wait[=秒]`（値を省くと 30 秒）は同じ id の行が現れるまでログをポーリングする。`status` が `error` でも終了コードは 0 で、1 になるのはタイムアウトと引数の誤り（`k=v` の形でない、タブ名が不正等）だけ。
4. GUI の起動時、停止中に積まれていたコマンドは実行せず `stale` として記録・削除する。

例外と補足:

- `window` / `tab` 以外のサブコマンドはこのキューを通らない（例外は `register-dataset` の自動オープン。この PC 向けの登録では、`--no-open` を付けない限り GUI の起動有無に関係なく `open-dataset` を積み、最大 10 秒待つ。GUI が起動していなければ次回起動時に `stale` として捨てられる）。
- `malformed` の行の `id` はファイル名から取った 8 文字のヒントなので、`--wait` はそれに一致せずタイムアウトになる。
- 読み取り系（`active` / `state` / `list-open-datasets` / `list-analyses`）は GUI が書いた `active.json` や `<work_dir>` 下の state ファイルを直接読む。
- 注釈（`annotate` / `clear-annotations`）は CLI が `annotations.json` を直接書き、GUI はディレクトリ監視で反映する。
- openai 互換バックエンドのツール呼び出しも同じキューを通る。`_StreamWorker` のスレッドから `gui/tools.py` の `_via_bridge` がコマンドを積み、GUI スレッドに実行させる（ワーカースレッドからウィジェットに触らない）。

### 3.3 エージェント子プロセス

| エンジン | 起動 | 会話の継続 | 既定の cwd | system プロンプトの渡し方 |
|---|---|---|---|---|
| claude | `claude` を stream-json の双方向モードで起動（`-p` ではない） | `--resume <session_id>` | `~/.myanalysis/agent_home`（リポジトリ外） | `--append-system-prompt`（設定で `--system-prompt`） |
| codex | `codex exec --json`。プロンプトは stdin | `codex exec resume <thread_id>` | `~/.myanalysis/codex_home`（リポジトリ外） | cwd に自動生成する `AGENTS.md` |
| pi | `pi --mode json --no-context-files --skill .pi/skills/myanalysis-bridge` | `--session <id>` | リポジトリ直下（既存セッションの互換のため。AGENTS.md / CLAUDE.md は読まない） | `--append-system-prompt` |
| openai / mock | 子プロセスなし（HTTP / 定型文） | 毎回履歴を送る | — | チャット作成時に保存した system メッセージ（`gui/chat.py` の `_SYSTEM_PROMPT`） |

- 3 つの子プロセスには `PYTHONPATH`（リポジトリ）と `PYTHONPYCACHEPREFIX`（`data/pycache`）が設定され、エージェントは `common.explore` と `python -m llm_bridge` を使える。cwd は `config.toml` の各セクションの `cwd` で変更できる。チャットにデータセットがあれば `MYANALYSIS_CHAT_DATASET` も設定する（無ければ継承値も消す。内部用で `.env` には書かない。Issue #111）。
- claude / codex の cwd をリポジトリ外にしているのは、リポジトリ内だと開発者向けの CLAUDE.md 等を拾って「アプリ開発役」として振る舞うため。
- resume token は PC ローカルの `data/llm_state/backend_sessions.json` にだけ保存し、同期しない（指す先のセッション実体が各 PC の `~/.claude` 等にあるため）。token が無いターンは全履歴を送り直す。

### 3.4 PC ローカルの状態

同期ドライブ側（データセットディレクトリ）に置くものは [usage_ja.md の「保存データと再開」](usage_ja.md#保存データと再開) を参照。PC ローカルに置くのは以下。

| パス | 内容 |
|---|---|
| `data/llm_state/commands/` | CLI → GUI のコマンドキュー |
| `data/llm_state/command_log.jsonl` | コマンドの実行結果（追記。1 MiB 以上になっていたら、次の追記の前に `command_log.jsonl.1` へ回す。保持は 1 世代） |
| `data/llm_state/results/` | 秘密を含む結果の一時置き場。`--wait` が読んだら消す。残った分は次に秘密を書くときか GUI の起動時に、1 時間以上経っていれば消す |
| `data/llm_state/active.json` | アクティブなタブ・データセット・開いているデータセット一覧 |
| `data/llm_state/ui_prefs.json` | 表示言語・ツール表示・全体ペルソナ等の UI 設定 |
| `data/llm_state/recent_datasets.json` | データセットピッカーの最近使った順 |
| `data/llm_state/last_window.json` | 「前回のセッションを復元」で開くデータセット群 |
| `data/llm_state/reload_manifest.json` | ホットリロード `app` / `restart` の一時ファイル（読んだら削除） |
| `data/llm_state/backend_sessions.json` | ネイティブ resume token（90 日で自動削除） |
| `data/llm_state/personas.json`（+ `.bak`） | AI ペルソナの定義 |
| `data/locks/` | 同期ドライブ上のファイルを守るロックの実体 |
| `data/logs/` | クラッシュログ（`gui-crash-*.log`）と warning 以上のログ（`myanalysis.log`。1 MB で回転、3 世代） |
| `data/pycache/` | `PYTHONPYCACHEPREFIX` の退避先 |
| `~/.myanalysis/agent_home/` | claude の cwd |
| `~/.myanalysis/codex_home/` | codex の cwd（`AGENTS.md` を自動生成・上書き） |
| `~/.myanalysis/config_share_state.json` | R2 設定同期の状態（登録時刻 `entry_meta`・登録削除の記録 tombstone を含む。同期前に消すと削除した登録が復活する） |
| `datasets.local.json`（+ `.lock`） | データセット登録簿（リポジトリ直下） |
| `models.toml` / `llm_backend/config.toml` | モデル設定 / バックエンドの運用設定 |
| `.env` | 秘密情報と環境変数（LLM バックエンド、R2 同期、ミーティング共有、同期ドライブの書込戦略） |

`data/` は丸ごと gitignore されている。リポジトリ直下の登録簿・`models.toml`・`llm_backend/config.toml`・`.env` も gitignore 済みで、R2 設定同期（任意機能）を使えば PC 間で同期できる（`.env` は明示したときだけ）。

---

## 4. モジュールマップ

解析モジュールはリポジトリには無い。データセット側の `<dataset_dir>/analyses/<name>/analysis.py` に置かれる（雛形は `newanalysis` が作る）。

### ルート

| パス | 役割 |
|---|---|
| `tool.py` | GUI のエントリポイント。`create_main_window()` はホットリロード `app` からも呼ばれる |
| `run.bat` | Windows 用ランチャ（CRLF 必須） |
| `config.py` | データセット登録簿のアクセス API（`DATASETS` / `get_dataset_dir` / `register_dataset` / `unregister_dataset` / `reload_datasets`） |
| `dataset_registry.py` | 登録簿 `datasets.local.json` の読み書き（ロック付き、型検証、破損は `RegistryError`） |
| `dataset_config.py` | データセットごとの `myanalysis.toml` と、解析・出力パスの解決（`analysis_file` / `state_dir` / `batch_dir` 等） |
| `config_share.py` | 登録簿と `models.toml` / `config.toml`（任意で `.env`）の R2 同期（任意機能。boto3 は遅延 import） |
| `models.example.toml` / `llm_backend/config.example.toml` / `.env.example` | 設定ファイルの雛形。コピーして `models.toml` / `llm_backend/config.toml` / `.env` を作る |
| `.gitattributes` | `.bat` / `.cmd` を CRLF 改行に固定する |
| `.gitlab-ci.yml` | `relay-worker/chatdock.html` を GitLab Pages に配信する。任意の複製で、ゲスト用ページはホストのリレー自身も配信する |

### `common/`（Qt 非依存）

| ファイル | 役割 |
|---|---|
| `paths.py` | 書込の chokepoint（`atomic_write_*` / `durable_*_json`）、`repo_root`、`safe_resolve`、相対パス検証 `validate_relpath` / `resolve_under` |
| `fs_kind.py` | 書込先が rename を信頼できる FS（local）か同期マウント等（fragile）かの判定 |
| `mount_compat.py` | `os.path.realpath` を包む shim。WinFsp 上で PIL / matplotlib が落ちる問題への対策 |
| `filelock.py` | プロセス間の排他ロック（POSIX は fcntl、Windows は msvcrt） |
| `rclone_paths.py` | rclone のキャッシュ・ログの場所の解決（引数 > 環境変数。doctor と mount_probe で共有） |
| `proc.py` | `no_window_kwargs()`（コンソール窓の抑止）、`resolve_cmd_shim()`（npm シムの実体解決） |
| `i18n.py` | 翻訳カタログと `tr()` |
| `explore.py` | エージェント・対話用の解析 API（`load_dataset` / `dataset_summary` / `save_fig` / `save_code` / `save_text`） |
| `loaders.py` | `load_csv_per_subdir` |
| `image_io.py` | 画像ビューア用のローダ（tifffile / Pillow は遅延 import） |
| `crashlog.py` | 未捕捉例外のログ記録 |
| `env.py` | `.env` の読み込み |
| `analysis_module.py` | `analysis.py` を import せず `compile` + `exec` で実行し、実行中だけ `sys.modules` に登録する（GUI と `python -m export` が共有） |
| `slots.py` | ペインの slot パス（`left/top` 等）の文法。Qt 非依存 |
| `chat_dataset.py` | チャットのデータセットを渡す環境変数名 `CHAT_DATASET_ENV` と検査 `chat_dataset_value`（Issue #111） |

### `core/`

| ファイル | 役割 |
|---|---|
| `figures.py` | matplotlib の作図ヘルパと `save()`。import 時に Agg バックエンドを確定する |

### `gui/`（PySide6）

| ファイル | 役割 |
|---|---|
| `__init__.py` | pyqtgraph の既定設定とダークテーマ |
| `window.py` | `ToolWindow`（メインウィンドウ、メニュー、window verb の受け口）、データセットごとのタブ群 `_DatasetGroup`、切替バー `DatasetSwitcher` |
| `tab.py` | `AnalysisTab`（パネル配置、tab verb の登録・実行、スナップショット） |
| `panels.py` | `SelectorPanel` / `TrajectoryPanel` / `ImagePanel` / `FigurePanel` |
| `imageviewer.py` | ImageJ 風の画像ビューア `ImageViewerPanel` とその tab verb |
| `tabbar.py` | 複数行タブバー `MultiRowTabBar` |
| `floating_window.py` | タブを別ウィンドウに切り出す `FloatingTabWindow` |
| `chat.py` | チャットドック `ChatWidget`、`_StreamWorker`、openai / mock 用の `_SYSTEM_PROMPT` |
| `tools.py` | openai 互換バックエンド向けのツール定義 `TOOLS` と実行 `_dispatch` |
| `open_dataset_dialog.py` | データセットピッカー |
| `config_push.py` | `ConfigPusher`（Qt 非依存）。GUI での登録・登録削除の後に `config_share.try_push` をデーモンスレッドで 1 本ずつ実行する（実行中の要求は 1 回にまとめる）。終了時は最大 5 秒待ち、終わらなければそのまま（プロセス終了で消える）。push 本体は `try_push` の push ロックで直列化 |
| `backend_selector_dialog.py` | バックエンド/モデル設定ダイアログ、チャット単位のエンジン上書き |
| `backend_status_window.py` | バックエンドの状況ウィンドウ |
| `persona_dialog.py` | AI ペルソナの設定ダイアログ |
| `qt_translation.py` | Qt 標準ボタン等の翻訳（`qtbase_<lang>.qm`）の読込と言語切替時の張り直し |
| `meeting_share.py` | 会議共有ウィンドウ |

### `llm_backend/`

| ファイル | 役割 |
|---|---|
| `__init__.py` | バックエンド登録簿 `_BACKENDS`、`get_backend()` / `build_backend()`、`config.toml` の読み込み |
| `base.py` | `LLMBackend` Protocol、`Message` 等の型、プロンプトの共通定数（全バックエンドの `NO_LOCAL_PERSISTENCE`、claude / codex / pi の `ANALYST_FRAMING` / `GUI_DISPLAY_VERBS` / `MOUNT_SAFE_EDITS` / `CHAT_DATASET_RULE`）、`compose_system_prompt`、`with_chat_context`、`build_prompt_with_history` |
| `claude_code.py` / `codex.py` / `pi.py` | サブプロセス型バックエンド |
| `openai_compat.py` | OpenAI 互換 HTTP（SSE ストリーミング、ツール呼び出し） |
| `mock.py` | 動作確認用の定型文バックエンド |
| `engines.py` | GUI で選べるエンジンの一覧 `ENGINES` と、選択の適用・チャット単位の設定解決 |
| `model_settings.py` | `models.toml` の読み込みと `merged_settings()` |
| `settings_store.py` | TOML のキーだけをコメントを保ったまま書き換える |
| `preflight.py` | ドライバの導入・認証状況の判定（`engines` CLI と状況ウィンドウが使う） |
| `ping.py` | 疎通確認（1 ターン実行する。課金される唯一の判定） |

### `llm_bridge/`（トップレベルで PySide6 を import しない）

| ファイル | 役割 |
|---|---|
| `__init__.py` | `attach_window()` / `attach_tab()`、組み込みの window verb（`_rewire_window`）、解析タブの構築 `_build_analysis` |
| `__main__.py` | CLI（`python -m llm_bridge`） |
| `commands.py` | コマンドキューの投入・待機・GUI 側の実行と watcher |
| `paths.py` | `data/llm_state` 下のパスと PC ローカル状態の読み書き |
| `session.py` | データセットごとの `session.json` の保存・復元 |
| `verbs.py` | `list-commands` が表示する verb の表（登録済みの verb との一致は `tests/test_verbs_registry.py` が検査） |
| `chat_store.py` | チャット履歴 `chat_sessions/<id>.json`（Qt・`config`・`dataset_config` に依存しない） |
| `state.py` / `snapshots.py` / `annotations.py` | 解析タブの `current.json` / `current_view.png` / `annotations.json` |
| `analysis_edit.py` | `draft-analysis` / `apply-analysis` / `recover-analysis` |
| `dataset_meta.py` | ピッカー用の `meta.json`（概要・完了フラグ・集計値） |
| `personas.py` | AI ペルソナ定義の保存 |
| `guard_write.py` | claude の PreToolUse hook（同期ドライブへの Write/Edit を拒否） |
| `doctor.py` | 同期ドライブ上のデータの健全性チェック |

### その他

| パス | 役割 |
|---|---|
| `devtools/hotreload.py` | ホットリロードの Qt 非依存コア（in-place パッチ、モジュールのパージ） |
| `devtools/qt_integration.py` | ホットリロードの 4 段階、`reload` verb、開発メニュー、manifest |
| `devtools/mount_probe.py` | 同期マウント上の書込戦略を実測する実験 |
| `devtools/migrate_dataset_registry.py` | 旧形式の登録簿を JSON へ移行する CLI |
| `meeting/local_relay.py` | インメモリのローカルリレー。**ワイヤ形式の真実ソース** |
| `meeting/relay.py` | ホスト側のリレークライアント `MeetingRelay` |
| `meeting/tunnel.py` | cloudflared named tunnel の起動・停止 |
| `relay-worker/` | ゲスト用ページ `chatdock.html` とセットアップ手順。ディレクトリ名は旧実装（Cloudflare Worker）の名残 |
| `export/` | `python -m export <dataset> <name>`（ヘッドレス PNG 出力） |
| `newanalysis/` | `python -m newanalysis <name> --dataset <ds>`（解析の雛形生成） |
| `i18n/` | `en.toml` / `ja.toml` |
| `.pi/skills/myanalysis-bridge/` | pi 用のスキル（`SKILL.md`） |
| `tests/` | pytest |

---

## 5. 最重要の規律

### 5.1 同期ドライブへの書込

rclone / WinFsp のような同期マウントでは、**直前に読んだファイルへの `os.replace(tmp, target)`** がマウント層では成功を返しながらキャッシュ層で失敗し、ファイルが 0 バイトに見える。例外は上がらない。そのため:

- **データセットディレクトリへの書込は必ず `common/paths.py` の関数を通す。**

  | 関数 | 使う場面 | 動作 |
  |---|---|---|
  | `atomic_write_text` / `atomic_write_bytes` | 作り直せる内容（state、PNG、コード等） | fragile FS では in-place 書込、local FS では mkstemp + `os.replace`。どちらも書込後に読み戻して検証し、合わせて最大 5 回（待ちは計約 6 秒）試す。検証できなければ `MountWriteError`（`OSError` のサブクラス。送出前にログへ記録し、GUI 実行中は GUI にも通知する）。内容が既に同じなら書かない（mtime も変わらない）。親ディレクトリは作らない |
  | `durable_write_json` / `durable_read_json` | 作り直せない JSON（`session.json`、`meta.json`、`annotations.json`、チャット履歴等） | primary と `.bak` の 2 コピーに単調増加の `_seq` を付けて書き、読むときは両方を読んで新しい方を採る。`_seq` は読取時に取り除かれる（生で読んで比較する側は `strip_seq()` を通す） |

- **生の `open(path, "w")` / `Path.write_text` / `fig.savefig(path)` / `QPixmap.save(path)` をデータセットディレクトリに向けない。** PNG は BytesIO / QBuffer でバイト列にしてから `atomic_write_bytes` に渡す（`core/figures.save` と `llm_bridge/snapshots.py` が実例）。
- **読めないものを既定値で上書きしない。** `read_json_classified` / `durable_read_json` が `unreadable` を返したら書込を中止する。データセットディレクトリの read-modify-write（`llm_bridge/annotations.py`、`llm_bridge/dataset_meta.py`）では 0 バイトのファイルも `unreadable` として扱い（`.bak` が読めればそちらを採る）、書込を中止する。0 バイト化したファイルは `doctor --rescue` で復旧する（rescue は 0 バイトのファイルを手がかりに復元元を探すので、書き直して消さない）。`myanalysis.toml` の 0 バイトも「設定なし」ではなく `ConfigUnreadableError` として扱う。
- 0 バイトのファイルを新しい内容で書き直してよいのは PC ローカルの `data/llm_state` の状態ファイルだけ（守る中身が無いため）。`llm_bridge/paths.py` の `update_ui_pref` / `note_recent_dataset` / `_write_backend_sessions` が `_preserve_unreadable` で判定する。`llm_bridge/personas.py` は primary と `.bak` の両方が 0 バイトのときだけ書き直す。
- ロックは `common/filelock.exclusive_lock` を使う。同期マウント上のパスのロックファイルは自動で `data/locks/` に置かれる（PC 間の排他はもともと成立しない）。
- 同期マウント上の `.py` を import 機構で読むと `__pycache__/*.pyc` が tmp + rename で書かれ、同じ失敗に当たる。GUI と `python -m export` は `analysis.py` を import せず `compile` + `exec` で読む（`common/analysis_module.py`）。手で実行するときの対策は [§2](#2-開発環境)。
- エージェント自身の Write/Edit ツールはこの chokepoint を通らない。claude バックエンドは PreToolUse hook（`python -m llm_bridge guard-write`）で同期マウント上への Write/Edit を拒否する（`analysis.draft.py` だけは許可）。**codex / pi にこの hook は無く**、プロンプト（`MOUNT_SAFE_EDITS`）による誘導だけに頼っている。

実測値と経緯は CLAUDE.md の「同期マウントへの書込規律」節、`common/paths.py` と `common/fs_kind.py` の docstring を参照。

### 5.2 データパス

- 解析コードに絶対パスを書かない。`config.get_dataset_dir(name)` で現在のホストのパスを引く。
- 解析の出力先・state・batch は `dataset_config` の関数で解決する（パスの封じ込め検証を含む）。

### 5.3 エージェントの system プロンプト

- 各バックエンドのプロンプトは `llm_backend/base.py` の `NO_LOCAL_PERSISTENCE`（作業はデータセットの `work_dir` にだけ保存し、ローカルには何も残さない）を含める。コードを実行できる claude / codex / pi は、さらに `ANALYST_FRAMING`（先頭。アプリの開発ではなくデータ解析の担当）、`GUI_DISPLAY_VERBS`（表示系 verb）、`MOUNT_SAFE_EDITS`（既存の `analysis.py` は draft → apply で編集する）、`CHAT_DATASET_RULE`（dataset 省略時はチャットのデータセット）を含める。openai / mock（`gui/chat.py` の `_SYSTEM_PROMPT`）はコードを実行できないので `MOUNT_SAFE_EDITS` を含めない。
- `save_text` に言及する（`work_dir` に任意のファイルを書く正規の経路）。
- `set-description` / `set-completed` には言及しない（利用者が決める値で、エージェントに書かせない。CLI の `--help` からも隠してある）。
- `tests/test_backend_prompts.py` が検査する（検査範囲は [§6.1](#61-llm-バックエンドの追加) の手順 7）。
- 変更が効く範囲はバックエンドで違う。claude / pi は毎ターン system プロンプトを渡し直し、codex は毎ターン `AGENTS.md` を確かめて違えば書き直すので、次のターンから効く。openai / mock の `gui/chat.py` の `_SYSTEM_PROMPT` は、チャットの作成時にセッションの system メッセージとして保存され（`llm_bridge/chat_store.py` の `new_session`）同期されるので、変更は新しく作ったチャットにだけ効く。

### 5.4 resume token

ネイティブ resume token は PC ローカル（`data/llm_state/backend_sessions.json`）にだけ置く。同期される `chat_sessions/<id>.json` に書かない。エンジン ID とバックエンド名の両方が一致したときだけ使い、失敗したターンでは捨てる（ユーザーの Stop は例外で維持する）。詳細は CLAUDE.md の「ネイティブ resume token は同期しない」節。

---

## 6. レシピ

### 6.1 LLM バックエンドの追加

1. **実装** — `llm_backend/<name>.py` に `LLMBackend` Protocol（`llm_backend/base.py`）を満たすクラスを作る。属性 `name` / `model` と、`TextDelta` / `ToolCallRequest` を yield する `stream(messages, tools=None)`。転送エラーは例外で知らせる。
2. **登録** — `llm_backend/__init__.py` の `_BACKENDS` にファクトリを追加する。`settings` が `None` なら `merged_settings("<セクション>", backend_config().get("<セクション>", {}))` で `config.toml` と `models.toml` を重ね、渡されたらそれを使う（疎通確認とチャット単位の上書きが使う）。
3. **エンジン** — `llm_backend/engines.py` の `ENGINES` に `Engine` を追加する（`id`、`label_key`、`backend_key` = `_BACKENDS` のキー、`settings_key` = TOML のセクション名、`config_patch`、`fields`、候補の種）。`ENGINES` に無いバックエンドは `current_engine_id()` が `openai-http` と誤認する。設定ダイアログと状況ウィンドウは `ENGINES` を列挙するので、GUI 側の変更は要らない。
4. **表示名** — `i18n/en.toml` と `i18n/ja.toml` に `backend.engine.<…>` を追加する（`tests/test_engines.py` がラベルキーの解決を検査する）。
5. **導入判定** — `llm_backend/preflight.py` の `_check_engine` に分岐を追加する。無いと「何も要らない」エンジン（mock と同じ）として表示される。判定には認証ファイルを変更しない読み取り専用のコマンドだけを使う（トークンをリフレッシュするコマンドは、同じトークンを共有する他の環境をログアウトさせ得る）。
6. **設定例** — `models.example.toml`（`model` / `thinking` / `effort` / `provider`）と `llm_backend/config.example.toml`（`bin` / `cwd` 等の運用設定）にセクションを追加する。
7. **system プロンプト** — モジュール直下に名前が `_SYSTEM_PROMPT` で始まる定数を置く。`ANALYST_FRAMING` で始め、`GUI_DISPLAY_VERBS`・`NO_LOCAL_PERSISTENCE`・`MOUNT_SAFE_EDITS`・`CHAT_DATASET_RULE` を連結する（いずれも `llm_backend/base.py`。[§5.3](#53-エージェントの-system-プロンプト)）。ペルソナは `set_persona(text)` で受け取り、送信時に `compose_system_prompt(base, persona)` で合成する。
   `tests/test_backend_prompts.py` は `llm_backend/` の `_SYSTEM_PROMPT*` 定数を自動収集し、`save_text` の有無、`GUI_DISPLAY_VERBS` を含むこと、`ANALYST_FRAMING` で始まることを検査する。`NO_LOCAL_PERSISTENCE` / `MOUNT_SAFE_EDITS` を含むことと meta 書込 verb を載せないことはバックエンドごとの明示テストなので、**新バックエンド用のテストを追加する**。
8. **任意の属性**（`gui/chat.py` が `hasattr` / `getattr` で使う）:

   | 属性 | 意味 |
   |---|---|
   | `_session_id` | ネイティブ resume token。セッション ID を運ぶイベントを受けたら代入する（claude は init を含め `session_id` を持つ全イベント、codex は `thread.started`、pi は `session` イベント）。`None` のターンは `build_prompt_with_history(messages, replay=True)` で履歴を丸ごと送る。保存と再充填は GUI が行い、失敗したターンの後は token を捨て、ユーザーの Stop の後は維持する（`gui/chat.py` の `_on_failed`） |
   | `set_persona(text)` | ペルソナ本文の注入（全バックエンドにある） |
   | `set_chat_dataset(ds)` | チャットのデータセットの注入（全バックエンドにある。毎ターン呼ばれる）。CLI 系は `_session_id is None` のターンだけ `with_chat_context` でプロンプトに前置し、`_build_env` で `MYANALYSIS_CHAT_DATASET` を設定・削除する |
   | `cancel()` | Stop ボタンで呼ばれる中断 |
   | `kill()` | `cancel()` 後 2 秒で終わらないときの強制終了。claude はプロセスツリーを止める。codex は `kill = cancel`。pi は `cancel()` が既にプロセスツリーを止めるので持たない |
   | `last_usage` | `input` / `output` / `context` / `context_window`（分かれば `cost` / `total_cost` も）を持つ dict。チャットドックのステータス表示に出る。無い値は 0 か省略でよい |

9. **子プロセスを起動する場合**（既存 3 バックエンドの `_build_env` / `_kill_tree` と同じにする）:
   - Windows の npm シム（`.cmd`）を `cmd.exe /c` で包まない。cmd.exe は引数中の最初の改行で残りを切り捨てる。`common.proc.resolve_cmd_shim()` で実体の argv に解決し、解決できない自作シムだけ従来のラップに戻す。
   - `common.proc.no_window_kwargs()` を渡す（`pythonw` 起動時にコンソール窓を出さない）。
   - 環境変数: `PYTHONPATH` の先頭にリポジトリ、`PYTHONPYCACHEPREFIX` に `common.paths.pycache_prefix()`（setdefault）、`PYTHONUTF8=1`、`PATH` の先頭に `sys.executable` のディレクトリ。
   - cwd はリポジトリの外にする（[§3.3](#33-エージェント子プロセス)）。
   - 子プロセスは無人で動く（権限確認に答える UI が無く、確認待ちになるとターンが止まる）ので、対話的な権限確認が出ない設定で起動する（claude は `bypassPermissions`、codex は既定で承認・サンドボックスを迂回）。
   - エージェント側のツール実行は、`base.py` の `TOOL_CALL_MARKER` / `TOOL_RESULT_MARKER` を使った行として `TextDelta` で流す。UI のツール表示切替と、履歴を送り直すときのツール行の除去がこの形式に依存する。pi は共通マーカーではなく `[<ツール名>...]` の行を出す例外で、これは真似しない。
   - モデルの思考（要約）は `base.py` の `format_thinking_line()` で `THINKING_MARKER`（💭）の 1 行にして `TextDelta` で流す（切り詰めない）。UI は通常・簡略で字下げした引用として表示し、非表示では隠す。`strip_tool_lines()` がこの行も落とすので、履歴の送り直しとチャット検索には入らない。少しずつストリームする場合（claude）は、行を閉じるまで改行を入れず、思考以外を出す前に必ず `"\n"` で閉じる。
   - 止めるときはプロセスツリーごと（Windows は `taskkill /T`、POSIX は `start_new_session=True` + `killpg`）。

### 6.2 window / tab verb の追加

- **window verb** — `llm_bridge/__init__.py` の `_rewire_window()` の中で `window.register_command("<verb>", handler)` する。ここに置くと、ホットリロード `patch` のときに `__on_reload__` が新しいコードで登録し直す（他の場所でクロージャや lambda として登録したハンドラは `patch` では更新されない。モジュール直下の関数を登録した場合は `__code__` の移植で更新される）。`reload` だけは `devtools/qt_integration.install_hotreload()` が登録する。
- **tab verb** — 全解析タブ共通なら `attach_tab()`、解析固有なら `analysis.py` の `build_tab()` 内で `tab.register_command()` する。
  - `attach_tab()` にはデータセットに属さないタブ（`--demo` の `_demo` タブ等）の分岐と通常の分岐があるので、全解析タブ共通の verb は両方に登録する。
  - `show` / `show-image` が作る図・画像ビューアのタブは `attach_tab()` を通らず、`llm_bridge/__init__.py` の各ハンドラが自分で verb を登録する（画像ビューアの verb は `gui/imageviewer.py` の `_register_viewer_verbs`）。
- CLI 側の変更は要らない（`window <verb> k=v` / `tab <name> <verb> k=v` は汎用）。
- **引数** — `k=v` の値は、キーに応じて文字列のまま、または int / float に変換されて、キーワード引数としてハンドラに渡る。
  - `true` / `false` は文字列のままなので、真偽値は `llm_bridge._flag()` で受ける（1/0・true/false・yes/no・on/off）。
  - 名前・パス・自由文のキー（`llm_bridge/__main__.py` の `_STRING_KEYS`）は文字列のまま届く。それ以外は普通の 10 進数（`12`・`-3`・`0.5`・`1e3`）のときだけ int / float になり、`1_000`・`nan`・`inf`・全角数字は文字列のまま届く。
  - tab verb の `dataset=` は宛先タブの特定に使われて取り除かれるので、tab verb は `dataset` という名前の引数を受け取れない。
  - 未知のキーは `TypeError` になり、`status: "error"` で記録される。
- **実行** — ハンドラは GUI スレッドで動く。例外は `status: "error"`、`error: "<型>: <メッセージ>"` と、`traceback`（末尾 10 フレーム。`raise … from` の連鎖も含む）として記録される。
- **戻り値** — JSON にできる値はそのまま `result` に入り、それ以外は `repr` される。既存の操作系 verb は `added:<name>` / `closed:<ds>:<n>` のような短い文字列を、一覧系は list / dict を返す。
- **周知** — エージェントは verb を system プロンプト等から知る。必要に応じて更新する: 表示系（図・画像ビューア・ペイン）の verb は `llm_backend/base.py` の `GUI_DISPLAY_VERBS`（claude / codex / pi 共通。画像ビューアの verb は `tests/test_backend_prompts.py` が `llm_bridge.verbs.IMAGE_VIEWER_VERBS` と突き合わせる）、それ以外は `llm_backend/claude_code.py` の `_SYSTEM_PROMPT`、`llm_backend/pi.py` の `_SYSTEM_PROMPT_PI` と `.pi/skills/myanalysis-bridge/SKILL.md`、`llm_backend/codex.py` の `_SYSTEM_PROMPT_CODEX`、openai 系は `gui/tools.py`（[§6.4](#64-openai-互換バックエンド向けの-gui-ツール)）。利用者向けの一覧は [cli_ja.md](cli_ja.md)。
- `python -m llm_bridge list-commands [タブ名]` は `llm_bridge/verbs.py` の表を表示する。verb を追加・削除したら表も直す（`tests/test_verbs_registry.py` が不一致を検出する）。解析ごとの独自 verb は表示されない。

### 6.3 UI 文言（i18n）の追加

- UI 文言は `common.i18n.tr("<key>", **params)` で出す。カタログは `i18n/en.toml`（既定言語・フォールバックの基底）と `i18n/ja.toml` の平坦なキー。`tr()` は「選択中の言語 → en → キー文字列」の順にフォールバックし、例外を出さない。
- **両カタログに同じキー・同じプレースホルダ（`{name}` 等）を入れる。** `tests/test_i18n_catalog.py` が全カタログのキー集合とプレースホルダの一致を検査する。
- 同テストはさらに、`tr()` を呼ぶファイル（リポジトリ直下の `*.py` と `_SCAN_DIRS` の各パッケージから自動検出した `IN_FILES`）について、使ったキーがカタログにあること、キーの形をした文字列リテラル（`chat.turn.stopped` 等）がカタログにあること、`tr()` の外に日本語の文字列リテラルが無いこと（docstring 等の式文は除く。CLI の `llm_bridge/__main__.py` は対象外、許可は `CJK_LITERAL_ALLOW`）を検査する。`tr()` の第 1 引数は文字列リテラルにする。変数を渡す呼び出しはファイルごとの件数を `DYNAMIC_TR_CALLS` で固定しており、増やすときはキーの出どころを別のテストで検査したうえで件数を更新する（例: `llm_backend/engines.py` の `engine_label` は `tests/test_engines.py` がラベルキーの解決を検査する）。`tr()` を呼ばないファイルは検査されないので自分で守る。パッケージを追加したら `_SCAN_DIRS` にも足す（`test_scan_dirs_cover_all_packages` が検出する）。
- **言語のライブ切替** — `ToolWindow.retranslate()` が自分のメニュー等を張り替える。ToolWindow の外で作る UI は `retranslate()` を用意し、`window.register_retranslate_hook(fn)` で登録する（開発メニュー、バックエンド状況ウィンドウ、会議共有ウィンドウが例）。
- 言語の追加は `i18n/<code>.toml` を置くだけでよく、次の起動から言語メニューに出る。`_lang.name` にその言語での言語名を入れ、キー集合を en と一致させる。

### 6.4 openai 互換バックエンド向けの GUI ツール

- claude / codex / pi は `tools` 引数を使わず、エージェント自身が `python -m llm_bridge` を実行する。`gui/tools.py` のツールを使うのは openai 互換バックエンドだけ。
- 追加するのは 2 か所: `TOOLS`（OpenAI function calling の JSON スキーマ）と `_dispatch()` の分岐。`tests/test_registry_consistency.py` が、`TOOLS` の全ツールが `_dispatch()` で処理される（未知ツール扱いにならない）ことを検査する。
- `_dispatch()` は `_StreamWorker` のスレッドで動く。ウィジェットに触る操作は `_via_bridge(tier, target, verb, args)` でコマンドキューを通す（既定 10 秒でタイムアウト、Stop で中断できる）。
- `make_dispatch()` の `dispatch(name, args, cancelled=None, chat_dataset=None)` には `ChatWidget` がチャットのデータセットを渡す（`functools.partial`）。`_via_bridge` はそれを `commands.submit(caller_dataset=…)` に載せるので、`dataset` を省略した verb はチャットのデータセットが既定になる。`get_state` のように `_via_bridge` を通らないツールは `_CHAT_DATASET.get()` を既定にする（Issue #111）。
- 戻り値は JSON 文字列。例外は `make_dispatch()` が `{"error": ...}` に変換する。1 回の送信でのツール往復は最大 8 回（`gui/chat.py` の `_MAX_TOOL_TURNS`）。
- ツールの使い方の指示が要るなら `gui/chat.py` の `_SYSTEM_PROMPT` も更新する。これはチャット作成時にセッションへ保存されるので、既存のチャットには効かない（[§5.3](#53-エージェントの-system-プロンプト)）。

### 6.5 会議共有のワイヤプロトコルの変更

次をまとめて変更する。

| ファイル | 役割 |
|---|---|
| `meeting/local_relay.py` | ワイヤ形式の真実ソース（ルート、レスポンスの形、エラー本文）。ルーティング本体 `RelayState.handle()` はソケットに依存しない |
| `meeting/relay.py` | ホスト側クライアント（`MeetingRelay` / `_RelayWorker`） |
| `relay-worker/chatdock.html` | ゲスト用ページ。ローカルリレーが `GET /` で配信する。GitLab Pages への配信は任意の複製で、`main` への push ですぐ更新されるため、古いホストとの互換に注意する |
| `tests/test_local_relay.py` / `tests/test_meeting_relay.py` | 偽のペイロード・レスポンスを持つので、形の変更に合わせて直す（`tests/test_meeting_chat.py` はワイヤ形式を持たず、`ChatWidget` 側のフックを検査する） |

- `relay.py` のスレッド規約: ウィジェットには GUI スレッドからだけ触る。urllib の呼び出しには必ず `timeout` を付ける。ワーカーは `requestInterruption()` + `wait()` で止め、強制終了しない。
- `chatdock.html` を `#selftest` を付けて開くと、Markdown レンダラの自己テストがブラウザで走る。
- ホスト側のセットアップ手順は `relay-worker/README.md`。

---

## 7. ホットリロード

走行中の GUI に修正したコードを反映する。トリガーは手動だけ（ファイル監視による自動リロードは無い）。

```bash
python -m llm_bridge window reload [scope=patch|tab|app|restart] [target=<タブ名>] --wait
```

| scope | 対象 | 仕組み | 結果の返り方 |
|---|---|---|---|
| `patch`（既定） | `sys.modules` にあるリポジトリのモジュール（`__main__`・`devtools.*` と、リポジトリの下にあってもサードパーティのコード（`site-packages` / `dist-packages` の下＝リポジトリ直下の `.venv` など、および `sys.prefix` 等がリポジトリの内側を指す Python 本体）を除く。判定は `devtools/hotreload.py` の `is_project_module`） | 変更されたファイルを構文チェックし、1 つでも構文エラーがあれば何もしない。問題なければ in-place でパッチする（関数は `__code__` を移植、クラスは属性を移植）。表示状態は保たれる。開いている解析の `analysis.py` の変更は「scope=tab を使え」と報告するだけ | すぐ返る（要約文字列） |
| `tab` | 1 つの解析タブの `analysis.py`。`target=<タブ名>` が必須。開いているデータセット全体でその名前のタブが 1 つだけなら、どのデータセットにあってもそのタブを対象にする。複数のデータセットにあればアクティブなデータセットのタブを対象にし、アクティブなデータセットに無ければエラーになる。`dataset=<ds>` を付けるとそのデータセットのタブだけを対象にする（チャットのエージェントが省略したときはチャットのデータセット）。図・画像ビューアのタブは拒否する | sandbox で新しいタブを組み立て、成功したら旧タブと差し替える。失敗したら旧タブが残る | すぐ返る |
| `app` | 構造の変更（`__init__`、Signal、`__bases__`、長寿命のクロージャ等） | 構文チェック → 開いているデータセットのセッション保存 → manifest → リポジトリのモジュールを全パージ → 新しいコードで ToolWindow を作り直す。失敗したら旧ウィンドウに戻る | まず `reload-scheduled` が返り、後から同じ id で `"verb": "reload-result"`（`status` は ok / failed）の行が `command_log.jsonl` に追記される |
| `restart` | `tool.py` 自体、PySide6 の更新、`app` の失敗後 | セッション保存 → manifest → `tool.py --resume-session` を起動して自分は終了 | まず `reload-scheduled` が返る。`reload-result` が記録されるのは失敗したとき（保存失敗、または実行の直前にチャットの応答中・モーダルダイアログの表示中になっていたとき）だけ。成功は、新しいウィンドウが出てから `python -m llm_bridge window list-tabs --wait` が `status: ok` を返すことで確かめる（新しい GUI の watcher が動き出す前に積まれたコマンドは `stale` として記録され、実行されない） |

- `--wait` が待つのは最初の結果（`app` / `restart` では `reload-scheduled`）まで。
- モーダルダイアログの表示中は `reload-busy:...` を返して何もしない。`patch` / `app` / `restart` はチャットの応答中も同じ（`tab` はエージェントが自分のターン内で使うので、応答中でも実行する）。`app` / `restart` はセッション保存に失敗したデータセットがあれば中止する。
- `restart` は構文チェックをしない（`app` はする）。`tool.py` が起動時に import するモジュールに構文エラーがあると、新しいプロセスは import の時点で落ちる（起動失敗のダイアログが出て、`gui-crash-*.log` に traceback が残る）が、旧プロセスは既に終了している。変更したモジュールは先に `patch` か `app` で確かめる。
- **開発(&D) メニュー**: 「コード再読み込み」= `patch`（パッチできない構造変更を検出すると再構築を勧めるダイアログが出る）、「アプリ再構築」= `app`、「再起動して復元」= `restart`。`tab` に当たる項目は無い。

**モジュール側のフック**

| 仕組み | 用途 |
|---|---|
| `__on_reload__(ctx)` | `patch` でモジュールを差し替えた後に呼ばれる（`ctx.window` でウィンドウを得る）。登録済みのクロージャを作り直すのに使う（`llm_bridge/__init__.py` は `_rewire_window` を再実行している） |
| `__hot_preserve__ = ["名前", ...]` | `patch` でモジュールを再実行しても旧い値を残す変数（`llm_bridge/session.py` の `_touched`） |
| `_m("<module>")` | フックではなく `devtools/qt_integration.py` の約束事。devtools は `app` でパージされないので、リポジトリのモジュールを import 時の参照で持たず、呼ぶたびに `sys.modules` から引く |

**既知の限界**

- 削除された名前や形の変わったクロージャは、Qt のシグナル接続など外部に保持された参照から旧いコードのまま呼ばれ続ける。長寿命の呼び出しが既に作ったネストしたクロージャ（コマンドキューのスロット等）も同じ。これらは警告として報告され、`app` を勧められる。
- dataclass のフィールド変更は、既存のインスタンスには反映されない。
- in-place で更新されるのは関数とクラスだけ。値として取り込んだ定数（`from X import CONST`）は、変更していないモジュール側では旧い値のままになる。
- `__main__`（`python tool.py` で起動したときの `tool.py`）と `devtools.*` は `patch` の対象外。`restart` を使う。

---

## 8. テスト

```bash
python -m pytest tests/
```

- リポジトリ直下から `python -m pytest tests/` で実行する。`pytest.ini` / `pyproject.toml` / `tests/__init__.py` は無く、conftest がトップレベルのモジュール（`dataset_registry` 等）を import するので、`pytest` コマンドを直接使うと conftest の読込で失敗する。
- pytest-qt は使っていない。GUI テストの多くは各ファイルの `qapp` fixture で、残りはテスト内で直接 `QApplication` を作る。どちらも `QT_QPA_PLATFORM=offscreen` を自分で設定する（ヘッドレスで動く）。
- テストの依存は `requirements-dev.txt` で揃う。`requirements.txt` の必須依存（PySide6・pyqtgraph・numpy・pandas・matplotlib・Pillow）はテストでも skip しないので、入っていないとそのテストは失敗する。skip するのは任意依存の tifffile を使うテストだけ（`test_figures.py`（matplotlib）、`test_mount_compat.py`（Pillow）、`test_backend_prompts.py`（PySide6）には以前からの skip が残っている）。
- **CI ではテストが走らない**（`.gitlab-ci.yml` は `chatdock.html` を GitLab Pages に配信するだけ）。push の前に手元で実行する。

**`tests/conftest.py` が隔離するもの**

| 対象 | 方法 |
|---|---|
| 登録簿 `datasets.local.json` | conftest の読込時とテストごとに一時パスへ差し替え、`config.DATASETS` を空にする |
| `data/llm_state` の `last_window` / `recent_datasets` / `backend_sessions` / `ui_prefs` / `personas` | `llm_bridge.paths` の各 `*_path` を `tmp_path` に向ける |
| FS 判定 | `MYANALYSIS_FS_OVERRIDE=<tmp_path>=local` を設定し、`MYANALYSIS_FORCE_FRAGILE` / `MYANALYSIS_WRITE_STRATEGY` を外す |
| R2 設定同期 | `R2_AUTOSYNC=0`。`.env` の読込・状態ファイル・boto3 クライアントをスタブにする（ネットワーク禁止） |
| session の持ち主・チャット baseline の記録 | `llm_bridge.session` の `_restored` / `_unreadable` / `_chat_baseline` / `_last_skipped` をテストごとに空の dict に差し替える |
| チャットのデータセットの環境変数 | `MYANALYSIS_CHAT_DATASET` を外す（チャットのエージェント内で pytest を実行しても継承しない） |

**隔離されないもの** — `data/llm_state` の `commands/`、`command_log.jsonl`、`active.json`、`reload_manifest.json`。これらに触れるテストは、`llm_bridge.paths.global_state_dir`（または個別の `*_path` 関数）を自分で `tmp_path` にパッチする。`from ... import` で取り込まれた名前は、取り込んだ側のモジュールでパッチする。

**注意点**

- `tests/test_local_relay.py` などは `0.0.0.0` に bind する。Windows では初回にファイアウォールの許可ダイアログが出ることがある。
- Qt と実際の QThread を使うテストは、fixture の teardown でワーカーの終了を待ち、widget を `deleteLater()` してイベントを流し切る。GC に任せると後続のテストで Qt が abort する。`tests/test_meeting_chat.py` の `widget` fixture が手本。

**サブシステムとテストの対応**

| サブシステム | テスト |
|---|---|
| 書込 chokepoint・FS 判定・ロック | `test_paths`, `test_fs_kind`, `test_filelock`, `test_mount_compat`, `test_figures`, `test_local_state_store` |
| 登録簿・データセット設定・設定同期 | `test_dataset_registry`, `test_register_dataset`, `test_migrate_dataset_registry`, `test_dataset_config`, `test_config_share`, `test_cli_register_auto_open`, `test_config_push` |
| explore / export / newanalysis | `test_explore`, `test_export_driver`, `test_newanalysis`, `test_analysis_module` |
| llm_bridge の CLI と状態ファイル | `test_active_state`, `test_cli_state_dataset`, `test_cli_set_description`, `test_cli_set_completed`, `test_annotations`, `test_annotations_handler`, `test_analysis_edit`, `test_guard_write`, `test_recent_datasets`, `test_dataset_meta`, `test_parse_kvs`, `test_command_log`, `test_cli_stdio`, `test_verbs_registry` |
| セッションとチャット履歴の保存 | `test_session`, `test_session_meta_hooks`, `test_chat_store`, `test_backend_session_store` |
| window / tab verb と GUI | `test_show`, `test_show_image`, `test_attach_tab_dataset`, `test_current_dataset_gui`, `test_workspace_gui`, `test_floating_tab`, `test_tabbar`, `test_tab_context_menu`, `test_dataset_context_menu`, `test_open_dataset_dialog`, `test_imageviewer`, `test_image_io`, `test_nested_split`, `test_slots` |
| チャット UI・ツール呼び出し | `test_chat_widget`, `test_chat_persona`, `test_llm_tool_use` |
| LLM バックエンド | `test_backend_registry`, `test_backend_prompt`, `test_backend_prompts`, `test_claude_code`, `test_claude_system_prompt`, `test_pi_backend`, `test_codex_backend`, `test_cmd_shim`, `test_proc`, `test_persona_prompt`, `test_registry_consistency` |
| エンジン設定・状況・ペルソナ | `test_engines`, `test_model_settings`, `test_settings_store`, `test_backend_selector`, `test_backend_status_window`, `test_preflight`, `test_ping`, `test_personas`, `test_persona_dialog` |
| i18n | `test_i18n`, `test_i18n_catalog`, `test_i18n_gui`, `test_qt_translation` |
| ホットリロード | `test_hotreload`（Qt 非依存コア）, `test_hotreload_qt` |
| 会議共有 | `test_local_relay`, `test_meeting_relay`, `test_meeting_chat` |
| クラッシュログ・起動 | `test_crashlog`, `test_tool_startup` |
| doctor・mount_probe | `test_doctor`, `test_mount_probe` |

---

## 9. 開発ツール

### 9.1 `python -m llm_bridge engines`

全エンジンの前提（node + npm）・導入・認証の状況を一覧する。GUI の「バックエンドの状況」と同じ判定（`preflight.check_all()`）で、LLM は呼ばないので課金されない。

### 9.2 `python -m llm_bridge doctor`

```bash
python -m llm_bridge doctor [--dataset <ds>] [--repair] [--rescue] [--cache <rclone の VFS キャッシュ>] [--log <rclone のログ>]
```

0 バイトのファイル、primary と `.bak` の食い違い、`myanalysis.toml` の破損、`data/llm_state` の 0 バイトファイル、rclone キャッシュの孤児 tmp とログの失敗イベントを報告する。未解決の問題があれば終了コード 1（孤児 tmp とログの失敗イベントは数えない。指定したキャッシュ・ログが見つからないこと、フォルダが見えないデータセットは数える）。`--dataset` が解決できないとき、`--rescue` にキャッシュの指定が無いときは終了コード 2。空の `__init__.py` / `py.typed` / `.gitkeep` / `*.lock` は 0 バイトとして報告しない。`--repair` は newest-wins で収束させ、`data/llm_state` の 0 バイトファイルは削除する（次回の書込で作り直される）。`--rescue` は 0 バイトのファイルを rclone キャッシュの孤児 tmp から復元する。

`--cache` / `--log` に既定値は無い。省略時は環境変数 `MYANALYSIS_RCLONE_CACHE` / `MYANALYSIS_RCLONE_LOG`（`common/rclone_paths.py`）を読み、それも無ければ、指定の無い方の点検をスキップする。利用者向けの説明は [troubleshooting_ja.md の「doctor（点検と修復）」](troubleshooting_ja.md#doctor点検と修復)。

### 9.3 `python -m devtools.mount_probe`

```bash
python -m devtools.mount_probe --dir <マウント上の既存のディレクトリ> --log <rclone のログ> [--cache <rclone の VFS キャッシュ>] [--n 50] [--pace 12] [--keep]
python -m devtools.mount_probe --describe-only   # FS 判定だけ表示
```

同期マウント上で 4 条件（`os.replace` / in-place × 事前に読む・読まない）の書込を繰り返し、rclone のログから失敗を数える。書込戦略を変えるときの検証に使う。

- `--dir` と `--log` は必須（`--log` は OS の環境変数 `MYANALYSIS_RCLONE_LOG` でもよい。`.env` からは読まない）。`--cache`（または `MYANALYSIS_RCLONE_CACHE`）を省くと孤児 tmp を数えない。
- `--dir` には既存のディレクトリを渡す。実行ごとにその下へ `mount_probe-XXXX` を作ってそこに書き、終了時（中断・例外でも）にこの実行が書いた `.bin` とそのディレクトリだけを消す（`--keep` で残す）。`--dir` 自体と、その中の他のファイルには触れない。
- FS 判定は `--dir` と、Windows では全ドライブについて表示する（Python 3.12 未満ではドライブ一覧を省く）。
- 既定（`--n 50`、`--pace 12` 秒）では 10 分以上かかる。
- `--size`（1 ファイルのバイト数。既定 4096）と `--settle`（計測後に待つ秒数。既定 12）も指定できる。

### 9.4 開発で使う環境変数

| 変数 | 効果 |
|---|---|
| `MYANALYSIS_WRITE_STRATEGY` | `replace` / `inplace` で書込戦略を強制する（FS 判定より優先するキルスイッチ） |
| `MYANALYSIS_FS_OVERRIDE` | `M:=fragile,D:=local` のようにパスの接頭辞ごとに FS 種別を上書きする（最長一致） |
| `MYANALYSIS_FORCE_FRAGILE` | `1` で全パスを fragile として扱う |
| `MYANALYSIS_RCLONE_CACHE` / `MYANALYSIS_RCLONE_LOG` | `doctor` と `devtools.mount_probe` で `--cache` / `--log` を省略したときの rclone の VFS キャッシュ / ログの場所（OS の環境変数。`.env` からは読まない） |
| `LLM_BACKEND` | バックエンドの全体既定の選択を上書きする（`config.toml` より優先。チャット単位のエンジン上書きには効かない） |
| `CLAUDE_CODE_BIN` / `CODEX_BIN` | claude / codex の実行ファイル（`config.toml` の `bin` が空のときに使われる） |
| `R2_AUTOSYNC` | `0` で R2 の自動同期（起動時と CLI の `register-dataset` の後の双方向同期、GUI での登録・登録削除の後の送信）を止める。手動の `config-*` は残る |

### 9.5 旧形式の登録簿からの移行

以前のバージョンでは、データセットの登録簿は `config.py` の中の `DATASETS` リテラルだった。現在は Git 管理外の `datasets.local.json` にある。旧バージョンからは次の手順で移行する。

1. 旧 GUI を終了し、登録や同期を実行中の CLI が無い状態にする。
2. **コードを更新する前に**、旧 `config.py` を Git 管理外の場所（例 `data/legacy_registry/config.py.legacy`）へコピーしておく。既存の退避ファイルは上書きしない。
3. コードを更新したら、新しいアプリを起動する**前に**変換する（起動時の同期や GUI からの登録で登録簿が先に作られると、変換が「内容の異なる登録簿がある」で止まるため）。

   ```bash
   python -m devtools.migrate_dataset_registry --source <退避した config.py> [--output <registry.json>] [--dry-run]
   ```

   - 旧ファイルは import / exec / eval せず、`ast.parse` と `ast.literal_eval` でモジュール直下の `DATASETS` だけを読む。`config`・同期機能・GUI を import せず、ネットワークにも接続しない。
   - `--output` を省くとリポジトリ直下の `datasets.local.json` に書く。`--output` は `.json` で、親ディレクトリが存在している必要がある。
   - 既存の登録簿は上書きしない（強制オプションは無い）。
   - 終了コード 0: 移行した / 既に同じ内容（no-op）/ `--dry-run` の検証が通った。
   - 終了コード 1: 既存の登録簿と内容が異なる、既存の登録簿が壊れていて読めない、旧ファイルが読めない・`DATASETS` が無い・複数ある・リテラルでない・型が不正、`--output` が `.json` でない、出力先ディレクトリが無い、書込に失敗した。手で原因を解消する。
   - 終了コード 2: 使い方の誤り（`--source` が無い等）。
   - 一致の確認は、同じコマンドをもう一度実行して `already up to date`（終了コード 0）になることで行う。
4. R2 同期を使っていて、同期データから登録を取り込みたい場合は、正常なローカル登録簿がある（または未作成の）状態で `python -m llm_bridge config-pull` を実行する。
5. 新しいコードで起動し、登録一覧（`python -m llm_bridge list-datasets`）と現在のホストでのパス解決を確認する。ネットワークを使わなくても移行は完了できる。

---

## 10. コーディング規約

**依存の向きと Qt 非依存コア**

- `llm_bridge/` パッケージは CLI からも import されるので、トップレベルで PySide6 を import しない（必要な関数の中で遅延 import する）。
- `llm_bridge/chat_store.py` は Qt にも `config` / `dataset_config` にも依存させない（依存するのは `llm_backend.base` と `common.paths` だけ）。`dataset_registry.py` は `config`・GUI・同期モジュールに依存しない。
- `common/paths.py` はトップレベルで `common.i18n` を import しない（i18n → paths の一方向）。`gui/window.py` は `devtools` を import しない（開発メニューは retranslate hook で連携する）。
- ロジックは Qt 非依存のコアに置き、Qt の配線と分けてヘッドレスでテストする。例: `config_share.py`・`gui/config_push.py`、`llm_bridge/session.py`・`chat_store.py`・`dataset_meta.py`、`llm_backend/engines.py`・`preflight.py`・`settings_store.py`・`ping.py`、`devtools/hotreload.py`、`meeting/local_relay.py` の `RelayState`。

**例外を出さない（never-raise）**

- GUI スレッドや起動経路から呼ばれる補助処理は、失敗しても例外で止めない。`common.i18n`、`llm_backend.preflight`、`llm_bridge.paths` の UI 設定・MRU・resume token、`llm_bridge.personas`、`common.crashlog` がこの方針。`guard-write` は内部エラー時に必ず許可する（fail-open）。
- `preflight` は i18n を知らない。`*_detail` にはバージョンやプロバイダ名などの中立な事実だけを入れ、説明文は `notes` に `(i18n キー, params)` で入れて呼び出し側（CLI / GUI）が訳す。状態の `unknown`（判定できない）と `missing`（無い）を混同しない。

**Windows**

- `.bat` / `.cmd` は CRLF 改行（`.gitattributes` で固定）。LF だと `cmd.exe` が壊れる。
- それ以外のファイルはすべて LF 改行（`.gitattributes` では強制していない）。Windows で Python からファイルを書き直すときは `newline="\n"` を指定する（既定では CRLF になる）。
- 子プロセスには `no_window_kwargs()` を渡す。npm シムの扱いは [§6.1](#61-llm-バックエンドの追加) の手順 9。インストール・ログイン等の 1 行コマンドは `preflight._wrap` が PATH 解決と `cmd.exe /c` ラップを行う。

**コミットしないもの**（`.gitignore` 済み）

- `data/`、`datasets.local.json`（とその派生ファイル）、`.env`、`llm_backend/config.toml`、`models.toml`、`.pi/` の skills 以外。
- 実際の登録データ（データセット名・ホスト名・パス）をコードやテストに書かない。

**その他**

- コメントと docstring は日本語と英語が混在している。最近のコードは日本語が多い。
- リンタの設定ファイルはコミットされていない（コード中の `# noqa` はリンタ利用の名残）。
- 新しいドキュメントは `docs/` に置く。

---

## 11. コミットメッセージ

```
<type>: <日本語の件名>

<本文: 何を・なぜ変えたかの要点>
```

- `type` は `feat` / `fix` / `docs` / `test` / `refactor` / `chore` / `style` を使う。Issue に対応する変更では `feat(#<番号>):` のようにスコープへ Issue 番号を入れた形も使われている。
- 件名は日本語で、変更の結果が分かるように書く（例: `fix: …が…していた`、`feat: …できるようにする`）。
- 本文には変更の要点と理由を書く。

---

## 12. 既知の技術的負債

| 項目 | 内容 |
|---|---|
| 使われていない API | `AnalysisTab.connect_state()` / `current_state()` は互換のため残している（`llm_bridge` は読まない。リポジトリ外の解析コードから呼ばれている可能性があるため削除しない） |
| 書込ガードが claude だけ | PreToolUse hook による同期ドライブへの Write/Edit の拒否は claude バックエンドにしか無い。codex / pi はプロンプトの指示だけ |
| `devtools/hotreload.py` の import | トップレベルで `dataset_config` と `common.paths.repo_root` を import しており、`_m()` の約束事（[§7](#7-ホットリロード)）に反する。`app` の後もパージ前のモジュールを参照し続ける |
| `data/llm_state` の一部が chokepoint 外 | `active.json`、コマンドキュー、`reload_manifest.json`、`command_log.jsonl`、`last_window.json`（`llm_bridge/session.py` の `write_last_window`。固定の tmp 名）は `common/paths.py` を通さず直接書いている（ローカルディスク前提） |
| mock が完全にはオフラインでない | `mock` バックエンドは応答の一部を外部サイトから取得する（失敗しても 3 秒でタイムアウトして続行する） |
| CI・リンタ設定が無い | CI でのテスト実行とリンタ設定が無い（依存定義は `requirements.txt` / `requirements-dev.txt`） |
