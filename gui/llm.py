"""LLM backend abstraction.

Initial implementation: OpenAI-compatible /v1/chat/completions client with
SSE streaming. To add a backend: implement LLMBackend and extend get_backend().
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError

_log = logging.getLogger(__name__)

_VALID_ROLES = frozenset({"system", "user", "assistant"})


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str

    def __post_init__(self) -> None:
        if self.role not in _VALID_ROLES:
            raise ValueError(
                f"invalid role {self.role!r}, expected one of {sorted(_VALID_ROLES)}"
            )


class LLMBackend(Protocol):
    name: str
    model: str

    def stream(self, messages: list[Message]) -> Iterator[str]:
        """Yield response text chunks. Raises on transport error."""
        ...


class OpenAICompatBackend:
    def __init__(self, *, base_url: str, api_key: str, model: str,
                 name: str = "openai-compat"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.name = name

    def stream(self, messages: list[Message]) -> Iterator[str]:
        payload = json.dumps({
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )
        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body).get("error", {}).get("message", body)
            except (json.JSONDecodeError, AttributeError):
                detail = body
            raise RuntimeError(f"HTTP {e.code}: {detail}") from e
        with resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    _log.debug("SSE: ignoring malformed JSON: %r", data)
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta", {}).get("content")
                if delta:
                    yield delta


def get_backend() -> LLMBackend:
    """Construct the configured backend from environment variables."""
    return OpenAICompatBackend(
        base_url=os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1",
        api_key=os.environ.get("OPENAI_API_KEY") or "not-needed",
        model=os.environ.get("OPENAI_MODEL") or "gpt-4o-mini",
    )
