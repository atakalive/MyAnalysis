"""Tests for llm_backend.ping.ping_backend."""
from llm_backend.base import TextDelta, ToolCallRequest
from llm_backend.ping import ping_backend


class _FakeBackend:
    name = "fake"

    def __init__(self, events, model="resolved-model"):
        self._events = events
        self.model = model
        self.finally_hit = False

    def stream(self, messages, tools=None):
        try:
            for e in self._events:
                yield e
        finally:
            self.finally_hit = True


def test_ping_ok_text_and_model():
    b = _FakeBackend([TextDelta("pong")])
    r = ping_backend(b)
    assert r.ok is True
    assert "pong" in r.text
    assert r.model == "resolved-model"
    assert r.elapsed >= 0


def test_ping_exception_reports_error():
    class _Boom:
        name = "boom"
        model = ""

        def stream(self, messages, tools=None):
            raise RuntimeError("no binary")
            yield  # pragma: no cover

    r = ping_backend(_Boom())
    assert r.ok is False
    assert "no binary" in r.error


def test_ping_early_break_closes_generator():
    # A long stream: ping should break at >= 8 chars and close the generator,
    # running its finally block.
    events = [TextDelta("x" * 3) for _ in range(100)]
    b = _FakeBackend(events)
    r = ping_backend(b)
    assert r.ok is True
    assert b.finally_hit is True          # generator was closed early
    assert len(r.text) <= 12              # broke soon after crossing the threshold


def test_ping_tool_call_is_success():
    b = _FakeBackend([ToolCallRequest(id="1", name="Bash", arguments={})])
    r = ping_backend(b)
    assert r.ok is True
    assert b.finally_hit is True
