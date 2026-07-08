"""Shared types for LLM backends.

Kept separate from llm_backend.__init__ so each backend module can
`from llm_backend.base import ...` without import cycles through the
registry that lives in __init__.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol


TOOL_CALL_MARKER = "🔧"
TOOL_RESULT_MARKER = "↳"
TOOL_ERROR_MARKER = "✗"
TOOL_RESULT_INDENT = "   "   # 結果行の 3 スペース字下げ


# Shared across ALL backend system prompts (claude / pi / gui-chat). This
# project's contract is that data AND work live together under the dataset
# directory (synced), so an analysis resumes identically on any PC. The agent
# must never stash memory/notes/state on the local machine (e.g. the CC engine's
# ~/.claude memory). Appended to each backend's prompt so the rule is present
# regardless of which backend is selected. See tests/test_backend_prompts.py.
NO_LOCAL_PERSISTENCE = (
    "Persistence: this project keeps ALL work under the dataset directory "
    "(synced across machines) so an analysis resumes identically on any PC. "
    "Never write memory, notes, progress/TODO, or scratch files to the local "
    "machine — not ~/.claude, ~/.myanalysis, your home dir, the working dir, or "
    "any path outside a dataset — and do not use any feature that persists "
    "memory to local disk. Save figures/code only with save_fig / save_code "
    "(they write to the dataset's work_dir); write nothing outside a dataset dir."
)


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: dict  # parsed JSON


_VALID_ROLES = frozenset({"system", "user", "assistant", "tool"})


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str | None = None
    tool_calls: list | None = None
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if self.role not in _VALID_ROLES:
            raise ValueError(
                f"invalid role {self.role!r}, expected one of {sorted(_VALID_ROLES)}"
            )

    def to_payload(self) -> dict:
        d: dict = {"role": self.role}
        if self.role == "tool":
            d["tool_call_id"] = self.tool_call_id
            d["content"] = self.content or ""
        elif self.role == "assistant" and self.tool_calls:
            d["content"] = self.content or ""
            d["tool_calls"] = self.tool_calls
        else:
            d["content"] = self.content or ""
        return d


class LLMBackend(Protocol):
    name: str
    model: str

    def stream(
        self, messages: list[Message], tools: list | None = None
    ) -> Iterator[TextDelta | ToolCallRequest]:
        """Yield TextDelta / ToolCallRequest events. Raises on transport error."""
        ...
