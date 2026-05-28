"""LLM backend abstraction.

Initial implementation: OpenAI-compatible /v1/chat/completions client with
SSE streaming. To add a backend: implement LLMBackend and extend get_backend().
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import random
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError

_log = logging.getLogger(__name__)


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

    def stream(self, messages: list[Message],
               tools: list | None = None) -> Iterator[TextDelta | ToolCallRequest]:
        """Yield TextDelta / ToolCallRequest events. Raises on transport error."""
        ...


class OpenAICompatBackend:
    def __init__(self, *, base_url: str, api_key: str, model: str,
                 name: str = "openai-compat"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.name = name

    def stream(self, messages: list[Message],
               tools: list | None = None) -> Iterator[TextDelta | ToolCallRequest]:
        body: dict = {
            "model": self.model,
            "messages": [m.to_payload() for m in messages],
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
            body = e.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body).get("error", {}).get("message", body)
            except (json.JSONDecodeError, AttributeError):
                detail = body
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
                    args = {"__parse_error__": f"malformed JSON arguments: {raw_args!r}"}
                else:
                    if isinstance(parsed, dict):
                        args = parsed
                    else:
                        args = {"__parse_error__": f"expected JSON object, got {type(parsed).__name__}: {raw_args!r}"}
            yield ToolCallRequest(id=buf["id"], name=buf["name"], arguments=args)


class MockBackend:
    """テスト用バックエンド。時刻・天気・占い・原油価格を返す。

    OPENAI_BASE_URL=mock で有効化。LLM 実機なしで GUI チャットの
    ストリーミング/スレッド/終了処理を確認するためのもの。
    原油価格のみ stooq.com から実取得(WTI先物連続)、その他はモック値。
    """
    name = "mock"

    _WEATHER = ["晴れ", "曇り", "雨", "雪", "快晴", "雷雨"]
    _FORTUNE = ["大吉", "中吉", "小吉", "吉", "末吉", "凶"]
    _CRUDE_URL = "https://stooq.com/q/l/?s=cl.f&f=sd2t2c&h&e=csv"

    def __init__(self, *, model: str):
        self.model = model

    def stream(self, messages: list[Message],
               tools: list | None = None) -> Iterator[TextDelta | ToolCallRequest]:
        today = datetime.date.today()
        now = datetime.datetime.now()
        rng = random.Random(today.toordinal())
        weather = rng.choice(self._WEATHER)
        fortune = rng.choice(self._FORTUNE)
        crude = self._fetch_crude_price()

        text = (
            f"現在時刻: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"今日の天気: {weather}\n"
            f"今日の占い: {fortune}\n"
            f"原油価格(WTI先物): {crude}\n"
        )
        for i in range(0, len(text), 3):
            yield TextDelta(text=text[i:i + 3])
            time.sleep(0.03)

    @classmethod
    def _fetch_crude_price(cls) -> str:
        try:
            req = urllib.request.Request(
                cls._CRUDE_URL,
                headers={"User-Agent": "Mozilla/5.0 (MyAnalysis mock backend)"},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            return f"取得失敗 ({e!r})"
        lines = body.strip().splitlines()
        if len(lines) < 2:
            return "取得失敗 (空レスポンス)"
        cols = lines[1].split(",")
        if len(cols) < 4:
            return f"取得失敗 (想定外フォーマット: {lines[1]!r})"
        _sym, date, _time, close = cols[0], cols[1], cols[2], cols[3]
        close = close.strip()
        if close in ("", "N/D", "-"):
            return f"取得失敗 (値なし, {date})"
        return f"{close} USD/bbl (WTI連続先物, {date})"


def get_backend() -> LLMBackend:
    """Construct the configured backend from environment variables."""
    base_url = os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    if base_url.strip().lower() == "mock":
        return MockBackend(model=os.environ.get("OPENAI_MODEL") or "mock-omni")
    return OpenAICompatBackend(
        base_url=base_url,
        api_key=os.environ.get("OPENAI_API_KEY") or "not-needed",
        model=os.environ.get("OPENAI_MODEL") or "gpt-4o-mini",
    )
