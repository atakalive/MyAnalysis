# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Experimental measurement data analysis project (Python). Data lives **outside the repo** on a synced drive; only code is versioned here.

## Data access — always go through `config.py`

Raw measurement data is not in the repo. It lives on a synced drive whose mount point varies by PC. The registry maps each dataset name to a `{hostname: full_path}` dictionary, resolving the per-PC mount point difference. Lookup uppercases this PC's hostname (`socket.gethostname().upper()`) and matches it exactly against the host keys; the keys themselves are not normalised, so they must be uppercase. `register_dataset` (CLI `register-dataset` and the GUI) uppercases the host before writing; a lowercase key hand-written into `datasets.local.json` never matches.

The registration data itself is **not in code**: it lives in `datasets.local.json` at the repo root (Git-ignored; storage implemented by [dataset_registry.py](dataset_registry.py)), and [config.py](config.py) is the access API (`DATASETS`, `get_dataset_dir`, `register_dataset`, `reload_datasets`). A fresh checkout has no registry file and starts empty.

**Never hardcode `G:\...` or any absolute data path in analysis code.** Always:

```python
from config import get_dataset_dir
path = get_dataset_dir("my_dataset")
```

When adding work for a new measurement, register the dataset via CLI (`python -m llm_bridge register-dataset <name> <path> [--host H]`) or GUI (File → データセットを新規登録). Both methods write `datasets.local.json` under a lock (verified `atomic_write_text`). **Never edit `config.py` to register a dataset.** CLI 登録は GUI 起動中なら自動でデータセットを開く（`--no-open` でスキップ可）。GUI 登録も登録後に `open-dataset` verb でそのデータセットを開く（File → データセットを開く… と同じ経路。保存済みのタブ構成・チャットがあれば復元する。open-dataset が未登録の素の ToolWindow では開かない）。登録の削除は File → データセットを開く… の「登録を削除」（`config.unregister_dataset`。登録簿からのみ外し DS フォルダには触れない。開いていれば先に閉じる）。同じ一覧の「この PC のパスを登録…」は可用性 `no-host` の行（他 PC の登録は届いているがこのホストのパスが無い）でだけ有効で、`config.register_dataset`（このホスト）→ `try_push` を行う（Issue #112。`missing` の行は同期ドライブ未マウントのことが多いので対象外。疑わしいフォルダは `_path_doubts` で確認を出す。GUI の新規登録と違い自動では開かない）。R2 同期（[config_share.py](config_share.py)）の登録簿マージは union・非破壊なので、削除は明示的な tombstone（ローカル状態と bundle の `deleted`、bundle schema 2）でだけ伝播する — tombstone を経ずに登録簿から消しても次回 sync で remote から復活する。自動同期は、GUI 起動時と CLI `register-dataset` の後が `config_share.try_sync`（双方向）、GUI での登録・登録削除の直後が `config_share.try_push`（送信のみ。`gui/config_push.py` の `ConfigPusher` がデーモンスレッドで実行し GUI スレッドを塞がない。push ロックで直列化。GUI からのバックグラウンド pull はしない）。 Hand-editing `datasets.local.json` is also supported (plain UTF-8 JSON, `{dataset: {HOST: path}}`); JSON has no comments, and key order is not a contract. A corrupt registry raises `RegistryError` instead of degrading to an empty config. When running on a new PC, add that hostname (uppercase) to each dataset you'll use. Unknown host or dataset raises a descriptive error pointing at the register CLI / `datasets.local.json`.

旧構成（`config.py` 内の `DATASETS` リテラル）からの移行は `python -m devtools.migrate_dataset_registry --source <旧 config.py> [--output <registry.json>] [--dry-run]`（旧ファイルを実行せず `ast.literal_eval` で読む。既存の異なる登録簿は上書きしない）。

### Per-dataset settings — `myanalysis.toml`

Settings specific to one dataset live in `myanalysis.toml` at the top of that dataset's directory (not in the registry `datasets.local.json`), so they sync with the data and follow it across PCs/repos. [dataset_config.py](dataset_config.py) reads/generates it. There are two settings: `work_dir` — where analysis output is saved (default `_work`) — and `format` — how the dataset is loaded (default `csv_per_subdir`). The tools write this sidecar mechanically; it's safe to hand-edit. Measurement files (CSV etc.) are never modified.

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

パス解決は [dataset_config.py](dataset_config.py) の `analyses_root` / `analysis_file` / `state_dir` / `batch_dir`（read 経路は `create=False` で副作用なし）。`mod.DATASET`（scaffold が焼く）は `load()` のデータ読込にのみ使い、出力先・セッション紐付けは所在データセット（引数 `dataset`）が真実ソース。メニュー列挙は「現在開いているデータセットのみ」。**別データセットの同名解析を同時に開くのは Issue #51 で対応済み**：タブはデータセットごとの `_DatasetGroup`（トップの `DatasetSwitcher` で切替）にグループ化され、タブ ID は「グループ内で一意」になる。`add-tab`/`show`/`show-image`/`set-active-tab`/`close-tab`/`reload scope=tab`/tab-tier verb は任意 `dataset=` を受ける。省略時は、データセットのあるチャットのエージェントからならチャットのデータセット（Issue #111。「Multiple datasets」節）。チャット外の CLI とデータセットの無いチャットからなら従来どおり各 verb の既存の規則（CLI の verb と `add-tab` は前面のデータセット。タブ名は開いている全データセットから前面を優先して探す。`show` / `show-image` は同名のタブを先に探し、無ければパスから推定）。

リポジトリ直下の旧 `analyses/` は削除済み（解析はデータセット配下 `<dataset_dir>/analyses/<name>/` に自己完結）。`data/analyses/` への書き込みは全廃。

## Git workflow

**`main` ブランチに直接コミットする。feature ブランチを切ってはならない。**
このリポジトリは `dev` ブランチを持たない `main` 直接運用。

## Repo conventions

- `data/` is gitignored — safe scratch space for local outputs, caches, exports. Don't commit anything inside.
- Exploratory analysis output (figures, code snippets, notes/reports, intermediates) goes to the dataset's `work_dir` (default `<dataset_dir>/_work`, configurable per dataset via `myanalysis.toml`). Created on first save by `common.explore.save_fig()` / `save_code()` / `save_text()`.
- `.env` is gitignored. It holds secrets and env overrides, loaded into the GUI process at startup by `common/env.py` (existing env vars win) and inherited by agent subprocesses: LLM backend (`OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_API_KEY`, `LLM_BACKEND`, `CLAUDE_CODE_BIN`, `CODEX_BIN`), R2 config sync (`R2_*`), meeting share (`RELAY_ADMIN_KEY`, `CLOUDFLARE_TUNNEL_NAME` / `CLOUDFLARE_TUNNEL_HOSTNAME` / `CLOUDFLARE_TUNNEL_CRED`, `CLOUDFLARED_BIN`, `RELAY_LAN_HOST`, `RELAY_LAN_PORT`, `RELAY_VIEW_MAX_MP`) and the sync-mount write strategy (`MYANALYSIS_WRITE_STRATEGY`, `MYANALYSIS_FS_OVERRIDE`, `MYANALYSIS_FORCE_FRAGILE`). pi does not read `PI_API_KEY` (use provider-specific keys). Copy `.env.example` to get started. `MYANALYSIS_CHAT_DATASET` is set by the GUI for agent subprocesses only — never put it in `.env`.
- Dataset registration (per-host paths) lives in the git-ignored `datasets.local.json`, **not** in tracked source — don't commit real registration data (names, hostnames, paths). Other non-secret config may still be committed.
- Windows `.bat`/`.cmd` files **must use CRLF line endings** — LF-only batch files break `cmd.exe` parsing (especially `if (...)` blocks) and fail to launch. `.gitattributes` pins `*.bat`/`*.cmd` to `eol=crlf`; keep that and don't let an editor save them as LF.

## LLM backends — `llm_backend/`

Chat-dock LLM access goes through the `llm_backend/` package. Backends implement
the `LLMBackend` Protocol in `llm_backend/base.py`; `get_backend()` selects one
by name. Five backends ship: `claude` (VS Code Claude Code engine), `openai`
(OpenAI-compatible HTTP), `mock` (smoke test; ignores the input, but fetches the crude-oil price from stooq.com), `pi` (pi-coding-agent
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

**Chat dataset (Issue #111).** The claude / codex / pi prompts also append `CHAT_DATASET_RULE` (`llm_backend/base.py`). Every backend implements a duck-typed `set_chat_dataset(ds)` (called by `ChatWidget._start_turn` every turn): the CLI backends prepend `with_chat_context` to the prompt only when `_session_id is None` and set / pop `MYANALYSIS_CHAT_DATASET` in `_build_env`; openai composes a chat-dataset note into the system payload (`_payload_messages`); mock ignores it. A new backend must do the same.

- **Selection order**: env `LLM_BACKEND` → `[backend].name` in
  `llm_backend/config.toml` → `OPENAI_BASE_URL` back-compat (`mock` → mock,
  anything else → openai), resolved by `llm_backend.resolve_backend_name()`.
  None of them = no engine: `get_backend()` raises `NoEngineConfigured`. In the
  chat, that and any other failure to build the global backend (e.g. an unknown
  name) leave `ChatWidget` holding an `UnconfiguredBackend`
  (`llm_backend/unconfigured.py`): it shows a notice line and refuses to send,
  never falling back to api.openai.com or the mock (Issue #115).
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
  `--effort xhigh --settings '{"ultracode": true}'`. pi maps `effort` to `--thinking`, codex to `-c model_reasoning_effort=`. The dialog's effort row is a fixed dropdown (`Engine.effort_levels`; codex narrows it per model via `engines.effort_choices` → `codex.supported_efforts`, which reads `$CODEX_HOME/models_cache.json`); a value already set when the dialog opens (hand-written or synced) is kept as an extra item even if it is not in the list, but only until the list changes: when editing codex's model field changes the list, a selected value missing from the new list falls back to the first entry (既定 / 全体設定と同じ), so the GUI never newly selects a level the model does not support (`_populate_effort(keep_current=True)` compares against `_effort_choices`). The codex backend never sends a level the catalog says the model lacks (`codex.sendable_effort`, checked in `CodexBackend.stream`; the header uses the same check via `engines.sent_effort`) — a session's 全体設定と同じ inherits the global effort, which can meet a different session model, so the dialog alone cannot prevent that pairing. The claude backend drops `CLAUDE_CODE_EFFORT_LEVEL` from the child env whenever an effort is set (that env var beats `--effort`). Both files load once (cached).
  Pick up edits by restarting, or live via **設定 → バックエンド/モデル設定**: the
  dialog rewrites `[backend].name` / `[claude_code].bin` in `config.toml` and the
  `model`/`provider`/`effort` keys in `models.toml` (comment-preserving; an empty effort also clears a leftover `effort` in `config.toml`), runs an optional
  connectivity check, then `cache_clear()`s both loaders + reseeds every chat
  session's backend so it applies from the next send — no restart (Issue #94).
  The model/provider dropdowns are user-editable: the 追加/削除 (Add/Remove) buttons next to each combo add or
  remove the typed value and persist the list to `models.toml` as
  `[<section>].model_choices` / `provider_choices` (saved immediately, independent
  of 適用). Every read-then-write edit of a choice list (追加/削除 and モデル取得) goes
  through `engines.update_choices`, which re-reads the list from `models.toml` while
  holding its lock (`settings_store.update_toml_keys`). The `model_config` cache can be
  stale after an R2 sync or another process's write, and building the new list from the
  cache would silently drop those changes. An absent key falls back to the seed in `engines.py`; `[]` means "no
  candidates" and is honoured. `merged_settings` overlays only bool/non-blank-str,
  so these list values never leak into a backend's settings. **`engines.PI_PROVIDERS`
  (`openai-codex` / `github-copilot` / `llama.cpp`) is the single source of truth for
  which providers pi is meant to be used with** — it seeds the dropdown, filters
  what the backend-status window reports, *and* limits which `pi --list-models` rows
  モデル取得 appends. Keep them from diverging: pi's `auth.json`
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
    deliberately no refresh timer (each probe spawns processes). モデル取得 (fetch models)
    runs no inference either, and never verifies the fetched IDs with a ping-pong turn —
    spending tokens stays the user's 疎通確認 click alone.
  - **Never call `pi auth print-bearer-token`**: it refreshes tokens expiring within 30
    minutes, and when the same refresh token is shared across pi(WSL)/pi(Windows)/codex
    it logs the others out. Measured-safe probes: `--version`, `codex login status`,
    `pi --list-models`, `claude auth status --json` (all leave the auth files
    byte-identical; the last measured on claude 2.1.282 / Linux).
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
  - **モデル取得 (fetch models)** — Qt-free `llm_backend/model_catalog.py` (`fetch_models`,
    never raises; `SUPPORTED` = claude-vscode / claude-cli / pi / codex) reads the IDs the
    installed tool itself knows and **only appends** the new ones to
    `[<section>].model_choices` (`append_new`). Nothing is removed (stale IDs are the user's
    to remove with 削除), and nothing is written when there is nothing new, so an
    uncustomised list stays on the seed. Sources: claude = spawn the engine in stream-json
    mode, send only an `initialize` control_request, read `response.response.models[].value`
    (minus `default`), close stdin — no `user` message, so no turn runs and no session file
    is written (measured on 2.1.282); pi = `pi --list-models` rows whose provider is in
    `PI_PROVIDERS` (only `[pi].provider` when set); codex = read
    `$CODEX_HOME/models_cache.json` (default `~/.codex`) without spawning codex, which
    refreshes that file from the server on each run. Safe because codex is a file read,
    pi's probe leaves the auth files byte-identical, and claude's token is not shared with
    other tools (the shared refresh-token hazard is pi ↔ codex only). It runs only on the
    button and once after a successful (rc 0) install/update of that engine — never on
    open/re-check, no timer. The OpenAI-compatible row has no button: no `/v1/models`, no
    web scraping, no filtering UI for huge lists. The append goes through
    `model_catalog.append_fetched` → `engines.update_choices`, so it lands on the list as it
    is on disk at that moment, not on the list the fetch started from. An ID the tool still
    lists comes back on the next fetch even if the user removed it (append-only). On win32
    npm cannot replace a running `claude.exe` / pi (EBUSY), so the window's install/update,
    fetch and 疎通確認 share one busy state (`_busy()` / `_apply_busy()`): only one runs at a
    time, and 再確認 is disabled while busy too (the probe spawns `--version` etc.). A
    worker still running after the window closes keeps its slot — and so busy — until it
    finishes (`_stop_all` does not drop it), and an install's done only changes state when
    its token matches `_install_token`, so a late done from before the close cannot clear or
    re-arm a newer install. Engine processes outside this window (a chat turn, the settings dialog's 疎通確認,
    other apps) are not tracked; an update then may fail with npm's own error (rc ≠ 0, no
    auto fetch).
- **Per-chat-session engine override** — the dialog above sets the *global default*;
  each chat tab can override it (タブ右クリック →「このチャットのモデル…」). Same two
  layers as `tool_display`: `ChatSession.engine` / `engine_model` / `engine_provider` / `engine_effort`
  in `chat_sessions/<id>.json`, where `engine` (an `ENGINES` id) is the sentinel —
  `None` = follow the global. Empty model/provider/effort mean "that engine's configured
  default", so switching engine alone is expressible. The unit is the whole engine
  (エンジン=プロバイダ=モデル), so session A can run Claude while B runs pi+llama.cpp.
  - `SessionEngineDialog` subclasses `BackendSelectorDialog` and swaps six hooks
    (`_baseline_engine_id` / `_seed_value` / `_probe_settings` /
    `_do_apply` / `_update_warnings` / `_effort_default_label`); the combos, 追加/削除 choice lists and ping shutdown are
    shared. The ping lock and the「全体設定に従う」checkbox are **separate booleans
    AND-ed** — merging them would let un-checking mid-ping re-enable the combos and
    revive the stale-result race. **適用は応答中でもブロックしない**: 進行中ターンは
    自参照の backend（`turn.backend`）で完走し、次の送信から新設定が使われる（resume
    token は `_load_backend_session` の engine-id 突合で自然無効化）。応答中の適用は
    ステータスバーで「次の送信から反映」を通知する — transcript への追記は
    `_flush_live_markdown` の anchor→末尾全置換に消されるため不可。 The effort row's first entry is 既定 (= no flag, CLI default) in the global dialog and 全体設定と同じ (`engine_effort=None`, keeps following the global `models.toml` effort of that engine) in the session dialog; the session dialog never seeds the global value into the override.
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
  interactive permission prompt would deadlock); to tighten, set `permission_mode`
  to a mode other than `bypassPermissions` (`allowed_tools` alone does not restrict
  under `bypassPermissions`). An empty, whitespace-only or non-string value falls back to
  `bypassPermissions` (warning + a note in the backend status window); any other string is passed to `--permission-mode` as is.
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
- **Chat search (Ctrl+F, Issue #108)**: `gui/chat_search.py` (dialog, text-hit Markdown, history panel) + `llm_bridge/chat_search.py` (Qt-free core, verb helpers, AI prompt) + `llm_bridge/chat_search_history.py`. Results are ordinary `ChatSession`s with `kind="search"` (saved like any chat; excluded from every search and verb; private by default in meeting share via `classify_private_sessions`; a fork becomes `kind="chat"`). Message `idx` is always the real index in `sess.messages` on every path (hits, `chat-show`, links, `_msg_positions`). Hit links are `chatsearch:<sid8>:<idx>[?q=…]` (`ChatWidget._on_anchor_clicked` → `_on_search_link`; highlight with `extraSelections`). AI search sends the instructions + chat index as the first turn's `wire_text` (the transcript keeps only the short display line), re-sends a short instruction block with every follow-up turn from the session's persisted `search_spec` (`_start_turn` → `_search_followup_wire`), and bakes `[chat_search]` of `llm_backend/config.toml` into the new session's per-chat engine override. Window verbs `chat-list` / `chat-search` / `chat-show` (OpenAI tools `chat_list` / `chat_search` / `chat_show`) read the GUI's in-memory chats; the AI prompt passes the scope as `search_tab=<sid8>` (resolved from that tab's `search_spec`), never as a dataset name. Search history: `<work_dir>/chat_search_history.json` via `durable_write_json`; never appended while it is `unreadable` with content. Dialog choices: ui_prefs `chat_search_scope` / `chat_search_archived` / `chat_search_ai` / `chat_search_history_open`.

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
- **ただし 0 バイトは「守るべき破損」ではなく「守る中身が無い」。** `unreadable` を一律 skip に
  すると、一度 0 バイト化したファイルには二度と書けなくなる — 実測で
  `data/llm_state/backend_sessions.json` が 0 バイトのまま固着し、resume token が全エンジンで
  一度も保存されていなかった（`--resume` が使われず毎回全履歴 replay）。判定は
  `llm_bridge/paths.py` の `_preserve_unreadable`（サイズ 0 なら書き直す・stat 不能なら守る側）。
- **PC ローカルの `data/llm_state` も chokepoint の例外ではない。** 同期マウントではないが 0 バイト化は
  現に起きた。`update_ui_pref` / `note_recent_dataset` / `_write_backend_sessions` は
  `atomic_write_text`（再計算可能なので 1 コピー）、`personas.json` は再生成できずコピーも無いので
  `durable_write_json`。旧実装は 4 writer が固定 tmp 名 `<name>.json.tmp` を共有しており、GUI 二重起動や
  Tier 4 restart で「A が tmp を truncate → B が replace」の競合が起き得た（mkstemp で構造的に解消）。
  検出は `python -m llm_bridge doctor`（`check_local_state`。`--repair` は削除＝次回書込で再生成）。
- ロックファイルはマウント外（`data/locks/`）へ自動マッピングされる（`common/filelock.py`）。
  マウント上では排他が効いている保証がなく、同期チャーンも生むため。PC 間排他は元々成立しない。
- バイトコードは `PYTHONPYCACHEPREFIX` でローカルへ退避する（`run.bat` と claude / codex / pi の各バックエンドが設定。`python -m export` は `sys.pycache_prefix` を自分で設定）。
  マウント上の `.py` を import すると CPython が `__pycache__/*.pyc` を tmp+rename で書き、
  同じ失敗経路に乗る（実測で rename 失敗の 16%）。
- **claude エンジンでは、チャットエージェントの `Write`/`Edit` がマウント上で機械的に拒否される**（PreToolUse hook →
  `python -m llm_bridge guard-write`。codex / pi にこの hook は無く、プロンプト `MOUNT_SAFE_EDITS` の指示だけ）。エージェントのツールは我々の chokepoint を通らないため。
  claude で拒否されたときは安全な経路（`save_text` / `save_code` / `save_fig` と draft→apply verb）が案内される。
  例外は `analysis.draft.py` — draft は Write/Edit を許可する（`apply-analysis` が strip /
  `ast.parse` / `build_tab` 束縛の 3 ゲートを通してからしか昇格させないので、draft が
  0 バイト化しても `analysis.py` に伝播しない）。hook は内部エラー時に必ず fail-open する。

診断と復旧: `python -m llm_bridge doctor [--repair] [--rescue]`
（0 バイトファイル・primary/.bak の乖離・空 TOML・rclone キャッシュの孤児 tmp・
ログの失敗イベントを報告。`--repair` は newest-wins で収束、`--rescue` は 0 バイトファイルを
キャッシュの孤児 tmp から復元。rclone の節は `--cache`/`--log`（または `MYANALYSIS_RCLONE_CACHE`/`MYANALYSIS_RCLONE_LOG`）で指定したものだけ。`--rescue` はキャッシュ必須）。実マウント上での検証は `python -m devtools.mount_probe --dir <マウント上の既存のディレクトリ> --log <rclone のログ>`（`--dir` と `--log` は必須。実行ごとに `--dir` の下へ専用ディレクトリを作り、終了時にそれだけを消す）。

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
エージェントに Write/Edit で直接書かせない（claude は hook で拒否、codex / pi はプロンプトの指示だけ）ので、これが唯一の正規経路),
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

データセット単位のセッション（開いていたタブ構成・アクティブタブ）は引き続き `<work_dir>/session.json` に保存・復元する（データバインドの真実ソース＝同期ドライブでどこでも開ける）。現在開いているデータセットは `python -m llm_bridge active` の `active_dataset`/`dataset` フィールド、開いているデータセット一覧は `open_datasets` フィールド（または `list-open-datasets`）で取得できる。チャットのエージェントが実行した `active` には、そのチャットのデータセット `chat_dataset`（チャット外・データセット未設定は `null`）も出る（`active.json` 自体には無い）。

- 保存: File → 「セッションを保存」、「保存して終了」、✕ 終了時の Yes/No/Cancel ダイアログ。
- 復元: File → 「データセットを開く…」、CLI `window open-dataset name=<dataset>`。
- `llm_bridge/session.py` が中核。`show` verb の `dataset=` 引数でタブ→データセット紐付け。
- `session.json` は durable 書込（`common/paths.py` の `durable_write_json` = primary + `.bak` の 2 コピー＋書込後 read-back 検証。同期マウントの 0 バイト truncate 対策）。検証失敗は `save_all` の failed に載り、Tier 3/4 リロード中止・「保存して終了」の close 拒否・close_dataset 中止という既存経路が発火する。読取は `.bak` フォールバック付きで、破損して回復不能なら `no-session` と区別して `unreadable-session:<ds>`（GUI が警告。破損ファイルは自動では上書きせず、File → セッションを保存 の確認で「はい」のときだけ上書き）。
- 暫定運用の `_work/code/restore_view.py` 方式は本機能で置換済み。

### 入れ子パネル分割（Issue #97）

- `AnalysisTab`（[gui/tab.py](gui/tab.py)）の splitter 部は**分割木**: root `_splitter` は常に 2 子、子は葉（QWidget+QVBoxLayout）か入れ子 QSplitter（常に 2 子）。slot は `left|right|top|bottom` を `/` で繋いだパス（[common/slots.py](common/slots.py)、Qt 非依存・深さ 6 まで）。配置（`show`/`show-image slot=`）だけが向きを変え、読み取り系（`close-pane`/`set-split slot=`/画像 verb の `slot=`）は厳密照合。
- **bridge 所有ペイン**: `show`/`show-image` が置いたパネルだけが `tab._bridge_panes`（key → kind/path）に載り、移動・破棄・永続化の対象になる。解析が `add_panel` したパネルを含む葉/領域への分割・畳み込み・close は LookupError（事前検査で無変更）。パネル key（`figure`/`figure-2`/`viewer-2`…）は構築順の識別子で位置を表さない。
- session: 旧フィールド（`figure`/`figure2`・`image`/`image2`）で表現できる配置は従来形式、それ以外（深さ 2 以上・図と画像の混在・単一画像の top/bottom 等）は `panes: [{slot, kind, path}]` 形式で保存する（`layout.splits` に入れ子の sizes）。**旧ビルドは `panes` を知らず先頭 1 枚で縮退表示し、次の保存で他ペインを落とす**（既知の制約）。復元は事前クリアせず差分適用＋`prune_panes`（成功 0 件なら既存タブ無変更）。
- Tier 1 hot reload 前から生き残っている旧 viewer タブ（`viewer_host` 属性が無く、dataset 未解決で spec も無いもの）はホスト扱いされず、slot 付き `show`/`show-image` は LookupError になる（安全側の縮退）。`reload scope=restart` かタブを開き直せば解消。

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
- 会議共有（meeting relay）は **開いている全データセットのタブとチャットを既定で** ゲストへ配信する（Issue #78。非公開にするのは明示的なオプトアウトのみ）。データセット未設定・閉じたデータセットのチャットとタブは配信しない（Issue #107）。opt-out は停止・開始を跨いで保持（アプリ終了まで）。同名タブはデータセットごとに別物として扱われる（ワイヤは `tabs_by_dataset` の DS 単位名前空間）。ゲスト HTML はホストの「DS レイヤー → タブレイヤー」をミラーし、DS バーは共有中の全 DS をクリック可能なチップとして並べる。`curDs` はゲストローカルの真実ソースで、ホストのアクティブ DS は初回ロードの既定値を決めるだけ（タブ選択・履歴・未送信ドラフトは DS 単位で保持）。DS バー右端の **「ホストに追従」トグル**（既定 OFF・非永続、Issue #80）を ON にした時だけホストのアクティブ DS へ追従し、DS チップの手動クリックで OFF に戻る。
- **チャットのデータセット（Issue #111）**: チャットは 1 つのデータセットに属する（`sess.dataset`）。`ChatWidget._start_turn` が毎ターン `set_chat_dataset` でバックエンドに渡し、エージェントには (1) 新規ネイティブセッションの最初のプロンプト先頭の `<myanalysis_context>`、(2) 子プロセスの環境変数 `MYANALYSIS_CHAT_DATASET`（`python -m llm_bridge` が読む）、(3) コマンドの `caller_dataset`（`commands._execute` が既定に使う）で伝わる（(1) は新しい会話だけ。途中で DS を採用したチャットとこの変更より前の resume token を持つチャットには届かず、エージェントは `active` の `chat_dataset` で知る）。dataset を省略した `state <name>` / `annotate` / `clear-annotations` / `draft|apply|recover-analysis`、`tab` verb、window verb `add-tab` / `close-tab` / `set-active-tab` / `show` / `show-image` / `reload scope=tab` はチャットのデータセットが対象。window / tab verb はチャットのデータセットが開いていなければエラー（前面へは倒れない）。CLI の `state <name>` 等はファイルを直接扱うので、閉じていても読み書きする（`--dataset` を明示したときと同じ）。`state`（名前なし）と `list-analyses` は従来どおり。チャットにデータセットが無ければ従来どおり各 verb の既存の規則（「解析ファイルはデータセット側に置く」節の段落と同じ）。
- **メモリ天井（既知の制約・v1）**: 各解析タブは開いた時点で `mod.load()` を eager 実行し、開いている限り DataFrame を常駐させる。複数データセットを同時に開くと全データセットの全解析の DataFrame が同時常駐するため、大きな測定データを多数開くとメモリを圧迫し得る（遅延ロード/アンロードは将来課題）。

## ホットリロード — `devtools/`

走行中の GUI に修正コードを注入し、開いていたタブ・プロット・チャット文脈を破壊せず新コードを有効化する。トリガーは**手動のみ**（CLI verb + 開発(&D) メニュー、自動 file-watch なし）。リロード実行時は作業静止が前提（モーダル表示中は `reload-busy:...` で拒否。`scope=tab` 以外はチャット応答中も拒否）。

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

No build system or package layout (no pyproject.toml; loose-directory layout); dependencies are listed in `requirements.txt` (runtime) and `requirements-dev.txt` (tests). Tests live under `tests/` as pytest modules — run `python -m pytest tests/` (GUI tests self-set `QT_QPA_PLATFORM=offscreen`). When introducing a build system / packaging, update this file with the resulting commands.
