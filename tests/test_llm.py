from types import SimpleNamespace as NS

from market_updater.llm import LLM


class FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        if kw["tool_choice"]["type"] == "tool":
            raise RuntimeError("400 tool_choice: type \"tool\" and \"any\" are not supported for this model.")
        return NS(content=[NS(type="tool_use", input={"ideas": []})],
                  usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0))


def test_forced_tool_refused_falls_back_to_auto(settings, db):
    llm = LLM(settings, db)
    msgs = FakeMessages()
    llm._anthropic = NS(messages=msgs)
    out = llm._claude_json("claude-opus-5-5", "trade_ideas", "sys", "user", {"type": "object"}, 100)
    assert out == {"ideas": []}
    assert [c["tool_choice"]["type"] for c in msgs.calls] == ["tool", "auto"]
    assert "submit" in msgs.calls[1]["system"]
