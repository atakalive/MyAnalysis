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

## Repo conventions

- `data/` is gitignored — safe scratch space for local outputs, caches, exports. Don't commit anything inside.
- `.env` is gitignored. Used for LLM backend overrides (`OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_API_KEY`, `PI_API_KEY`, `LLM_BACKEND`). Copy `.env.example` to get started.
- Private repo on GitLab (`git@gitlab.com:atakalive/MyAnalysis.git`), so non-secret config like dataset paths is fine to commit.

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

## State

Greenfield as of 2026-05-27 — no build system, dependencies, tests, or package layout yet. When introducing those (pyproject.toml, requirements, test runner, src/ layout), update this file with the resulting commands.
