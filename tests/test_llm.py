from types import SimpleNamespace as NS

from market_updater.llm import LLM, strict_schema

USAGE = NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0)


class StructuredMessages:
    """Answers structured-output calls; the first call is cut off to test the retry."""

    def __init__(self, cut_first=False):
        self.calls = []
        self.cut_first = cut_first

    def create(self, **kw):
        self.calls.append(kw)
        assert "tool_choice" not in kw
        if self.cut_first and len(self.calls) == 1:
            return NS(content=[NS(type="text", text='{"ideas": [')], stop_reason="max_tokens", usage=USAGE)
        return NS(content=[NS(type="thinking", thinking="..."), NS(type="text", text='{"ideas": []}')],
                  stop_reason="end_turn", usage=USAGE)


def test_structured_output_with_room_for_thinking(settings, db):
    llm = LLM(settings, db)
    msgs = StructuredMessages()
    llm._anthropic = NS(messages=msgs)
    out = llm._claude_json("claude-sonnet-5-5", "write_market_story", "sys", "user",
                           {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1}}}, 3000)
    assert out == {"ideas": []}
    call = msgs.calls[0]
    assert call["max_tokens"] >= 12_000                      # thinking counts toward max_tokens
    schema = call["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False and "minimum" not in schema["properties"]["n"]


def test_cut_off_answer_is_retried_with_more_room(settings, db):
    llm = LLM(settings, db)
    msgs = StructuredMessages(cut_first=True)
    llm._anthropic = NS(messages=msgs)
    assert llm._claude_json("claude-opus-5-5", "trade_ideas", "s", "u", {"type": "object"}, 3000) == {"ideas": []}
    assert msgs.calls[1]["max_tokens"] == 2 * msgs.calls[0]["max_tokens"]


class ToolOnlyMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        if "output_config" in kw:
            raise RuntimeError("400 output_config: json_schema not supported")
        if kw["tool_choice"]["type"] == "tool":
            raise RuntimeError("400 tool_choice: type \"tool\" and \"any\" are not supported for this model.")
        return NS(content=[NS(type="tool_use", input={"ideas": []})], stop_reason="tool_use", usage=USAGE)


def test_falls_back_to_tool_use_when_structured_refused(settings, db):
    llm = LLM(settings, db)
    msgs = ToolOnlyMessages()
    llm._anthropic = NS(messages=msgs)
    out = llm._claude_json("claude-opus-5-5", "trade_ideas", "sys", "user", {"type": "object"}, 100)
    assert out == {"ideas": []}
    assert [c.get("tool_choice", {}).get("type") for c in msgs.calls] == [None, "tool", "auto"]
    assert "submit" in msgs.calls[-1]["system"]


def test_strict_schema_closes_nested_objects():
    s = strict_schema({"type": "object", "properties": {"a": {"type": "array", "items": {
        "type": "object", "properties": {"b": {"type": "string", "maxLength": 5}}}}}})
    inner = s["properties"]["a"]["items"]
    assert inner["additionalProperties"] is False and "maxLength" not in inner["properties"]["b"]


class StreamingMessages:
    """Like the real SDK: large non-streamed calls are refused, so calls must stream."""

    def __init__(self):
        self.streamed = []

    def create(self, **kw):
        raise ValueError("Streaming is required for operations that may take longer than 10 minutes.")

    def stream(self, **kw):
        from contextlib import contextmanager
        self.streamed.append(kw)

        @contextmanager
        def cm():
            yield NS(get_final_message=lambda: NS(content=[NS(type="text", text='{"ok": 1}')],
                                                  stop_reason="end_turn", usage=USAGE))
        return cm()


def test_large_budgets_are_streamed(settings, db):
    llm = LLM(settings, db)
    msgs = StreamingMessages()
    llm._anthropic = NS(messages=msgs)
    assert llm._claude_json("claude-opus-5-5", "trade_ideas", "s", "u", {"type": "object"}, 8000) == {"ok": 1}
    assert msgs.streamed[0]["max_tokens"] == 32_000
