# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Experimental measurement data analysis project (Python). Data lives **outside the repo** on a synced drive; only code is versioned here.

## Data access — always go through `config.py`

Raw measurement data is not in the repo. It lives at a per-host root (e.g. `G:\同期\測定` on PC `HOST_A`). Two-layer indirection:

- `DATA_ROOTS[hostname]` → base directory (varies by PC, since the same cloud drive folder mounts at different paths on different machines)
- `DATASETS[name]` → relative path under that root (same on all PCs)

**Never hardcode `G:\...` or any absolute data path in analysis code.** Always:

```python
from config import get_dataset_dir
path = get_dataset_dir("dataset_a")
```

When adding work for a new measurement, add an entry to `DATASETS` in [config.py](config.py). When running on a new PC, add an entry to `DATA_ROOTS`. Unknown host or dataset raises a descriptive error pointing at `config.py`.

Dataset directories contain session folders named `session_<yyyymmdd>_<hhmmss>_<id>`.

## Repo conventions

- `data/` is gitignored — safe scratch space for local outputs, caches, exports. Don't commit anything inside.
- `.env` is gitignored. Currently empty; reserved for future secrets/local overrides.
- Private repo on GitLab (`git@gitlab.com:atakalive/MyAnalysis.git`), so non-secret config like dataset paths is fine to commit.

## State

Greenfield as of 2026-05-27 — no build system, dependencies, tests, or package layout yet. When introducing those (pyproject.toml, requirements, test runner, src/ layout), update this file with the resulting commands.
