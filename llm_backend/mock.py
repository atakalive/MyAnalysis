"""LLM-free mock backend for GUI smoke testing."""

from __future__ import annotations

import datetime
import random
import time
import urllib.request
from collections.abc import Iterator

from llm_backend.base import Message, TextDelta, ToolCallRequest


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

    def stream(
        self, messages: list[Message], tools: list | None = None
    ) -> Iterator[TextDelta | ToolCallRequest]:
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
            yield TextDelta(text=text[i : i + 3])
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
