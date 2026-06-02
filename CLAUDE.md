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

When adding work for a new measurement, add an entry to `DATASETS` in [config.py](config.py). When running on a new PC, add that hostname (uppercase) to each dataset you'll use. Unknown host or dataset raises a descriptive error pointing at `config.py`.

Dataset directories contain session folders named `session_<yyyymmdd>_<hhmmss>_<id>`.

### Per-dataset settings — `myanalysis.toml`

Settings specific to one dataset live in `myanalysis.toml` at the top of that dataset's directory (not in `config.py`), so they sync with the data and follow it across PCs/repos. [dataset_config.py](dataset_config.py) reads/generates it. Today the only setting is `work_dir` — where analysis output is saved (default `_work`). The tools write this sidecar mechanically; it's safe to hand-edit. Measurement files (CSV etc.) are never modified.

## Repo conventions

- `data/` is gitignored — safe scratch space for local outputs, caches, exports. Don't commit anything inside.
- Exploratory analysis output (figures, code snippets, intermediates) goes to the dataset's `work_dir` (default `<dataset_dir>/_work`, configurable per dataset via `myanalysis.toml`). Created on first save by `common.explore.save_fig()` / `save_code()`.
- `.env` is gitignored. Used for LLM backend overrides (`OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_API_KEY`, `PI_API_KEY`, `LLM_BACKEND`). Copy `.env.example` to get started.
- Private repo on GitLab (`git@gitlab.com:atakalive/MyAnalysis.git`), so non-secret config like dataset paths is fine to commit.
- Windows `.bat`/`.cmd` files **must use CRLF line endings** — LF-only batch files break `cmd.exe` parsing (especially `if (...)` blocks) and fail to launch. `.gitattributes` pins `*.bat`/`*.cmd` to `eol=crlf`; keep that and don't let an editor save them as LF.

## LLM backends — `llm_backend/`

Chat-dock LLM access goes through the `llm_backend/` package. Backends implement
the `LLMBackend` Protocol in `llm_backend/base.py`; `get_backend()` selects one
by name. Three backends ship: `openai` (OpenAI-compatible HTTP), `mock` (offline
smoke test), `pi` (pi-coding-agent subprocess).

- **Selection order**: env `LLM_BACKEND` → `[backend].name` in
  `llm_backend/config.toml` → `OPENAI_BASE_URL` back-compat.
- **Config file**: copy `llm_backend/config.example.toml` → `llm_backend/config.toml`
  (gitignored). The `[pi]` section sets `cwd` (pi working directory), `bin`,
  `model`, `provider`, `tools`.
- **pi backend** runs `python -m llm_bridge` via the `.pi/skills/myanalysis-bridge`
  skill to drive the GUI live. **External dependency**: Node + pi
  (`npm i -g @mariozechner/pi-coding-agent`). On Windows pi additionally needs a
  bash shell (Git Bash) for its tool execution.

## llm_bridge — cross-platform

`llm_bridge` (GUI ↔ CLI over the filesystem) works on Windows and POSIX. File
locking is abstracted in `common/filelock.py` (`exclusive_lock`): `fcntl` on
POSIX, `msvcrt` on Windows. `python -m llm_bridge <verb>` runs without PySide6.

## Exploratory analysis — `common/explore.py`

LLM agents analyse data via code execution + CLI, not just GUI remote control.
`common/explore.py` provides a minimal surface: `load_dataset(name)` (config → loaders
in one call), `save_fig(name, fig, label)` (saves to `<work_dir>/figures/<label>.png`),
`save_code(name, label, content)` (saves to `<work_dir>/code/<label>.py`),
`dataset_summary(name)` (columns, dtypes, row counts per session). Output goes to the
dataset's `work_dir` (default `<dataset_dir>/_work`, set in `myanalysis.toml`); the
sidecar `myanalysis.toml` and `work_dir` are created on first save. Measurement files
(CSV etc.) are never modified — but a hand-set `work_dir` may place new output files
(PNG/PY) in any subdirectory of the dataset dir. Use `python -m llm_bridge list-datasets`
to discover registered dataset names.

## State

Greenfield as of 2026-05-27 — no build system, dependencies, tests, or package layout yet. When introducing those (pyproject.toml, requirements, test runner, src/ layout), update this file with the resulting commands.
