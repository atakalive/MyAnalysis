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

When adding work for a new measurement, register the dataset via CLI (`python -m llm_bridge register-dataset <name> <path> [--host H]`) or GUI (File → データセットを新規登録). Both methods rewrite `config.py` in place (ast-based, atomic). CLI 登録は GUI 起動中なら自動でデータセットを開く（`--no-open` でスキップ可）。GUI 登録はアクティブデータセットを更新するがセッション復元はしない（File → データセットを開く… で明示的に復元）。 Manual editing of `DATASETS` in config.py is also supported but inline comments inside `DATASETS` will be lost on the next automated registration. When running on a new PC, add that hostname (uppercase) to each dataset you'll use. Unknown host or dataset raises a descriptive error pointing at config.py.

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

リポジトリ直下の旧 `analyses/` は削除済み（解析はデータセット配下 `<dataset_dir>/analyses/<name>/` に自己完結）。`data/analyses/` への書き込みは全廃。

## Git workflow

**`main` ブランチに直接コミットする。feature ブランチを切ってはならない。**
このリポジトリは `dev` ブランチを持たない `main` 直接運用。review tool の automerge も main に対して動作する。`feat/issue-N-xxx` 等のブランチを作ると automerge 後にゴミとして残る。

## Repo conventions

- `data/` is gitignored — safe scratch space for local outputs, caches, exports. Don't commit anything inside.
- Exploratory analysis output (figures, code snippets, notes/reports, intermediates) goes to the dataset's `work_dir` (default `<dataset_dir>/_work`, configurable per dataset via `myanalysis.toml`). Created on first save by `common.explore.save_fig()` / `save_code()` / `save_text()`.
- `.env` is gitignored. Used for LLM backend overrides (`OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_API_KEY`, `PI_API_KEY`, `LLM_BACKEND`). Copy `.env.example` to get started.
- Private repo on GitLab (`git@gitlab.com:atakalive/MyAnalysis.git`), so non-secret config like dataset paths is fine to commit.
- Windows `.bat`/`.cmd` files **must use CRLF line endings** — LF-only batch files break `cmd.exe` parsing (especially `if (...)` blocks) and fail to launch. `.gitattributes` pins `*.bat`/`*.cmd` to `eol=crlf`; keep that and don't let an editor save them as LF.

## LLM backends — `llm_backend/`

Chat-dock LLM access goes through the `llm_backend/` package. Backends implement
the `LLMBackend` Protocol in `llm_backend/base.py`; `get_backend()` selects one
by name. Five backends ship: `claude` (VS Code Claude Code engine), `openai`
(OpenAI-compatible HTTP), `mock` (offline smoke test), `pi` (pi-coding-agent
subprocess), `codex` (OpenAI Codex CLI subprocess).

**No local persistence (all backends).** Every backend's system prompt appends
the shared `NO_LOCAL_PERSISTENCE` rule from `llm_backend/base.py`: the agent must
keep all work under the dataset's `work_dir` (synced) and never stash
memory/notes/state on the local machine (e.g. the CC engine's `~/.claude`
memory) — that's the "resume the same analysis on any PC" contract. Each backend
builds its own prompt (`claude_code._SYSTEM_PROMPT`, `pi._SYSTEM_PROMPT_PI`,
`codex._SYSTEM_PROMPT_CODEX`, `gui/chat._SYSTEM_PROMPT` — the last also seeds
the `openai`/`mock` system message), so a **new backend must append this
constant too**. `tests/test_backend_prompts.py`
guards that every prompt still contains it.

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
  `--effort xhigh --settings '{"ultracode": true}'`. Both files load once (cached).
  Pick up edits by restarting, or live via **設定 → バックエンド/モデル設定**: the
  dialog rewrites `[backend].name` / `[claude_code].bin` in `config.toml` and the
  `model`/`provider` keys in `models.toml` (comment-preserving), runs an optional
  connectivity check, then `cache_clear()`s both loaders + reseeds every chat
  session's backend so it applies from the next send — no restart (Issue #94).
  The model/provider dropdowns are user-editable: ＋/－ next to each combo add or
  remove the typed value and persist the list to `models.toml` as
  `[<section>].model_choices` / `provider_choices` (saved immediately, independent
  of 適用). An absent key falls back to the seed in `engines.py`; `[]` means "no
  candidates" and is honoured. `merged_settings` overlays only bool/non-blank-str,
  so these list values never leak into a backend's settings. **`engines.PI_PROVIDERS`
  (`openai-codex` / `github-copilot` / `llama.cpp`) is the single source of truth for
  which providers pi is meant to be used with** — it seeds the dropdown *and* filters
  what the backend-status window reports. Keep them from diverging: pi's `auth.json`
  and `--list-models` can surface others (anthropic, google, …), and listing those
  implies "you can use these too" when they actually bill separately. Plain `openai`
  is not listed because it serves the same purpose as `openai-codex`. Local models
  never appear in pi's
catalog until llama-server has them loaded, which is why the list is editable.
- **Backend status window** (設定 → バックエンドの状況…) — an inventory of every supported
  driver, **independent of which engine is currently selected**. Detection lives in
  Qt-free [preflight.py](llm_backend/preflight.py) (`check_all()`), also exposed as
  `python -m llm_bridge engines`. Stages are **0. prereq (node+npm) → 1. installed →
  2. auth**, and only the first blocking stage's button is shown — offering "install"
  when npm is missing would just fail.
  - **Nothing here bills.** No LLM turn runs on open/re-check; the per-row 疎通確認
    button (`ping.ping_backend`) is the only paid path and only fires on click. There is
    deliberately no refresh timer (each probe spawns processes).
  - **Never call `pi auth print-bearer-token`**: it refreshes tokens expiring within 30
    minutes, and when the same refresh token is shared across pi(WSL)/pi(Windows)/codex
    it logs the others out. Measured-safe probes: `--version`, `codex login status`,
    `pi --list-models` (all leave the auth files byte-identical).
  - `install` doubles as the **update** command (`npm i -g <pkg>` is the same either
    way); the row labels it インストール when missing and 更新 when present, so an
    installed driver still has an upgrade path. ログイン is a separate button and stays
    available even when authenticated (account switching is legitimate).
  - `unknown` ≠ `missing`: a timeout must not offer a re-install of something already
    installed. Note that `ClaudeCodeBackend._resolve_bin` returns the bare name when PATH
    lookup fails, so preflight verifies the path actually resolves.
  - Spawned argv must go through `preflight._wrap`, which **resolves the bare name
    first** (win32 `npm` is `npm.cmd`; `Popen(["npm", ...])` is WinError 2) and then
    wraps shims in `cmd.exe /c`. Skipping step one makes every install/login button
    fail on Windows — argument-shape tests do not catch it, only running the process does.
  - **preflight returns no localised prose** — it feeds both CLI and GUI, so `*_detail`
    holds neutral facts (versions, provider names, tool output) and explanations are
    `(i18n key, params)` pairs in `notes`, translated by the caller.
- **Per-chat-session engine override** — the dialog above sets the *global default*;
  each chat tab can override it (タブ右クリック →「このチャットのモデル…」). Same two
  layers as `tool_display`: `ChatSession.engine` / `engine_model` / `engine_provider`
  in `chat_sessions/<id>.json`, where `engine` (an `ENGINES` id) is the sentinel —
  `None` = follow the global. Empty model/provider mean "that engine's configured
  default", so switching engine alone is expressible. The unit is the whole engine
  (エンジン=プロバイダ=モデル), so session A can run Claude while B runs pi+llama.cpp.
  - `SessionEngineDialog` subclasses `BackendSelectorDialog` and swaps five hooks
    (`_baseline_engine_id` / `_seed_value` / `_probe_settings` /
    `_do_apply` / `_update_warnings`); the combos, ＋/－ lists and ping shutdown are
    shared. The ping lock and the「全体設定に従う」checkbox are **separate booleans
    AND-ed** — merging them would let un-checking mid-ping re-enable the combos and
    revive the stale-result race. **適用は応答中でもブロックしない**: 進行中ターンは
    自参照の backend（`turn.backend`）で完走し、次の送信から新設定が使われる（resume
    token は `_load_backend_session` の engine-id 突合で自然無効化）。応答中の適用は
    ステータスバーで「次の送信から反映」を通知する — transcript への追記は
    `_flush_live_markdown` の anchor→末尾全置換に消されるため不可。
  - Backends are built in exactly one place, `ChatWidget._build_session_backend`,
    via `engines.session_settings()` + `build_backend()`. `session_settings` applies
    `config_patch` **only when its truthiness disagrees with the base**, matching
    `current_engine_id`'s own discriminator — `candidate_settings(engine_changed=…)`
    is wrong here (it either inherits the global's `bin` or clobbers a hand-set one,
    and makes the same override resolve differently depending on the global).
  - An engine id that will not resolve degrades to the global default and is **never
    rewritten** — a session round-tripping through an older build, or opened on a PC
    lacking that engine, keeps the user's choice. `chat_store` deliberately does not
    whitelist against `ENGINES` (it must stay importable without `llm_backend`).
  - The transcript header shows the resolved engine/model plus a 既定/個別 marker,
    built from the catalog — never by constructing a backend (it runs on every tab
    switch) and never from `sess.backend_name` (new sessions carry the prototype's
    name until their first turn).
- **AI ペルソナ（応答スタイル）** — 設定 → AIペルソナ… で全体既定の選択と定義の
  作成・編集・削除、タブ右クリック →「このチャットのペルソナ…」でセッション単位の
  上書き。定義（名前＋本文）は `data/llm_state/personas.json`
  （[llm_bridge/personas.py](llm_bridge/personas.py)。PC ローカル・GUI 管理。ファイル
  不在の間だけ `SEED_PERSONAS` を提示し、初回書込で実体化 — 空リストでもシードは
  復活しない。**`config_share.PORTABLE_FILES` には意図的に入れない**ので別 PC には
  自動では渡らない）。全体選択は ui_prefs `"chat_persona"`、セッション上書きは
  `ChatSession.persona` の 3 値センチネル（`None`=全体に従う / `""`=明示的になし /
  非空=ペルソナ名）。engine と違い**未解決名は「なし」へ degrade**（全体設定へは
  戻さない — 上書きは「全体と違える」意思表示なので別ペルソナへの勝手な差替えを
  しない）し、フィールドは never-rewrite（同期される `chat_sessions/<id>.json` を
  定義のない PC で開いても選択は保持され、定義を再作成すれば復活する）。注入は
  `base.py` の `compose_system_prompt`（persona 空なら base と**同一オブジェクト**を
  返す＝既定はバイト同一で、pinned プロンプトテスト群がそのまま回帰ガード）＋
  各バックエンドの duck-typed `set_persona`（唯一の構築点
  `_build_session_backend` で実効テキストを設定）。ペルソナが変えるのは
  **口調・文体のみ**で運用ルールが常に優先（`PERSONA_HEADER` ブロックの契約）。
  resume token はペルソナ変更で破棄しない（全バックエンドが毎ターン system
  プロンプトを再供給する — `use_provider_system_prompt` トグルと同じ前提）。
  既知の制約: codex の AGENTS.md は全セッション共有の 1 ファイルなので、ペルソナの
  異なる codex ターンが並走すると last-writer-wins（影響は口調のみ）。CLI からの
  ペルソナ操作 verb は非目標。
- **Windows の npm シム spawn（claude/pi/codex 共通）** — バックエンドの子プロセスを
  `.cmd` シムのまま `cmd.exe /c` でラップしてはならない。cmd.exe は**引数中の最初の
  改行で残り全部を切断**する（実測: 5603 文字の複数行 system プロンプトが 372 文字に
  切れ、後続の `--resume` 等のフラグごと消えた）。[common/proc.py](common/proc.py) の
  `resolve_cmd_shim` が npm シムの 2 形式（exe 直接型 = claude、node+JS 型 = pi/codex）
  を実体 argv に解決して直接 spawn し、解決不能な自作シムのみ従来ラップに
  フォールバックする。preflight の `_wrap`（インストール/ログイン等の単一行コマンド）は
  この制約の対象外。
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
  `[claude_code].use_provider_system_prompt` defaults to `true` (append MyAnalysis's
  instructions onto CC's built-in system prompt); set it `false` to replace the CC
  default so only MyAnalysis's instructions remain (`--system-prompt` instead of
  `--append-system-prompt`; also toggleable per-launch via the 設定 menu).
  **External dependency**: the VS Code Claude Code extension installed + logged in.
- **pi backend** runs `python -m llm_bridge` via the `.pi/skills/myanalysis-bridge`
  skill to drive the GUI live. **External dependency**: Node + pi
  (`npm i -g @earendil-works/pi-coding-agent` — the old `@mariozechner/…` name is
  deprecated). On Windows pi additionally needs a bash shell (Git Bash) for its
  tool execution, and **must be installed Windows-side**: `pi.py` resolves the
  binary with `shutil.which` from the Windows Python process, so a WSL-only
  install is unreachable (no `wsl.exe` wrapper, no path translation).
- **Visual feedback** (chat self-view): requires a vision-capable `[pi].model`
  or the pi-vision-proxy extension (`PI_VISION_PROXY_MODEL`).

## llm_bridge — cross-platform

`llm_bridge` (GUI ↔ CLI over the filesystem) works on Windows and POSIX. File
locking is abstracted in `common/filelock.py` (`exclusive_lock`): `fcntl` on
POSIX, `msvcrt` on Windows. `python -m llm_bridge <verb>` runs without PySide6.

### 同期マウントへの書込規律（Issue #96 — 最重要）

**同期マウント（rclone/WinFsp）上で rename-into-place をしてはならない。**

`os.replace(tmp, target)` はマウント層では成功を返しながら rclone のキャッシュ層で
`Access is denied` になり、rclone が cache item を破棄して**ファイルが 0 バイトに見える**。
例外は一切上がらない。実測（`devtools/mount_probe.py`, n=30 × 4 条件）:

| 条件 | rename 失敗 | 破損 |
|---|---|---|
| 書くだけ → `os.replace` | 0 | 0 |
| in-place write | 0 | 0 |
| **読んでから `os.replace`** | **26/30** | **26** |
| 読んでから in-place | 0 | 0 |

**直前に読んだファイルへの rename だけが壊れる**（読むと rclone が cache file の fd を
保持し、`MoveFileEx` が宛先を置換できない）。`meta.json` のような read-modify-write が
集中的に壊れ、`figures/*.png` のような書きっぱなしが無傷だったのはこのため。

規律:

- **書込は必ず `common/paths.py` の `atomic_write_text` / `atomic_write_bytes` を通す。**
  FS 種別（`common/fs_kind.py`）で戦略を切り替え、fragile では in-place write、
  local では従来の tmp+replace。どちらでも **read-back 検証 + 最大 5 回リトライ**を行い、
  最後まで検証できなければ `MountWriteError` を送出する（黙って成功にしない）。
- 生の `open(path,"w")` / `Path.write_text` / `QPixmap.save(path)` / `fig.savefig(path)` を
  データセットディレクトリに向けてはならない。バイナリは BytesIO/QBuffer でバイト列にしてから
  `atomic_write_bytes` へ渡す。
- **非再計算の JSON は `durable_write_json` / `durable_read_json`**。2 コピー（primary + `.bak`）に
  単調 `_seq` を刻み、読取は**両方を読んで新しい方を採る**（newest-wins）。`_seq` は読取時に
  剥がされるので呼び出し側からは見えない。生読みして内容比較する側は `strip_seq()` を通すこと。
- **読めないものを既定値で上書きしない。** `durable_read_json` が `unreadable` を返したら
  書込を中止する（`patch_description` / `patch_completed` が status を捨てて `{}` から
  書き直していたため、meta.json が 15 キー → 2 キーに縮退する事故が起きた）。
  `myanalysis.toml` も 0 バイトは「設定なし」ではなく破損として `ConfigUnreadableError`。
- ロックファイルはマウント外（`data/locks/`）へ自動マッピングされる（`common/filelock.py`）。
  マウント上では排他が効いている保証がなく、同期チャーンも生むため。PC 間排他は元々成立しない。
- バイトコードは `PYTHONPYCACHEPREFIX` でローカルへ退避する（`run.bat` と両バックエンドが設定）。
  マウント上の `.py` を import すると CPython が `__pycache__/*.pyc` を tmp+rename で書き、
  同じ失敗経路に乗る（実測で rename 失敗の 16%）。
- **チャットエージェントの `Write`/`Edit` はマウント上で機械的に拒否される**（PreToolUse hook →
  `python -m llm_bridge guard-write`）。エージェントのツールは我々の chokepoint を通らないため。
  拒否時は安全な経路（`save_text` / `save_code` / `save_fig` と draft→apply verb）が案内される。
  例外は `analysis.draft.py` — draft は Write/Edit を許可する（`apply-analysis` が strip /
  `ast.parse` / `build_tab` 束縛の 3 ゲートを通してからしか昇格させないので、draft が
  0 バイト化しても `analysis.py` に伝播しない）。hook は内部エラー時に必ず fail-open する。

診断と復旧: `python -m llm_bridge doctor [--repair] [--rescue]`
（0 バイトファイル・primary/.bak の乖離・空 TOML・rclone キャッシュの孤児 tmp・
ログの失敗イベントを報告。`--repair` は newest-wins で収束、`--rescue` は 0 バイトファイルを
キャッシュの孤児 tmp から復元）。実マウント上での検証は `python -m devtools.mount_probe`。

キルスイッチ: `MYANALYSIS_WRITE_STRATEGY=replace` で従来挙動へ戻せる。
`MYANALYSIS_FS_OVERRIDE="M:=fragile,D:=local"` で判定を明示上書き。

### 既存 analysis.py の編集はマウント安全経路で（Issue #89）

チャットエージェントが既存 `analyses/<name>/analysis.py` を Edit/Write で直接編集すると、同期マウント上の書き込み失敗で 0 バイトに truncate され得る。安全経路の 3 verb を使う（いずれも `--dataset <ds>` 基本形）: `draft-analysis <name> --dataset <ds>`（analysis.py を work_dir 上の編集用 draft へコピー）→ draft を自由に編集 → `apply-analysis <name> --dataset <ds>`（構文＋トップレベル `build_tab` 束縛を検証してから `atomic_write_text` で昇格。draft は残す）。0 バイト化してしまったら `recover-analysis <name> --dataset <ds>`（`.bak` から復旧。0 バイト or 不在のときだけ復旧し中身があれば上書きしない）。`.bak` は **「最後にアプリへ正常反映（タブ成立）したビルドの内容」** を add-tab / reload-tab の成功末尾で自動退避したもの。ただし `.bak` は analysis.py と同じ同期マウント上にあり drive 単位の障害は救えない — 深いバックアップは git／チャット履歴。

## Exploratory analysis — `common/explore.py`

LLM agents analyse data via code execution + CLI, not just GUI remote control.
`common/explore.py` provides a minimal surface: `load_dataset(name, subdir_pattern=..., csv_name=...)`
(config → loaders in one call — 既定パターンは無い。まず `dataset_summary` で実構成を確認してから
実在のパターンを渡す), `save_fig(name, fig, label)` (saves to `<work_dir>/figures/<label>.png`),
`save_code(name, label, content)` (saves to `<work_dir>/code/<label>.py`。**label に拡張子は
付けない** — ドットを含めると `save_text` を案内する ValueError。以前は黙って
`notes.md.py` が出来ていた。`save_fig` の label も同様),
`save_text(name, relpath, content)` (saves to `<work_dir>/<relpath>` — 任意の拡張子と
サブディレクトリを受ける汎用テキスト書込。`.md`/`.csv`/`.json`/`.txt` はこれで書く。マウント上では
エージェントの Write/Edit が hook で拒否されるので、これが唯一の正規経路),
`dataset_summary(name)` (データセット直下の実構成＝subdirs と代表 CSV の columns/rows を歩いて報告する
“まず見る”ステップ). Output goes to the
dataset's `work_dir` (default `<dataset_dir>/_work`, set in `myanalysis.toml`); the
sidecar `myanalysis.toml` and `work_dir` are created on first save. Measurement files
(CSV etc.) are never modified — but a hand-set `work_dir` may place new output files
in any subdirectory of the dataset dir. Use `python -m llm_bridge list-datasets`
to discover registered dataset names.

`save_text` の relpath は `common/paths.py` の `validate_relpath` + `resolve_under` が検証する:
`..`・絶対/ルート相対/ドライブ相対/UNC・Windows 禁止文字 `<>:"|?*`・先頭ドット・末尾ドット/空白・
**拡張子付きも含む予約デバイス名**（`nul.txt` は NUL デバイス）を拒否し、work_dir 配下への
封じ込めを `safe_resolve` で再チェックする（symlink 脱出もここで落ちる）。深さ上限は設けない。
`<work_dir>/session.json` と `chat_sessions/`、および任意の `*.bak` は GUI の live 状態なので拒否する。

## Session save/restore

データセット単位のセッション（開いていたタブ構成・アクティブタブ）は引き続き `<work_dir>/session.json` に保存・復元する（データバインドの真実ソース＝同期ドライブでどこでも開ける）。現在開いているデータセットは `python -m llm_bridge active` の `active_dataset`/`dataset` フィールド、開いているデータセット一覧は `open_datasets` フィールド（または `list-open-datasets`）で取得できる。

- 保存: File → 「セッションを保存」、「保存して終了」、✕ 終了時の Yes/No/Cancel ダイアログ。
- 復元: File → 「データセットを開く…」、CLI `window open-dataset name=<dataset>`。
- `llm_bridge/session.py` が中核。`show` verb の `dataset=` 引数でタブ→データセット紐付け。
- `session.json` は durable 書込（`common/paths.py` の `durable_write_json` = primary + `.bak` の 2 コピー＋書込後 read-back 検証。同期マウントの 0 バイト truncate 対策）。検証失敗は `save_all` の failed に載り、Tier 3/4 リロード中止・「保存して終了」の close 拒否・close_dataset 中止という既存経路が発火する。読取は `.bak` フォールバック付きで、破損して回復不能なら `no-session` と区別して `unreadable-session:<ds>`（GUI が警告・破損ファイルは上書きしない）。
- 暫定運用の `_work/code/restore_view.py` 方式は本機能で置換済み。

#### ネイティブ resume token は同期しない（PC ローカル）

チャット履歴（`messages`）は `<work_dir>/chat_sessions/<id>.json` に同期されるが、
**ネイティブ resume token（claude `--resume` / pi `--session` / codex `exec resume`）は
同期してはならない。** 指す先の実体（`~/.claude`, `~/.pi/agent/sessions`, `~/.codex`）が
PC ローカルだからで、同期すると別 PC で存在しない ID を `--resume` に渡すことになる
（README 設計原則「ローカルには何も残さない／どの PC でも再開」の帰結）。

- 置き場所は `data/llm_state/backend_sessions.json`（PC ローカル・gitignored）。
  `recent_datasets.json`（MRU）/ `last_window.json`（workspace）と同じ machine 状態の層。
  `{session id: {engine, backend, token, updated}}`。`ChatSession.backend_session_id` は
  `draft` と同じくフィールドだけ残した**非永続**値で、毎ターンここから再充填される。
- **`engine`（エンジン ID）と `backend`（名前）の両方が一致したときだけ token を使う。**
  `ClaudeCodeBackend.name` は claude-vscode と claude-cli で同じ `"claude-code"` なので、
  名前だけでは別エンジンの token を素通ししてしまう。
- **失敗したターンでは token を捨てる**（`_forget_backend_session`）。バックエンドの
  `_session_id` は成功イベントでしか代入されないので、失敗後のインスタンスには死んだ token が
  残っている。これを書き戻すと「死んだ token で `--resume` → 失敗」を永久に繰り返し、しかも
  `_session_id` が非 None なので `replay=False` になり履歴すら送られず、そのチャットが恒久的に
  壊れる。ただし **ユーザーの Stop（`turn.stopped`）は例外で token を維持する** — Stop も
  `_on_failed` に落ちるため、一律破棄にすると中断のたびに全履歴再送になる（replay に上限は無い）。
- エントリは書込のたびに TTL（90 日）で掃除する。セッション ID 突合では孤児を消せない
  （閉じているデータセットのチャットは GUI に載らない）ため年齢で切る。セッション削除時は明示的に破棄。
- テストは `tests/conftest.py` の autouse fixture が `backend_sessions_path` を tmp へ向ける。
  ここを外すと開発者の実チャットの token をテストが上書きする。

### Multiple datasets（Issue #51 — ワークスペース）

1 プロセスに複数データセットを同時に開ける。トップの `DatasetSwitcher` で切り替えると、そのデータセットの解析タブ群とチャットセッション群に入れ替わる。

- **ワークスペースメンバー一覧**（どのデータセットが一緒に開いていたか＋アクティブ）だけを repo-local・gitignored の `data/llm_state/last_window.json`（`{version, datasets, active}`）に集約する。#50 の `recent_datasets.json`（MRU＝履歴順）の隣に並ぶ 2 つ目の PC ローカルレコード（workspace＝同時開き集合）。両者とも **PC 間同期はしない** machine/window 状態で、per-dataset のタブ内容 `session.json` が同期側を担う。Tier 4 の `reload_manifest.json`（transient）とも別レコード。`datasets` は DS タブの表示順（ドラッグ並べ替えを反映し、復元で再現される。Issue #59）。
- 復元は **手動**：File →「前回のセッションを復元」（起動時自動復元はしない）。復元は **ADDITIVE**（既に開いているデータセット/タブは閉じない・上書きしない）。
- 会議共有（meeting relay）は **開いている全データセットのタブとチャットを既定で** ゲストへ配信する（Issue #78。非公開にするのは明示的なオプトアウトのみ）。同名タブはデータセットごとに別物として扱われる（ワイヤは `tabs_by_dataset` の DS 単位名前空間）。ゲスト HTML はホストの「DS レイヤー → タブレイヤー」をミラーし、DS バーは共有中の全 DS をクリック可能なチップとして並べる。`curDs` はゲストローカルの真実ソースで、ホストのアクティブ DS は初回ロードの既定値を決めるだけ（タブ選択・履歴・未送信ドラフトは DS 単位で保持）。DS バー右端の **「ホストに追従」トグル**（既定 OFF・非永続、Issue #80）を ON にした時だけホストのアクティブ DS へ追従し、DS チップの手動クリックで OFF に戻る。
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
