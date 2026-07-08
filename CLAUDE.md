# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Experimental measurement data analysis project (Python). Data lives **outside the repo** on a synced drive; only code is versioned here.

## Data access — always go through `config.py`

Raw measurement data is not in the repo. It lives on a synced drive whose mount point varies by PC. `DATASETS` in [config.py](config.py) maps each dataset name to a `{hostname: full_path}` dictionary, resolving the per-PC mount point difference. Host keys are uppercase; lookup normalises via `.upper()`.

**Never hardcode `G:\...` or any absolute data path in analysis code.** Always:

```python
from config import get_dataset_dir
path = get_dataset_dir("dataset_a")
```

When adding work for a new measurement, register the dataset via CLI (`python -m llm_bridge register-dataset <name> <path> [--host H] [--with-analysis [ANALYSIS_NAME]]`) or GUI (File → データセットを新規登録). Both methods rewrite `config.py` in place (ast-based, atomic). CLI 登録は GUI 起動中なら自動でデータセットを開く（`--no-open` でスキップ可）。GUI 登録はアクティブデータセットを更新するがセッション復元はしない（File → データセットを開く… で明示的に復元）。 Manual editing of `DATASETS` in config.py is also supported but inline comments inside `DATASETS` will be lost on the next automated registration. When running on a new PC, add that hostname (uppercase) to each dataset you'll use. Unknown host or dataset raises a descriptive error pointing at config.py.

Dataset directories contain session folders named `session_<yyyymmdd>_<hhmmss>_<id>`.

### Per-dataset settings — `myanalysis.toml`

Settings specific to one dataset live in `myanalysis.toml` at the top of that dataset's directory (not in `config.py`), so they sync with the data and follow it across PCs/repos. [dataset_config.py](dataset_config.py) reads/generates it. Today the only setting is `work_dir` — where analysis output is saved (default `_work`). The tools write this sidecar mechanically; it's safe to hand-edit. Measurement files (CSV etc.) are never modified.

### 解析ファイルはデータセット側に置く（Issue #37）

解析は **`(dataset, name)` ペア** で解決する。あるデータセットの解析一式はそのデータセットディレクトリ配下に自己完結する:

```
<dataset_dir>/
  myanalysis.toml
  analyses/<name>/analysis.py   ← 解析コード本体（git 管理外・同期ドライブで sync）
  analyses/<name>/README.md
  <work_dir>/                    ← 既定 "_work"（myanalysis.toml で変更可）
    analyses/<name>/state/       ← current.json / current_view.png / annotations.json
    analyses/<name>/batch/       ← export 出力 PNG
```

パス解決は [dataset_config.py](dataset_config.py) の `analyses_root` / `analysis_file` / `state_dir` / `batch_dir`（read 経路は `create=False` で副作用なし）。`mod.DATASET`（scaffold が焼く）は `load()` のデータ読込にのみ使い、出力先・セッション紐付けは所在データセット（引数 `dataset`）が真実ソース。メニュー列挙は「現在開いているデータセットのみ」。**別データセットの同名解析を同時に開くのは Issue #51 で対応済み**：タブはデータセットごとの `_DatasetGroup`（トップの `DatasetSwitcher` で切替）にグループ化され、タブ ID は「グループ内で一意」になる。エージェント/CLI は衝突時のみ `dataset=` でアドレッシングを修飾する（`add-tab`/`show`/`set-active-tab`/`close-tab`/tab-tier verb が任意 `dataset=` を受ける）。

リポジトリ直下の旧 `analyses/`（4 件）はコードから参照されなくなり不活性化済み（物理削除はせず、手動移行は範囲外）。`data/analyses/` への書き込みは全廃。

## Git workflow

**`main` ブランチに直接コミットする。feature ブランチを切ってはならない。**
このリポジトリは `dev` ブランチを持たない `main` 直接運用。review tool の automerge も main に対して動作する。`feat/issue-N-xxx` 等のブランチを作ると automerge 後にゴミとして残る。

## Repo conventions

- `data/` is gitignored — safe scratch space for local outputs, caches, exports. Don't commit anything inside.
- Exploratory analysis output (figures, code snippets, intermediates) goes to the dataset's `work_dir` (default `<dataset_dir>/_work`, configurable per dataset via `myanalysis.toml`). Created on first save by `common.explore.save_fig()` / `save_code()`.
- `.env` is gitignored. Used for LLM backend overrides (`OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_API_KEY`, `PI_API_KEY`, `LLM_BACKEND`). Copy `.env.example` to get started.
- Private repo on GitLab (`git@gitlab.com:atakalive/MyAnalysis.git`), so non-secret config like dataset paths is fine to commit.
- Windows `.bat`/`.cmd` files **must use CRLF line endings** — LF-only batch files break `cmd.exe` parsing (especially `if (...)` blocks) and fail to launch. `.gitattributes` pins `*.bat`/`*.cmd` to `eol=crlf`; keep that and don't let an editor save them as LF.

## LLM backends — `llm_backend/`

Chat-dock LLM access goes through the `llm_backend/` package. Backends implement
the `LLMBackend` Protocol in `llm_backend/base.py`; `get_backend()` selects one
by name. Four backends ship: `claude` (VS Code Claude Code engine), `openai`
(OpenAI-compatible HTTP), `mock` (offline smoke test), `pi` (pi-coding-agent
subprocess).

- **Selection order**: env `LLM_BACKEND` → `[backend].name` in
  `llm_backend/config.toml` → `OPENAI_BASE_URL` back-compat.
- **Config file**: copy `llm_backend/config.example.toml` → `llm_backend/config.toml`
  (gitignored). Holds operational/transport settings — e.g. the `[pi]` section
  sets `cwd` (pi working directory), `bin`, `tools`.
- **Model settings**: copy `models.example.toml` → `models.toml` (repo root,
  gitignored — user-edited config lives at the root). One `[<backend>]` section per backend
  with the conventional knobs `model` / `thinking` / `effort` / `provider`; each
  backend maps them to its own CLI/API ([model_settings.py](llm_backend/model_settings.py),
  `merged_settings()`). `models.toml` is canonical and overlays the matching
  `config.toml` section (a legacy `model` left in `config.toml` still works as a
  fallback; for the `openai-compat` backend, `OPENAI_MODEL` env is the fallback). Adding a new
  backend = add a section here + wrap its config with `merged_settings(key, …)`.
  For the claude engine, `effort = "ultracode"` expands to
  `--effort xhigh --settings '{"ultracode": true}'`. Both files load once — restart
  to pick up edits.
- **claude backend** ([claude_code.py](llm_backend/claude_code.py)) reuses the
  **VS Code Claude Code extension's own bundled engine** — the `claude` binary
  at `~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/`
  (auto-detected; override via `[claude_code].bin` or `CLAUDE_CODE_BIN`). It is
  driven exactly as the extension drives it: spawned in bidirectional stream-json
  mode (`--output-format stream-json --input-format stream-json --verbose
  --include-partial-messages`), **not** `claude -p`. Auth/history/settings are
  shared via `~/.claude`, so whoever is logged into VS Code answers here. Turns
  continue via `--resume <session_id>`; the engine's own Bash tool runs
  `python -m llm_bridge` to drive the GUI — no separate skill needed. **cwd is a
  neutral dir outside the repo** (`~/.myanalysis/agent_home`, override via
  `[claude_code].cwd`) so the engine is framed as a *data analyst*, not a
  developer of this repo: CLAUDE.md is auto-discovered by walking cwd upward, so
  any cwd inside the repo loads this developer-oriented file. The repo stays
  reachable via `--add-dir` + `PYTHONPATH` for `common/explore.py` and
  `llm_bridge`. `[claude_code].permission_mode` defaults to `bypassPermissions` so
  GUI-driving tool calls run unattended (stdin is closed after the prompt, so an
  interactive permission prompt would deadlock); tighten with `allowed_tools`.
  **External dependency**: the VS Code Claude Code extension installed + logged in.
- **pi backend** runs `python -m llm_bridge` via the `.pi/skills/myanalysis-bridge`
  skill to drive the GUI live. **External dependency**: Node + pi
  (`npm i -g @mariozechner/pi-coding-agent`). On Windows pi additionally needs a
  bash shell (Git Bash) for its tool execution.
- **Visual feedback** (chat self-view): requires a vision-capable `[pi].model`
  or the pi-vision-proxy extension (`PI_VISION_PROXY_MODEL`).

## llm_bridge — cross-platform

`llm_bridge` (GUI ↔ CLI over the filesystem) works on Windows and POSIX. File
locking is abstracted in `common/filelock.py` (`exclusive_lock`): `fcntl` on
POSIX, `msvcrt` on Windows. `python -m llm_bridge <verb>` runs without PySide6.

## Exploratory analysis — `common/explore.py`

LLM agents analyse data via code execution + CLI, not just GUI remote control.
`common/explore.py` provides a minimal surface: `load_dataset(name, subdir_pattern=..., csv_name=...)`
(config → loaders in one call — 既定パターンは無い。まず `dataset_summary` で実構成を確認してから
実在のパターンを渡す), `save_fig(name, fig, label)` (saves to `<work_dir>/figures/<label>.png`),
`save_code(name, label, content)` (saves to `<work_dir>/code/<label>.py`),
`dataset_summary(name)` (データセット直下の実構成＝subdirs と代表 CSV の columns/rows を歩いて報告する
“まず見る”ステップ). Output goes to the
dataset's `work_dir` (default `<dataset_dir>/_work`, set in `myanalysis.toml`); the
sidecar `myanalysis.toml` and `work_dir` are created on first save. Measurement files
(CSV etc.) are never modified — but a hand-set `work_dir` may place new output files
(PNG/PY) in any subdirectory of the dataset dir. Use `python -m llm_bridge list-datasets`
to discover registered dataset names.

## Session save/restore

データセット単位のセッション（開いていたタブ構成・アクティブタブ）は引き続き `<work_dir>/session.json` に保存・復元する（データバインドの真実ソース＝同期ドライブでどこでも開ける）。現在開いているデータセットは `python -m llm_bridge active` の `active_dataset`/`dataset` フィールド、開いているデータセット一覧は `open_datasets` フィールド（または `list-open-datasets`）で取得できる。

- 保存: File → 「セッションを保存」、「保存して終了」、✕ 終了時の Yes/No/Cancel ダイアログ。
- 復元: File → 「データセットを開く…」、CLI `window open-dataset name=<dataset>`。
- `llm_bridge/session.py` が中核。`show` verb の `dataset=` 引数でタブ→データセット紐付け。
- 暫定運用の `_work/code/restore_view.py` 方式は本機能で置換済み。

### Multiple datasets（Issue #51 — ワークスペース）

1 プロセスに複数データセットを同時に開ける。トップの `DatasetSwitcher` で切り替えると、そのデータセットの解析タブ群とチャットセッション群に入れ替わる。

- **ワークスペースメンバー一覧**（どのデータセットが一緒に開いていたか＋アクティブ）だけを repo-local・gitignored の `data/llm_state/last_window.json`（`{version, datasets, active}`）に集約する。#50 の `recent_datasets.json`（MRU＝履歴順）の隣に並ぶ 2 つ目の PC ローカルレコード（workspace＝同時開き集合）。両者とも **PC 間同期はしない** machine/window 状態で、per-dataset のタブ内容 `session.json` が同期側を担う。Tier 4 の `reload_manifest.json`（transient）とも別レコード。`datasets` は DS タブの表示順（ドラッグ並べ替えを反映し、復元で再現される。Issue #59）。
- 復元は **手動**：File →「前回のセッションを復元」（起動時自動復元はしない）。復元は **ADDITIVE**（既に開いているデータセット/タブは閉じない・上書きしない）。
- 会議共有（meeting relay）は **アクティブデータセットのタブおよびチャットのみ** ゲストへ配信する（DS 切替で共有対象も切替）。同名タブのクロス DS 共有は v1 非サポート。ゲスト HTML はホストの「DS レイヤー → タブレイヤー」をミラーする: DS バーがアクティブ DS を表示し、ホストの DS 切替に追従（チャットは新 DS のセッションへ自動追従、タブ選択・履歴は DS 単位で保持）。**既知の v1 制約**: DS が非表示の間に丸ごと生成されたメッセージは切替復帰後もゲストへ届かない（ストリーミング途中で切替えた場合の最終メッセージだけは配信される）。
- **メモリ天井（既知の制約・v1）**: 各解析タブは開いた時点で `mod.load()` を eager 実行し、開いている限り DataFrame を常駐させる。複数データセットを同時に開くと全データセットの全解析の DataFrame が同時常駐するため、大きな測定データを多数開くとメモリを圧迫し得る（遅延ロード/アンロードは将来課題）。

## ホットリロード — `devtools/`

走行中の GUI に修正コードを注入し、開いていたタブ・プロット・チャット文脈を破壊せず新コードを有効化する。トリガーは**手動のみ**（CLI verb + 開発(&D) メニュー、自動 file-watch なし）。リロード実行時は作業静止が前提（チャット応答中・モーダル表示中は `reload-busy:...` で拒否）。

**エージェントの使い方**: repo コードを編集したら `python -m llm_bridge window reload --wait`。警告（「scope=app 推奨」）が出たら `python -m llm_bridge window reload scope=app --wait` → `command_log.jsonl` を `id==<送信id>` かつ `verb=="reload-result"` でポーリング（30秒）。

4 段階エスカレーション:

| scope | 対象 | 機構 |
|---|---|---|
| `patch`（既定） | sys.modules 内の repo モジュール | superreload 式 in-place パッチ（関数は `__code__` 移植、クラスは `__dict__` 更新）。表示状態は無傷 |
| `tab` | `<dataset_dir>/analyses/<name>/analysis.py` | 単一 sandbox ビルド → 成功後に旧タブ close → 新タブ採用。`target=<tab名>` 必須 |
| `app` | 構造変更（`__init__`/Signal/`__bases__`/watcher closure 等、Tier 1 が警告するもの） | blue-green: 状態 flush → repo モジュール全パージ → 新コードで ToolWindow 再構築 → manifest 復元。失敗時は旧ウィンドウが無傷で残る |
| `restart` | tool.py 自体・PySide6 更新・Tier 3 失敗後 | プロセス再起動 + `--resume-session` 自動復元 |

- 中核は Qt 非依存の [devtools/hotreload.py](devtools/hotreload.py)（superreload, AST 名抽出, purge）と Qt 配線の [devtools/qt_integration.py](devtools/qt_integration.py)（4 tier, manifest, verb, メニュー）。
- 既知の限界: stale 名除去は dict 除去のみで Qt signal 接続・closure 捕捉等の外部参照は旧コードを保持し続ける（→ 警告で scope=app 推奨）。dataclass 既存インスタンスに新フィールドは生えない。`__main__`（= `python tool.py` の tool.py）と `devtools.*` は Tier 1 対象外。

## State

Greenfield as of 2026-05-27 — no build system, dependencies, or package layout yet (no pyproject.toml/requirements, loose-directory layout). Tests live under `tests/` as pytest modules — run `python -m pytest tests/` (GUI tests self-set `QT_QPA_PLATFORM=offscreen`). When introducing a build system / packaging, update this file with the resulting commands.
