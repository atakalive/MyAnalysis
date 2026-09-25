"""OpenAI-compatible /v1/chat/completions client with SSE streaming."""

from __future__ import annotations

import json
import logging
import urllib.request
from collections.abc import Iterator
from urllib.error import HTTPError

from llm_backend.base import Message, TextDelta, ToolCallRequest, compose_system_prompt

_log = logging.getLogger(__name__)


class OpenAICompatBackend:
    def __init__(
        self, *, base_url: str, api_key: str, model: str, name: str = "openai-compat"
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.name = name
        # ユーザー選択ペルソナ本文（"" = なし）。ChatWidget が duck-typed に注入する。
        self._persona = ""

    def set_persona(self, value: str) -> None:
        self._persona = str(value or "")

    def _payload_messages(self, messages: list[Message]) -> list[dict]:
        """送信用 payload の messages を組み立てる（ペルソナは送信時合成）。

        合成は最初の system メッセージの **payload dict のみ**に施す — 保存済み
        Message オブジェクトは変異させない（セッションは同期・永続で、mint 時の
        system を凍結したまま持つ。送信時注入なので旧セッションにも効く）。
        persona ありで system 不在なら合成 system を先頭挿入する。persona 空なら
        従来どおり素の to_payload 列（バイト同一）。getattr はホットリロード後の
        旧インスタンス対策。
        """
        payload = [m.to_payload() for m in messages]
        persona = (getattr(self, "_persona", "") or "").strip()
        if not persona:
            return payload
        for d in payload:
            if d.get("role") == "system":
                d["content"] = compose_system_prompt(d.get("content") or "", persona)
                return payload
        # system 不在: ペルソナ節だけの system を先頭に（base 空なので lstrip で整える）。
        payload.insert(
            0,
            {"role": "system",
             "content": compose_system_prompt("", persona).lstrip("\n")},
        )
        return payload

    def stream(
        self, messages: list[Message], tools: list | None = None
    ) -> Iterator[TextDelta | ToolCallRequest]:
        body: dict = {
            "model": self.model,
            "messages": self._payload_messages(messages),
            "stream": True,
        }
        if tools:
            body["tools"] = tools
        payload = json.dumps(body).encode("utf-8")
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
            err_body = e.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(err_body).get("error", {}).get("message", err_body)
            except (json.JSONDecodeError, AttributeError):
                detail = err_body
            raise RuntimeError(f"HTTP {e.code}: {detail}") from e
        tool_buffers: dict[int, dict] = {}
        with resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    _log.debug("SSE: ignoring malformed JSON: %r", data)
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                content = delta.get("content")
                if content:
                    yield TextDelta(text=content)
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    if idx not in tool_buffers:
                        tool_buffers[idx] = {
                            "id": tc.get("id", ""),
                            "name": tc.get("function", {}).get("name", ""),
                            "arguments_str": "",
                        }
                    buf = tool_buffers[idx]
                    if tc.get("id"):
                        buf["id"] = tc["id"]
                    fn = tc.get("function", {})
                    if fn.get("name"):
                        buf["name"] = fn["name"]
                    buf["arguments_str"] += fn.get("arguments", "")
        for buf in tool_buffers.values():
            raw_args = buf["arguments_str"]
            if not raw_args.strip():
                args = {}
            else:
                try:
                    parsed = json.loads(raw_args)
                except (json.JSONDecodeError, ValueError):
                    args = {
                        "__parse_error__": f"malformed JSON arguments: {raw_args!r}"
                    }
                else:
                    if isinstance(parsed, dict):
                        args = parsed
                    else:
                        args = {
                            "__parse_error__": f"expected JSON object, got {type(parsed).__name__}: {raw_args!r}"
                        }
            yield ToolCallRequest(id=buf["id"], name=buf["name"], arguments=args)
