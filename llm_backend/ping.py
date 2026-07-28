"""Connectivity check for a candidate backend (Qt-independent).

Runs a single minimal turn through a backend's ``stream()`` and reports whether it
produced output without raising. Used by the backend/model dialog to probe a
not-yet-applied configuration (built via ``build_backend(name, settings)``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from llm_backend.base import Message, TextDelta, ToolCallRequest

# Enough to prove liveness ("pong" is 4 chars; 8 tolerates a leading token). More
# just wastes drip-output wall-clock, billing, and outbound HTTP.
_MIN_CHARS = 8

_PROMPT = (
    "Connectivity check. Reply with the single word: pong. "
    "Do not use any tools."
)


@dataclass
class PingResult:
    ok: bool
    elapsed: float
    text: str = ""
    model: str = ""
    error: str = ""


def ping_backend(backend) -> PingResult:
    """Run one minimal turn; return a PingResult (never raises).

    ``ok=True`` means the turn completed without an exception. Breaks early once a
    reply is evident (≥ _MIN_CHARS accumulated, or a ToolCallRequest arrives, or the
    stream ends). Breaking is safe — each backend's stream generator cleans up its
    subprocess/HTTP on close (see claude_code/pi/openai finally blocks).
    """
    start = time.monotonic()
    text = ""
    try:
        gen = backend.stream(
            [Message(role="user", content=_PROMPT)], tools=None
        )
        for event in gen:
            if isinstance(event, ToolCallRequest):
                # A tool attempt still means "a reply came back" → success.
                text += " [tool call requested]"
                break
            if isinstance(event, TextDelta):
                text += event.text
                if len(text) >= _MIN_CHARS:
                    break
        gen.close()
    except Exception as e:  # binary-missing RuntimeError etc. surface in the worker
        return PingResult(
            ok=False,
            elapsed=time.monotonic() - start,
            error=repr(e),
        )
    return PingResult(
        ok=True,
        elapsed=time.monotonic() - start,
        text=text.strip(),
        model=getattr(backend, "model", "") or "",
    )
