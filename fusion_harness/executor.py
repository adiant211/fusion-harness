"""One agent session: a tool loop with an append-only history, recorded as a trajectory.

Every role runs on this loop -- the worker that does a Task, the verifier that judges it, the
improver that edits the harness, triage and reflection -- each with its own system prompt,
tools and `finish` schema. The session ends when the model calls `finish`, runs out of steps,
or the provider fails.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .model import Model, ModelError, Usage
from .tools import BUILTINS, ToolContext, toolbox, validate_input

NUDGE = (
    "You stopped without calling `finish`. Continue the work, or call `finish` with your "
    "status and summary."
)


@dataclass
class Session:
    stop: str
    finish: dict | None = None
    trajectory: list[dict] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    error: str = ""
    needs_human: bool = False
    transient: bool = False
    steps: int = 0

    def rendered(self, obs_chars: int = 4_000) -> str:
        return render(self.trajectory, obs_chars=obs_chars)


def render(events: list[dict], *, obs_chars: int = 4_000) -> str:
    """The shared trajectory grammar: [SAY] / [ACTION] / [OBSERVATION] / [ERROR]."""
    lines = []
    for e in events:
        kind = e["kind"]
        if kind == "say":
            lines.append(f"[SAY] {e['text'].strip()}")
        elif kind == "action":
            args = json.dumps(e["input"], ensure_ascii=False)
            if len(args) > 1500:
                args = args[:1500] + f"... [{len(args)} chars]"
            lines.append(f"[ACTION] {e['tool']} {args}")
        elif kind == "observation":
            out = e["output"]
            if len(out) > obs_chars:
                out = out[: obs_chars // 2] + "\n[... cut for this view ...]\n" + out[-obs_chars // 2 :]
            tag = "[ERROR]" if e.get("error") else "[OBSERVATION]"
            lines.append(f"{tag} {out}")
        elif kind == "stop":
            lines.append(f"[STOP] {e['reason']} {e.get('detail', '')}".rstrip())
    return "\n".join(lines)


def run_session(
    model: Model,
    *,
    system: str,
    prompt: str,
    tree: dict[str, str],
    ctx: ToolContext,
    finish: dict,
    builtins=BUILTINS,
    max_steps: int = 60,
    run_dir: Path | None = None,
) -> Session:
    tools, handlers = toolbox(tree, finish=finish, builtins=builtins)
    schemas = {t["name"]: t["input_schema"] for t in tools}
    messages: list[dict] = [{"role": "user", "content": prompt}]
    s = Session(stop="max_steps")
    nudges = reissues = 0

    def done(stop: str, detail: str = "") -> Session:
        s.stop = stop
        s.trajectory.append({"kind": "stop", "reason": stop, "detail": detail})
        if run_dir is not None:
            save(run_dir, s)
        return s

    for step in range(max_steps):
        s.steps = step + 1
        try:
            turn = model.turn(system, messages, tools)
        except ModelError as exc:
            s.error, s.needs_human, s.transient = str(exc), exc.needs_human, exc.transient
            return done("error", str(exc))
        s.usage.add(turn.usage)

        if turn.stop_reason == "invalid_json":
            reissues += 1
            s.trajectory.append({"kind": "observation", "output": turn.error, "error": True})
            if reissues > 2:
                return done("error", "the model's tool input could not be parsed three times")
            continue
        if turn.stop_reason == "refusal":
            return done("refusal", turn.text)

        messages.append({"role": "assistant", "content": turn.content})
        if turn.text.strip():
            s.trajectory.append({"kind": "say", "text": turn.text})
        if turn.stop_reason == "pause_turn":
            continue
        if not turn.tool_calls:
            if nudges >= 2:
                return done("no_finish")
            nudges += 1
            messages.append({"role": "user", "content": NUDGE})
            continue

        truncated = turn.stop_reason == "max_tokens"
        results: list[dict] = []
        finished: dict | None = None
        for call in turn.tool_calls:
            s.trajectory.append({"kind": "action", "tool": call.name, "input": call.input})
            if truncated:
                out, err = (
                    "Not run: your output reached max_tokens, so this input may be cut off. "
                    "Issue it again in smaller pieces (for a large file, write a first part "
                    "and extend it with edit_file).",
                    True,
                )
            elif call.name not in schemas:
                out, err = f"unknown tool {call.name!r}", True
            elif problem := validate_input(schemas[call.name], call.input):
                out = json.dumps({"INVALID_INPUT": problem, "received": call.input})[:4000]
                err = True
            elif call.name == "finish":
                finished, out, err = call.input, "finished", False
            else:
                try:
                    out, err = handlers[call.name](ctx, call.input)
                except Exception as exc:  # a tool bug is an observation, not a crash
                    out, err = f"{type(exc).__name__}: {exc}", True
            s.trajectory.append({"kind": "observation", "output": out, "error": err})
            results.append(
                {"type": "tool_result", "tool_use_id": call.id, "content": out, "is_error": err}
            )
        if finished is not None:
            s.finish = finished
            return done("finished")
        left = max_steps - (step + 1)
        if left == 3:
            results.append(
                {"type": "text", "text": "3 steps left in this attempt: wrap up and call `finish`."}
            )
        messages.append({"role": "user", "content": results})
    return done("max_steps")


def save(run_dir: Path, s: Session) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "trajectory.jsonl").open("w", encoding="utf-8") as f:
        for e in s.trajectory:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    (run_dir / "trajectory.md").write_text(render(s.trajectory), encoding="utf-8")
    (run_dir / "session.json").write_text(
        json.dumps(
            {
                "stop": s.stop,
                "finish": s.finish,
                "error": s.error,
                "steps": s.steps,
                "usage": s.usage.__dict__,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
