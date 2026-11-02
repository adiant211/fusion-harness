from types import SimpleNamespace as NS

import anthropic
import httpx2
import pytest

from fusion_harness.model import AnthropicModel, ModelError


class FakeStream:
    def __init__(self, msg=None, exc=None):
        self.msg, self.exc = msg, exc

    def __enter__(self):
        if self.exc:
            raise self.exc
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class FakeMessages:
    def __init__(self, result):
        self.result = result
        self.kwargs = None

    def stream(self, **kwargs):
        self.kwargs = kwargs
        return self.result


def message():
    return NS(
        content=[
            NS(type="thinking", thinking=""),
            NS(type="text", text="Looking."),
            NS(type="tool_use", id="toolu_1", name="bash", input={"command": "ls"}),
        ],
        stop_reason="tool_use",
        usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=7,
                 cache_creation_input_tokens=3),
    )


def client(result):
    msgs, beta = FakeMessages(result), FakeMessages(result)
    return NS(messages=msgs, beta=NS(messages=beta)), msgs, beta


TOOLS = [{"name": "bash", "description": "d", "input_schema": {"type": "object"}}]


def test_request_shape_and_turn():
    c, msgs, beta = client(FakeStream(message()))
    m = AnthropicModel("some-model", effort="high", client=c)
    turn = m.turn("SYSTEM", [{"role": "user", "content": "hi"}], TOOLS)
    k = msgs.kwargs
    assert k["model"] == "some-model" and k["system"] == "SYSTEM"
    assert k["cache_control"] == {"type": "ephemeral"}
    assert k["thinking"] == {"type": "adaptive"} and k["output_config"] == {"effort": "high"}
    assert k["tools"][0]["eager_input_streaming"] is True and "eager_input_streaming" not in TOOLS[0]
    assert beta.kwargs is None
    assert turn.text == "Looking." and turn.tool_calls[0].input == {"command": "ls"}
    assert turn.content is message().content or len(turn.content) == 3  # appended unchanged
    assert (turn.usage.cache_read, turn.usage.cache_write) == (7, 3)


def test_thinking_off_and_fallbacks_use_beta():
    c, msgs, beta = client(FakeStream(message()))
    AnthropicModel("m", thinking="off", effort="", fallbacks="default", client=c).turn("s", [], TOOLS)
    k = beta.kwargs
    assert msgs.kwargs is None
    assert "thinking" not in k and "output_config" not in k
    assert k["fallbacks"] == "default" and k["betas"] == ["server-side-fallback-2026-07-01"]


def test_unparseable_tool_json_becomes_reissue():
    c, *_ = client(FakeStream(exc=ValueError("Unable to parse tool parameter JSON")))
    turn = AnthropicModel("m", client=c).turn("s", [], TOOLS)
    assert turn.stop_reason == "invalid_json" and "parse" in turn.error


def api_error(cls, status):
    resp = httpx2.Response(status, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    return cls("boom", response=resp, body=None)


@pytest.mark.parametrize("cls,status,human,transient", [
    (anthropic.AuthenticationError, 401, True, False),
    (anthropic.NotFoundError, 404, True, False),
    (anthropic.RateLimitError, 429, False, True),
    (anthropic.BadRequestError, 400, False, False),
])
def test_error_mapping(cls, status, human, transient):
    c, *_ = client(FakeStream(exc=api_error(cls, status)))
    with pytest.raises(ModelError) as e:
        AnthropicModel("m", client=c).turn("s", [], TOOLS)
    assert e.value.needs_human is human and e.value.transient is transient


def test_model_is_required():
    with pytest.raises(ValueError, match="FH_MODEL"):
        AnthropicModel("", client=object())
