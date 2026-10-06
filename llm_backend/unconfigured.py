"""エンジン未設定を表すプレースホルダ（Issue #115）。

LLM_BACKEND も [backend].name も OPENAI_BASE_URL も無いとき、または全体設定の
バックエンドを作れなかったとき、チャットは別のエンジン（OpenAI 互換・モック）へ倒さず
これを持つ。送信は ChatWidget._start_turn が断る。stream() も常に例外を送出するので、
そのガードを抜けても通信しない。

llm_backend.base 以外を import しない（llm_backend/__init__.py から import されるため）。
"""

from __future__ import annotations

from collections.abc import Iterator

from llm_backend.base import Message, TextDelta, ToolCallRequest


class NoEngineConfigured(RuntimeError):
    """AI エンジンが選ばれていない。"""

    def __init__(self) -> None:
        super().__init__(
            "no AI engine is selected; choose one in Settings → Backend / model "
            "settings, or set [backend].name in llm_backend/config.toml or LLM_BACKEND"
        )


class UnconfiguredBackend:
    """送れないバックエンド。_BACKENDS には登録しない（名前では選べない）。"""

    name = "unconfigured"
    model = ""

    def set_persona(self, value: str) -> None:
        """不活性。duck-typed 注入の対称性のため。"""

    def set_chat_dataset(self, value: object) -> None:
        """不活性。duck-typed 注入の対称性のため。"""

    def stream(
        self, messages: list[Message], tools: list | None = None
    ) -> Iterator[TextDelta | ToolCallRequest]:
        raise NoEngineConfigured()
