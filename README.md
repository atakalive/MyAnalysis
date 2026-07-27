# MyAnalysis

Experimental measurement-data analysis project (Python). It provides a **PySide6 GUI** + an **LLM chat dock** + a **hot-reload** development environment. Measurement data lives **outside the repository** (on a synced drive); only code is versioned here.

> This file is an overview for users and newcomers. For the detailed conventions aimed at developers and LLM agents, see [CLAUDE.md](CLAUDE.md). A Japanese version of this README is available at [README_ja.md](README_ja.md).

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
| [llm_backend/](llm_backend/) | LLM backend abstraction (claude / openai / pi / mock) |
| [llm_bridge/](llm_bridge/) | GUI↔CLI bridge (over the filesystem, cross-platform) |
| [devtools/](devtools/) | Hot reload (`hotreload.py`, `qt_integration.py`) |
| [analyses/](analyses/) | Analysis modules (1 directory = 1 analysis, each with `analysis.py`) |
| [newanalysis/](newanalysis/) | Analysis-module scaffold generator |
| [export/](export/) | Headless PNG export driver |
| [tests/](tests/) | pytest test suite |
| `data/` | Gitignored local scratch space (outputs, caches). Never commit its contents |
| `docs/` | Documentation (currently mostly empty) |

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
from common.explore import load_dataset, save_fig, save_code, dataset_summary

summary = dataset_summary("my_dataset")   # INSPECT first: real subdirs + sample CSV columns/rows
# No default pattern is assumed — use what you saw above to load:
sessions = load_dataset("my_dataset", subdir_pattern="<real_folder_*>", csv_name="<real>.csv")
# → list[dict]: each {"name": str, "dir": Path, "df": DataFrame}

save_fig("my_dataset", fig, "overview")    # → <work_dir>/figures/overview.png (fig is closed after save)
save_code("my_dataset", "helper", code)    # → <work_dir>/code/helper.py
```

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
- **Menus** — File / View / Help / Develop(&D).
- **Session save/restore** — per-dataset tab layout and chat are saved/restored under `work_dir` (File → Save session / on-exit dialog). For the saved-file breakdown, see [CLAUDE.md](CLAUDE.md) / [llm_bridge/session.py](llm_bridge/session.py).

---

## LLM chat & backends

Chat-dock LLM access goes through [llm_backend/](llm_backend/). Four backends:

| Backend | Description |
|---|---|
| `claude` | Reuses the VS Code Claude Code extension's bundled engine (stream-json mode) |
| `openai` | OpenAI-compatible HTTP (including local endpoints like Ollama) |
| `pi` | pi-coding-agent subprocess |
| `mock` | Offline smoke test |

**Selection order**: env `LLM_BACKEND` → `[backend].name` in `llm_backend/config.toml` → `OPENAI_BASE_URL` back-compat.

**Where settings live**:
- `.env` — connection & secrets (API keys, etc.).
- `llm_backend/config.toml` — backend selection and operational settings (cwd / bin / tools, etc.).
- `models.toml` — model knobs (`model` / `thinking` / `effort` / `provider`).

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
re-targets half-typed text at another dataset's chat. The meeting relay itself uses
only the stdlib — see `relay-worker/` (local in-memory relay + public tunnel, $0)
for deployment details.

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
