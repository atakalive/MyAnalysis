# MyAnalysis

Experimental measurement-data analysis project (Python). It provides a **PySide6 GUI** + an **LLM chat dock** + a **hot-reload** development environment. Measurement data lives **outside the repository** (on a synced drive); only code is versioned here.

> This file is an overview for users and newcomers. For the detailed conventions aimed at developers and LLM agents, see [CLAUDE.md](CLAUDE.md). A Japanese version of this README is available at [README_ja.md](README_ja.md).

---

## How MyAnalysis works (concept)

MyAnalysis is built around one idea: **you analyze measurement data by telling an AI agent in the chat dock what you want to know — in plain language — and it does the analysis and drives the GUI for you.** You are not expected to hand-write plotting code or memorize commands; you direct the work at the level of *questions and goals*, and the agent handles the mechanics (finding the data's real layout, loading it, plotting, saving, laying out tabs).

**Design principles**

1. **Everything lives with the data, on the synced drive.** Only the application code is in this repository. Each dataset is self-contained — its settings (`myanalysis.toml`), its analysis code (`analyses/<name>/`), and its output (`_work/`) all sit under the dataset directory on the synced drive. The point: *the same analysis resumes identically on any PC.*
2. **The chat agent is a data analyst, not an app developer.** Its job is to analyze the registered datasets and operate the GUI — never to modify MyAnalysis itself.
3. **Inspect first.** No data layout is assumed. The agent looks at a dataset's real structure before loading it, so you can point it at a brand-new dataset and it works out the shape.
4. **No local persistence.** Figures, code, and state are written only to the dataset's `work_dir` on the synced drive; nothing is stashed on the local machine. This is what makes "resume on any PC" hold.

**How you instruct it** — say what you want in ordinary language. The agent decides where to load from, where to save (the `work_dir` is automatic — you never specify it), how to label things, and which GUI operations to run. For example:

- "Open `<Dataset Name>` and explain the overview."
- "Visualize the relationship between `<quantity A>` and `<quantity B>`."
- "Display the results of the current `<Tab A>` and `<Tab B>` side by side in a vertical panel split."
- "Apply an equivalent analysis to `<Dataset A>` and `<Dataset B>` and compare them."
- "Summarize the overall conclusion as a single presentation slide."
- "Explain exactly what this analysis did."

Under the hood the agent analyzes with Python (`common/explore.py`) and drives the GUI with `python -m llm_bridge` verbs — but you don't have to think about that. For the exact agent contract, see the chat system prompt in [llm_backend/claude_code.py](llm_backend/claude_code.py); for conventions, [CLAUDE.md](CLAUDE.md).

---

## Requirements / Setup

- **Python 3.11 or newer is required** (the code uses `tomllib` from the standard library; 3.10 and earlier will not work).
- There is **no dependency manifest** yet (`requirements.txt` / `pyproject.toml`). For now, install the dependencies inferred from imports manually.

**Core dependencies** (needed to launch the GUI):

```
PySide6  pyqtgraph  numpy  pandas  matplotlib
```

**Optional dependencies** (only for specific analyses):

- `h5py` — needed only for analyses that handle `.h5` camera images (e.g. `dataset_d`). Imported lazily inside `load()`.
- `tifffile` + `Pillow` — needed only for the ImageJ-style image viewer (Issue #60, `show_image`). `tifffile` opens 16bit / multi-page / N-dimensional TIFF; `Pillow` opens PNG/JPG/BMP. Both are imported lazily inside `common/image_io.py`; without them TIFF / raster loading raises a graceful `ImportError` pointing at `pip install`.

**Config files** (copy each `.example` to activate; the real files are gitignored):

| Copy from | Copy to | Purpose |
|---|---|---|
| [.env.example](.env.example) | `.env` | LLM connection & secrets (`OPENAI_BASE_URL` / `OPENAI_API_KEY` / `PI_API_KEY` / `LLM_BACKEND`, etc.) |
| [llm_backend/config.example.toml](llm_backend/config.example.toml) | `llm_backend/config.toml` | Backend selection and operational settings |
| [models.example.toml](models.example.toml) | `models.toml` | Model settings (model / thinking / effort / provider) |

The GUI itself starts even without these files — the chat simply has no configured backend.

---

## Launching

```bash
# Windows: double-click also works. Activates .venv if present, then launches the
# GUI windowless via pythonw (no lingering console). Use `python tool.py` to see errors.
run.bat

# Direct (keeps a console — use this to see startup errors / tracebacks)
python tool.py
python tool.py --demo            # Adds a synthetic demo tab exercising every panel type
python tool.py --resume-session  # Restore window/tabs/chat from the reload manifest (mainly for hot-reload)
```

The GUI entry point is [tool.py](tool.py) (PySide6).

When launched windowless (`run.bat` / `pythonw`), uncaught exceptions are written as tracebacks to `data/logs/gui-crash-*.log` (a crash at the same site is recorded only on its first occurrence even if the message changes; at most 200 files per process; intentional exits are not recorded). Startup import errors etc. are visible only with the console-attached `python tool.py`.

---

## Directory layout

| Path | Role |
|---|---|
| [tool.py](tool.py) | GUI entry point (`QApplication` + `ToolWindow`) |
| [run.bat](run.bat) | Windows launcher (activate `.venv` → windowless `pythonw tool.py`) |
| [config.py](config.py) | Dataset registry: `DATASETS` (dataset name → {hostname: full path}) |
| [dataset_config.py](dataset_config.py) | Reads/writes per-dataset settings (`myanalysis.toml`) |
| [common/](common/) | Shared utilities (`explore.py`, `loaders.py`, `paths.py`, `filelock.py`, `env.py`) |
| [core/](core/) | Low-level modules (`figures.py` = matplotlib helpers; forces the Agg backend on import) |
| [gui/](gui/) | PySide6 GUI (`window.py`, `tab.py`, `chat.py`, `panels.py`, `tools.py`) |
| [llm_backend/](llm_backend/) | LLM backend abstraction (claude / openai / pi / codex / mock) |
| [llm_bridge/](llm_bridge/) | GUI↔CLI bridge (over the filesystem, cross-platform) |
| [devtools/](devtools/) | Hot reload (`hotreload.py`, `qt_integration.py`) |
| [meeting/](meeting/) | Meeting-share relay (local in-memory relay + cloudflared tunnel) |
| [relay-worker/](relay-worker/) | Meeting-share guest page (`chatdock.html`, published to GitLab Pages) + its setup README |
| [config_share.py](config_share.py) | Optional Cloudflare R2 sync for `config.py` (push / pull / sync) |
| [i18n/](i18n/) | UI text catalogs (`en.toml` / `ja.toml`) |
| [newanalysis/](newanalysis/) | Analysis-module scaffold generator |
| [export/](export/) | Headless PNG export driver |
| [tests/](tests/) | pytest test suite |
| `data/` | Gitignored local scratch space (outputs, caches). Never commit its contents |

Analysis modules are **not** in the repository — each analysis lives with its data on the synced drive at `<dataset_dir>/analyses/<name>/analysis.py` (see "Adding an analysis & exporting" below).

---

## Data access

Measurement data lives outside the repository on a synced drive, and **the mount-point drive letter differs per PC**. To absorb that difference, `DATASETS` in [config.py](config.py) maps each dataset to "hostname → full path on that PC":

```python
DATASETS = {
    "dataset_a": {
        "HOST_A": r"G:\同期\測定\000000\example",
        "HOST_B": r"H:\同期\測定\000000\example",
    },
    ...
}
```

**Never hardcode absolute paths like `G:\...` in analysis code.** Always go through:

```python
from config import get_dataset_dir
path = get_dataset_dir("dataset_a")  # resolved by current hostname
```

Unknown dataset names and unregistered hosts raise descriptive errors pointing at `config.py`. When using a new PC, add an entry for that hostname (uppercase) to each dataset you'll use.

### Registering a dataset

```bash
# CLI (rewrites config.py atomically via AST; auto-opens if the GUI is running)
python -m llm_bridge register-dataset <name> <path> [--host H] [--no-open]

# GUI: File → データセットを新規登録 (New dataset)
```

### Session folder naming

Inside a dataset directory, sessions are folders named `session_<yyyymmdd>_<hhmmss>_<id>`.

### Per-dataset settings — `myanalysis.toml`

Settings specific to one dataset live not in `config.py` but in a **`myanalysis.toml` at the top of that dataset's directory** (managed by [dataset_config.py](dataset_config.py)), so they travel with the data on the synced drive.

- `work_dir` (default `_work`) — where analysis output is saved. Relative paths resolve under the dataset directory.
- `format` (default `csv_per_subdir`) — load format: `csv_per_subdir` or `custom`.

Measurement files (CSV etc.) are never modified. `myanalysis.toml` is written mechanically at save time and is safe to hand-edit.

---

## Exploratory analysis

For LLM agents and interactive exploration, [common/explore.py](common/explore.py) offers a minimal surface:

```python
from common.explore import load_dataset, save_fig, save_code, save_text, dataset_summary

summary = dataset_summary("my_dataset")   # INSPECT first: real subdirs + sample CSV columns/rows
# No default pattern is assumed — use what you saw above to load:
sessions = load_dataset("my_dataset", subdir_pattern="<real_folder_*>", csv_name="<real>.csv")
# → list[dict]: each {"name": str, "dir": Path, "df": DataFrame}

save_fig("my_dataset", fig, "overview")    # → <work_dir>/figures/overview.png (fig is closed after save)
save_code("my_dataset", "helper", code)    # → <work_dir>/code/helper.py   (.py only; no extension in the label)
save_text("my_dataset", "reports/summary.md", md)   # → <work_dir>/reports/summary.md (any extension, subdirs OK)
```

`save_text` is the general text writer — notes, reports, derived CSV/JSON. The relative path is validated (no `..`, no absolute/drive-relative/UNC paths, no Windows forbidden characters or reserved device names — `nul.txt` included) and re-checked to stay under `work_dir`; `session.json`, `chat_sessions/` and any `*.bak` are refused because they are live GUI state. `save_fig` / `save_code` labels must not contain a dot — the extension is added for you. All three return the absolute `Path`.

The lower-level loader is `load_csv_per_subdir` in [common/loaders.py](common/loaders.py) (globs subdirectories and `pd.read_csv`). Output goes to the dataset's `work_dir`.

---

## Adding an analysis & exporting

Each analysis is a single `<dataset_dir>/analyses/<name>/analysis.py` module that lives on the synced drive beside its data (not in the repo). Standard pattern:

```python
NAME = "my_analysis"      # module identifier
DATASET = "my_dataset"    # dataset key it uses

def load() -> dict:                         # load data (no GUI side effects)
    ...
def build_export_figs(data) -> dict:        # headless PNG: {filename: Figure}
    ...
def build_tab(parent, data) -> AnalysisTab: # build the GUI tab
    ...
```

Existing examples: `dataset_d` (.h5 camera images), `analysis_c` (scaffold), `example_analysis`, `dataset_b`.

### Scaffold generation

```bash
python -m newanalysis <name> --dataset <key>
```

Creates `<dataset_dir>/analyses/<name>/` with an `analysis.py` (standard pattern) + `README.md`. `--dataset` is required (the scaffold writes into that dataset's directory).

### Headless PNG export

```bash
python -m export <dataset> <name>
```

Runs `build_export_figs()` on the Agg backend and writes PNGs to `<work_dir>/analyses/<name>/batch/` (no GUI required).

---

## GUI overview

- **Multi-tab** — reorder analysis tabs by drag & drop.
- **LLM chat dock** (right side, floatable) — multiple session tabs, Ctrl+Enter to send, streaming output, tool calls (tab control, snapshots, etc.), font zoom.
- **Menus** — File / View / Settings / Help / Develop(&D).
- **Session save/restore** — per-dataset tab layout and chat are saved/restored under `work_dir` (File → Save session / on-exit dialog). For the saved-file breakdown, see [CLAUDE.md](CLAUDE.md) / [llm_bridge/session.py](llm_bridge/session.py).

---

## LLM chat & backends

The chat dock talks to an LLM backend through [llm_backend/](llm_backend/). Six engines exist, but **in practice MyAnalysis runs on Claude (VS Code bundled engine)**. The config values (env `LLM_BACKEND` / `[backend].name` in `config.toml`) take a **backend name** (second column); the two Claude routes are told apart by whether `[claude_code].bin` is empty (empty = auto-detect the bundled binary, non-empty = the CLI at that path):

| Engine (GUI label) | `[backend].name` | Description |
|---|---|---|
| Claude (VS Code bundled engine) | `claude` (empty `bin`) | **Default in practice.** Reuses the VS Code Claude Code extension's bundled engine (stream-json mode) and your VS Code login |
| Claude (CLI on PATH) | `claude` (`bin` set) | Alternate route through a `claude` CLI on PATH |
| pi coding agent | `pi` | pi-coding-agent subprocess |
| Codex CLI (OpenAI) | `codex` | OpenAI Codex CLI subprocess |
| OpenAI-compatible HTTP | `openai` | OpenAI-compatible HTTP (including local endpoints like Ollama) |
| Mock (smoke test) | `mock` | Offline smoke test (no LLM) |

### Setting up the `claude` backend (recommended)

1. Install the **VS Code Claude Code extension and sign in** — the backend reuses its bundled engine and `~/.claude` login, so there is no API key to put in `.env`.
2. In `llm_backend/config.toml`, set `[backend].name = "claude"` (leave `bin` empty to auto-detect the bundled binary; `permission_mode = "bypassPermissions"` is the default and is what lets the agent drive the GUI unattended).
3. In `models.toml`, set the `[claude_code]` knobs — e.g. `model = "claude-opus-4-8"`, `thinking = "enabled"`, `effort = "xhigh"`.
4. Apply — if you edited the files by hand, **restart the app** (they are read once at startup). Changes made through **Settings → Backend / model settings…** need no restart: applying reseeds every chat session from the next send onward.

> The shipped `llm_backend/config.example.toml` defaults `[backend].name` to `pi` (a development placeholder); change it to `claude` for normal use.

### Other backends (optional)

- `openai` — point `.env`'s `OPENAI_BASE_URL` / `OPENAI_MODEL` / `OPENAI_API_KEY` at any OpenAI-compatible endpoint (e.g. a local Ollama).
- `pi` — `npm i -g @earendil-works/pi-coding-agent` (the old `@mariozechner/…` name is deprecated); operational settings go in `config.toml`, secrets in `.env`. On Windows it must be the Windows-side install — a WSL-only `pi` is not reachable.
- `codex` — `npm i -g @openai/codex`; log in with `codex login`. Model knobs live in `models.toml`'s `[codex]`.
- `mock` — offline, no configuration; useful for smoke-testing the GUI.

### Backend status window

**Settings → Backend status…** lists the install state of every supported driver (an inventory independent of which engine is currently selected). The window is non-modal, so you can keep using the app while an npm install runs. The CLI equivalent is `python -m llm_bridge engines`.

- **Display** — one row per engine; columns are Driver / Prereq (node+npm) / Installed (version) / Auth (logged-in providers). Marks: ✓ = OK, ✗ = missing, ? = could not be determined, — = not applicable. `?` does not mean "not installed" — a probe that merely timed out is deliberately not answered with a re-install offer.
- **Install / Update** — both run the same `npm i -g <package>`; the button just reads "Install" on missing rows and "Update" on installed ones. No button appears when the prereq is missing or the state is `?`. Claude (VS Code bundled) has no button — updating the extension is its delivery channel.
- **Log in** — shown even when already authenticated (account switching / re-login). On Windows it opens a new console for the interactive login; on other OSes it only prints the command to the log pane for you to paste into a terminal.
- **Connectivity check** — actually sends one turn to the backend and is the **only billed action** in this window (fires only on click). Opening the window and pressing "Re-check" run local probes only and never bill. There is deliberately no auto-refresh timer. The CLI `engines` has no connectivity check, so it is entirely free.
- **Safety** — probing only uses read-only commands that leave auth files untouched (`--version` / `codex login status` / `pi --list-models`); checking the status can never log out another PC or CLI.
- **Note** — if a row stays ✗ after an install finishes, restart the GUI (PATH is a snapshot from launch, so an npm global bin added to PATH afterwards is not seen).

The **Backend / model settings dialog** (Settings → Backend / model settings…) is where you pick the engine / model / provider and apply it globally. The model/provider candidate lists are editable via the ＋/− next to each combo (persisted to `models.toml`), and an optional connectivity check runs before applying. Right-click a chat tab → **Model for this chat…** to switch just that session to a different engine (sessions without an override follow the global setting).

**Selection order**: env `LLM_BACKEND` → `[backend].name` in `llm_backend/config.toml` → `OPENAI_BASE_URL` back-compat.

**Where settings live**:
- `.env` — connection & secrets. Note: real shell environment variables override `.env`, and inline `# comments` are **not** supported there (`KEY=value # x` sets the value to `value # x`).
- `llm_backend/config.toml` — backend selection and operational settings (bin / cwd / tools, etc.).
- `models.toml` — model knobs (`model` / `thinking` / `effort` / `provider`).

`config.toml` and `models.toml` are read at startup — hand edits need a restart; changes made through the settings dialog apply immediately.

---

## llm_bridge — GUI↔CLI bridge

Connects the GUI and CLI over the filesystem so a running GUI can be driven externally (Windows / POSIX; some verbs run without PySide6).

```bash
python -m llm_bridge <verb> ...
```

Key verbs:

| verb | Purpose |
|---|---|
| `list-datasets [--json]` | List registered datasets |
| `register-dataset <name> <path> ...` | Register a dataset (optionally scaffold + auto-open) |
| `list-analyses [--dataset <ds>] [--json]` | List analyses across all open datasets (`{dataset: [names]}` with `--json`) |
| `list-open-datasets` | Datasets open in the window + the active one |
| `active` | Active tab, active dataset, and `open_datasets` |
| `state [name]` | An analysis's `state.json` (active tab if omitted) |
| `window <verb> [k=v] [--wait]` | Window ops (`open-dataset` / `add-tab` / `set-active-dataset` / `close-dataset` / `reload`, etc.) |
| `tab <target> <verb> [k=v]` | Tab ops (`set-split` / `snapshot` / `refresh-state`, etc.) |
| `annotate` / `clear-annotations` | Add/clear annotations (marker / note) |
| `meeting-start [lan=true]` / `meeting-lan-link` | Start a meeting share / get the in-facility LAN link (see "Meeting share (hosting)") |

### Multiple datasets

Several datasets can be open in one process; the top-level dataset switcher swaps
between each dataset's tabs and chat sessions. `open-dataset` adds a dataset to the
workspace (others stay open); `set-active-dataset name=<ds>` (alias `switch-dataset`)
brings one to the front; `close-dataset name=<ds>` flushes its layout to
`session.json` then drops it. When the same tab name exists in two open datasets,
pass `dataset=<ds>` to any tab-addressing verb (`add-tab` / `show` / `set-active-tab`
/ `close-tab` / `tab <name> <verb>`) to disambiguate.

**Workspace restore.** File → "Restore last session" re-opens the datasets that were
open together (recorded in the gitignored `data/llm_state/last_window.json`); restore
is additive (it never closes already-open datasets). No automatic restore at startup.

**Meeting share** broadcasts every open dataset's tabs and chat sessions by default;
making an item private is an explicit opt-out (Issue #78). Same-named tabs in
different datasets stay distinct — the wire carries a per-dataset tab namespace. The
guest page mirrors the host's DS-above-tabs structure: the DS bar lists every shared
dataset as a clickable chip and each guest navigates on their own (`curDs` is
guest-local; the host's active dataset only seeds the default on first load). A
**Follow host** toggle sits at the right end of that bar — default OFF and not
persisted; switching it ON snaps the guest to the host's active dataset and keeps it
in sync, and clicking any dataset chip turns it back OFF (Issue #80). Switching
datasets stashes the unsent message draft per dataset, so a host-driven switch never
re-targets half-typed text at another dataset's chat. Guests also get their own "new
chat session" button and their chat send-key follows the host's (Issues #81, #82), and
an optional in-facility LAN direct link can be emitted alongside the public one for
guests whose network can't resolve the tunnel host (Issue #85). The relay itself uses
only the stdlib — see the [Meeting share (hosting)](#meeting-share-hosting) section and
[relay-worker/README.md](relay-worker/README.md) for how to set it up.

**Memory note (v1):** each open analysis tab eager-loads its DataFrame and keeps it
resident while open, so opening many large datasets at once can pressure memory
(lazy load/unload is a future item).

Examples:

```bash
python -m llm_bridge list-datasets --json
python -m llm_bridge window open-dataset name=my_dataset --wait 30
python -m llm_bridge window set-active-dataset name=other_dataset --wait
python -m llm_bridge tab summary snapshot dataset=other_dataset --wait
```

---

## Meeting share (hosting)

Meeting share (View → ミーティング共有…) live-shares your chat dock and analysis view to remote guests in their browser — by default every open dataset's tabs and chat are broadcast (Issue #78; making an item private is an explicit opt-out). Guests join by opening a link; nothing is persisted server-side.

**Architecture.** A local in-memory relay (bound to `127.0.0.1` by default) plus a single public tunnel — a **cloudflared named tunnel**. It costs $0 and uses no external key/value store. The full walkthrough and source of truth is [relay-worker/README.md](relay-worker/README.md); the `.env` variables are documented in [.env.example](.env.example).

**What you need to host a share** — all in `.env`, which is read once at startup (restart the GUI after editing):

1. `RELAY_ADMIN_KEY` — **required**, a non-empty secret (generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`). If it is empty, the share feature is simply disabled and nothing else is affected.
2. For the public (external) link, cloudflared needs `CLOUDFLARE_TUNNEL_NAME` and `CLOUDFLARE_TUNNEL_HOSTNAME`, plus a one-time cloudflared setup (delegate a domain to Cloudflare, then `cloudflared tunnel login` / `create` / `route dns`). See [relay-worker/README.md](relay-worker/README.md).

**In-facility LAN direct link (Issue #85, optional).** For guests on a venue network whose DNS can't resolve the tunnel hostname, you can emit a *second* link that connects them straight to the host PC over the LAN. Turn on "LAN リンクも出す" in the share dialog (default OFF), or run `python -m llm_bridge meeting-start lan=true` (plus `meeting-lan-link`); related env: `RELAY_LAN_HOST` / `RELAY_LAN_PORT`. The external (tunnel, https) and in-facility (LAN, http) links are active at the same time, so internal and external guests can join one meeting. The LAN link is distributed as a full copy-link only (it is http, so a bare token pasted onto an https page would be blocked as mixed content), and the relay binds `0.0.0.0` only while a share is live — see the security notes in [relay-worker/README.md](relay-worker/README.md).

---

## Hot reload

Injects edited code into the running GUI and activates it while keeping open tabs, plots, and chat context intact ([devtools/](devtools/)). **Manual trigger only** (CLI verb + Develop(&D) menu; no automatic file watching).

```bash
python -m llm_bridge window reload [scope=app] --wait
```

Four-tier escalation:

| scope | Target | Mechanism |
|---|---|---|
| `patch` (default) | repo modules in sys.modules | in-place patch (display state untouched) |
| `tab` | `<dataset_dir>/analyses/<name>/analysis.py` | rebuild a single tab in a sandbox → swap |
| `app` | structural changes (`__init__` / Signal / `__bases__`, etc.) | blue-green window rebuild |
| `restart` | tool.py itself, PySide6 upgrade | process restart + automatic session restore |

For details and known limitations, see [CLAUDE.md](CLAUDE.md).

---

## Tests

```bash
python -m pytest tests/
```

GUI tests set `QT_QPA_PLATFORM=offscreen` themselves, so they run headless.

---

## Development conventions (excerpt)

- **Commit directly to `main`** (do not create feature branches).
- `data/` is gitignored — local scratch space; never commit its contents.
- `.bat` / `.cmd` files use **CRLF line endings** (LF breaks `cmd.exe`; pinned via `.gitattributes`).
- The repository is a private GitLab repo (`git@gitlab.com:atakalive/MyAnalysis.git`).

For fuller conventions and per-subsystem design, see [CLAUDE.md](CLAUDE.md).
