"""Pane slot paths for nested splits (Issue #97) — Qt-free.

A slot is a ``/``-separated list of sides. ``left``/``right`` split that level
horizontally, ``top``/``bottom`` vertically; ``left`` ≡ ``top`` (1st child) and
``right`` ≡ ``bottom`` (2nd child). Parsed into steps ``(axis, idx)`` with axis
``"h"``/``"v"`` and idx ``0``/``1``.

Shared by gui/tab.py (split tree) and llm_bridge/session.py (restore-time
validation), so this module must NOT import PySide6.
"""
from __future__ import annotations

MAX_DEPTH = 6

Step = tuple[str, int]  # (axis "h"|"v", idx 0|1)

_TOKENS: dict[str, Step] = {
    "left": ("h", 0),
    "right": ("h", 1),
    "top": ("v", 0),
    "bottom": ("v", 1),
}
_NAMES: dict[Step, str] = {v: k for k, v in _TOKENS.items()}

GRAMMAR = (
    "grammar: '/'-separated sides, each left|right (horizontal split) or "
    f"top|bottom (vertical split), at most {MAX_DEPTH} levels, "
    "e.g. 'top/left'"
)


def parse_slot(slot: str | None) -> list[Step]:
    """Parse a slot path. ``None``/``""`` (after strip) → ``[]`` (= omitted).

    Invalid input raises ``ValueError("invalid slot: ... (grammar: ...)")``.
    """
    if slot is None:
        return []
    if not isinstance(slot, str):
        raise ValueError(f"invalid slot: {slot!r} ({GRAMMAR})")
    s = slot.strip()
    if not s:
        return []
    tokens = s.split("/")
    if len(tokens) > MAX_DEPTH:
        raise ValueError(
            f"invalid slot: {slot!r} (depth {len(tokens)} > {MAX_DEPTH}; {GRAMMAR})"
        )
    steps: list[Step] = []
    for tok in tokens:
        t = tok.strip().lower()
        if t not in _TOKENS:
            raise ValueError(f"invalid slot: {slot!r} (bad side {tok!r}; {GRAMMAR})")
        steps.append(_TOKENS[t])
    return steps


def format_slot(steps: list[Step]) -> str:
    """Inverse of parse_slot: ``[]`` → ``""``; h→left/right, v→top/bottom."""
    return "/".join(_NAMES[(axis, idx)] for axis, idx in steps)


def conflicts(a: list[Step], b: list[Step]) -> bool:
    """True when two slot paths cannot both hold a pane.

    Walking the common prefix: an axis mismatch at the same node conflicts; a
    different child index means the paths diverge (unrelated → no conflict).
    Equal paths or a prefix relation conflict.
    """
    for i in range(min(len(a), len(b))):
        if a[i][0] != b[i][0]:
            return True
        if a[i][1] != b[i][1]:
            return False
    return True
