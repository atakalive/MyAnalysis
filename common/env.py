"""Minimal .env file loader. No external dependencies."""
from __future__ import annotations

import os
from pathlib import Path

from common.paths import repo_root


def load_env(path: Path | None = None) -> None:
    """Load KEY=VALUE pairs from *path* into ``os.environ``.

    Existing environment variables are **not** overwritten (``setdefault``).
    If *path* does not exist or is unreadable the call is a silent no-op.
    """
    if path is None:
        path = repo_root() / ".env"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().removeprefix("export").lstrip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        os.environ.setdefault(key, val)
