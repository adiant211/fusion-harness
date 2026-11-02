"""Model adapters. The loop needs one thing from a model: given a system prompt, an append-only
message history and tool definitions, return the next turn.

`AnthropicModel` is the shipped provider (official SDK, streaming, prompt caching). Another
provider plugs in by implementing `Model.turn` and returning the same `Turn` shape.
`ScriptedModel` drives tests and the offline demo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write


@dataclass
class Turn:
    """One assistant turn. `content` is appended to history unchanged (append-only history
    keeps prompt caching and thinking blocks valid)."""

    content: Any
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    usage: Usage = field(default_factory=Usage)
    error: str = ""


class ModelError(RuntimeError):
    """A provider failure the loop should not retry blindly. `needs_human` marks failures only a
    person can fix, such as a rejected credential."""

    def __init__(self, message: str, *, needs_human: bool = False, transient: bool = False):
        super().__init__(message)
        self.needs_human = needs_human
        self.transient = transient


class Model(Protocol):
    name: str

    def turn(self, system: str, messages: list[dict], tools: list[dict]) -> Turn: ...


class AnthropicModel:
    """Claude through the official SDK: streamed requests, automatic prompt caching, adaptive
    thinking and an effort level, with optional server-side refusal fallbacks."""

    def __init__(
        self,
        model: str,
        *,
        effort: str = "high",
        thinking: str = "adaptive",
        fallbacks: str = "",
        max_tokens: int = 32_000,
        client: Any = None,
    ):
        if not model:
            raise ValueError(
                "no model configured: set FH_MODEL (or pass --model) to a model id your "
                "Anthropic account can use"
            )
        if client is None:
            import anthropic

            client = anthropic.Anthropic(max_retries=4)
        self.client = client
        self.name = model
        self.effort = effort
        self.thinking = thinking
        self.fallbacks = fallbacks
        self.max_tokens = max_tokens

    def request(self, system: str, messages: list[dict], tools: list[dict]) -> tuple[Any, dict]:
        kwargs: dict = {
            "model": self.name,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
            # Large tool inputs (whole files) stream as they are generated; the executor
            # validates every input against its schema before running the tool.
            "tools": [{**t, "eager_input_streaming": True} for t in tools],
            "cache_control": {"type": "ephemeral"},
        }
        if self.thinking == "adaptive":
            kwargs["thinking"] = {"type": "adaptive"}
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        api = self.client.messages
        if self.fallbacks:
            api = self.client.beta.messages
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = self.fallbacks
        return api, kwargs

    def turn(self, system: str, messages: list[dict], tools: list[dict]) -> Turn:
        import anthropic

        api, kwargs = self.request(system, messages, tools)
        try:
            with api.stream(**kwargs) as stream:
                msg = stream.get_final_message()
        except ValueError as exc:
            # The stream could not parse a tool input at all; there is no tool_use id to
            # answer, so the executor re-issues the request.
            return Turn(content=None, text="", tool_calls=[], stop_reason="invalid_json",
                        error=str(exc))
        except anthropic.AuthenticationError as exc:
            raise ModelError(f"the model provider rejected the credentials: {exc}",
                             needs_human=True) from exc
        except anthropic.PermissionDeniedError as exc:
            raise ModelError(f"the credentials lack access to {self.name}: {exc}",
                             needs_human=True) from exc
        except anthropic.NotFoundError as exc:
            raise ModelError(f"model {self.name!r} was not found: {exc}",
                             needs_human=True) from exc
        except anthropic.BadRequestError as exc:
            raise ModelError(f"the request was rejected: {exc}") from exc
        except (anthropic.RateLimitError, anthropic.APIConnectionError,
                anthropic.InternalServerError) as exc:
            raise ModelError(f"provider unavailable after retries: {exc}",
                             transient=True) from exc

        text = "".join(getattr(b, "text", "") for b in msg.content if b.type == "text")
        calls = [
            ToolCall(id=b.id, name=b.name, input=b.input if isinstance(b.input, dict) else {})
            for b in msg.content
            if b.type == "tool_use"
        ]
        u = msg.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        return Turn(content=msg.content, text=text, tool_calls=calls,
                    stop_reason=msg.stop_reason or "", usage=usage)


Script = Callable[[str, list[dict], list[dict]], list[dict]]


class ScriptedModel:
    """A deterministic model for tests and the demo. `script(system, messages, tools)` returns
    the turn's content blocks as dicts: {"type": "text", "text": ...} or
    {"type": "tool_use", "name": ..., "input": {...}} (ids are filled in)."""

    def __init__(self, script: Script, name: str = "scripted"):
        self.script = script
        self.name = name
        self.calls = 0

    def turn(self, system: str, messages: list[dict], tools: list[dict]) -> Turn:
        self.calls += 1
        blocks = self.script(system, messages, tools)
        content, calls, text = [], [], []
        for i, b in enumerate(blocks):
            if b["type"] == "tool_use":
                b = {"id": b.get("id") or f"toolu_{self.calls}_{i}", **b}
                calls.append(ToolCall(b["id"], b["name"], dict(b.get("input", {}))))
            elif b["type"] == "text":
                text.append(b["text"])
            content.append(b)
        stop = blocks[-1].get("stop_reason") if blocks and "stop_reason" in blocks[-1] else None
        stop = stop or ("tool_use" if calls else "end_turn")
        clean = [{k: v for k, v in b.items() if k != "stop_reason"} for b in content]
        return Turn(content=clean, text="".join(text), tool_calls=calls, stop_reason=stop,
                    usage=Usage(input_tokens=100, output_tokens=20))


def build(settings, *, improver: bool = False) -> Model:
    name = settings.improver() if improver else settings.model
    return AnthropicModel(
        name,
        effort=settings.effort,
        thinking=settings.thinking,
        fallbacks=settings.fallbacks,
        max_tokens=settings.max_tokens,
    )
