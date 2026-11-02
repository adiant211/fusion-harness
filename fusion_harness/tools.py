"""Tools the agent can call, and the checks every tool input passes before it runs.

Built-ins cover files and the shell. Harness tools (`tools/<name>.py` in the harness tree) are
small programs the improver can create and rewrite: each declares `TOOL = {...}` and reads its
JSON arguments from stdin. That is how the harness refines its own tools.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import scrub_env
from . import tree as harness_tree


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


BASH = {
    "name": "bash",
    "description": (
        "Run a shell command in the workspace and return its exit code and output. Use it for "
        "builds, tests, git, gh and the harness scripts in $FH_HARNESS/scripts/."
    ),
    "input_schema": _schema(
        {
            "command": {"type": "string", "description": "The command, run with bash -c."},
            "timeout_s": {"type": "integer", "description": "Seconds before it is killed."},
        },
        ["command"],
    ),
}
READ_FILE = {
    "name": "read_file",
    "description": "Read a text file (numbered lines). Paths are relative to the workspace.",
    "input_schema": _schema(
        {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "First line, 1-based."},
            "limit": {"type": "integer", "description": "Number of lines."},
        },
        ["path"],
    ),
}
WRITE_FILE = {
    "name": "write_file",
    "description": "Create or overwrite a file with the given content.",
    "input_schema": _schema(
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]
    ),
}
EDIT_FILE = {
    "name": "edit_file",
    "description": (
        "Replace an exact string in a file. `old` must occur exactly once unless replace_all."
    ),
    "input_schema": _schema(
        {
            "path": {"type": "string"},
            "old": {"type": "string"},
            "new": {"type": "string"},
            "replace_all": {"type": "boolean"},
        },
        ["path", "old", "new"],
    ),
}
SKILL = {
    "name": "skill",
    "description": "Load a skill by name: its full instructions and the helper scripts it ships.",
    "input_schema": _schema({"name": {"type": "string"}}, ["name"]),
}

WORKER_FINISH = {
    "name": "finish",
    "description": (
        "End this attempt. status=done when the work is complete and verified; need_human only "
        "when you cannot continue without a person (a credential, access, information that "
        "exists nowhere you can reach, or a decision that is theirs); impossible when the task "
        "cannot be done as stated."
    ),
    "input_schema": _schema(
        {
            "status": {"type": "string", "enum": ["done", "need_human", "impossible"]},
            "summary": {
                "type": "string",
                "description": "One or two sentences a person can act on: the result, or why "
                "you stopped. No implementation detail.",
            },
            "details": {"type": "string", "description": "Technical record: changes, checks run."},
            "question": {
                "type": "string",
                "description": "With need_human: ONE concrete question whose answer unblocks you.",
            },
            "category": {
                "type": "string",
                "enum": ["missing_access", "missing_info", "decision", "external"],
            },
            "tried": {"type": "string", "description": "With need_human: what you tried."},
        },
        ["status", "summary"],
    ),
}

BUILTINS = (BASH, READ_FILE, WRITE_FILE, EDIT_FILE, SKILL)


def finish_tool(description: str, properties: dict, required: list[str]) -> dict:
    return {"name": "finish", "description": description,
            "input_schema": _schema(properties, required)}


# ---------------------------------------------------------------- validation

_TYPES = {
    "string": str,
    "integer": int,
    "boolean": bool,
    "number": (int, float),
    "array": list,
    "object": dict,
}


def validate_input(schema: dict, data: object) -> str:
    """Return "" if `data` fits the schema's top level, else what is wrong.

    Eager input streaming hands over whatever was parsed, possibly truncated, so nothing runs
    on input that misses a required field or carries the wrong type.
    """
    if not isinstance(data, dict):
        return "input is not a JSON object"
    props = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in data:
            return f"missing required field {name!r}"
    for name, value in data.items():
        spec = props.get(name)
        if spec is None:
            if schema.get("additionalProperties") is False:
                return f"unknown field {name!r}"
            continue
        want = _TYPES.get(spec.get("type", ""))
        if want is not None:
            if want is int and isinstance(value, bool):
                return f"{name!r} must be an integer"
            if not isinstance(value, want):
                return f"{name!r} must be {spec['type']}"
        if "enum" in spec and value not in spec["enum"]:
            return f"{name!r} must be one of {spec['enum']}"
    return ""


# ----------------------------------------------------------------- execution


@dataclass
class ToolContext:
    workspace: Path
    harness_dir: Path
    timeout: int = 600
    observation_chars: int = 20_000
    env: dict | None = None

    def path(self, rel: str) -> Path:
        p = Path(rel).expanduser()
        return p if p.is_absolute() else self.workspace / p

    def environ(self) -> dict:
        return scrub_env(
            {"FH_WORKSPACE": str(self.workspace), "FH_HARNESS": str(self.harness_dir),
             **(self.env or {})}
        )


def clip(text: str, limit: int) -> str:
    """Keep the head and the tail; the middle is where long outputs are least informative."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    cut = len(text) - head - tail
    return f"{text[:head]}\n[... {cut} chars cut by the harness ...]\n{text[-tail:]}"


def run_bash(ctx: ToolContext, command: str, timeout_s: int | None = None) -> tuple[str, bool]:
    timeout = min(int(timeout_s or ctx.timeout), ctx.timeout)
    try:
        proc = subprocess.run(
            ["bash", "-c", command], cwd=ctx.workspace, env=ctx.environ(), capture_output=True,
            text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) else ""
        return clip(f"timed out after {timeout}s\n{out}", ctx.observation_chars), True
    out = proc.stdout + (("\n[stderr]\n" + proc.stderr) if proc.stderr.strip() else "")
    return clip(f"exit {proc.returncode}\n{out}", ctx.observation_chars), proc.returncode != 0


def read_file(ctx: ToolContext, path: str, offset: int = 1, limit: int = 2000) -> tuple[str, bool]:
    p = ctx.path(path)
    if not p.is_file():
        return f"no such file: {path}", True
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, int(offset or 1))
    chunk = lines[start - 1 : start - 1 + int(limit or 2000)]
    body = "\n".join(f"{start + i:6}\t{line}" for i, line in enumerate(chunk))
    more = f"\n[{len(lines)} lines total]" if start - 1 + len(chunk) < len(lines) else ""
    return clip(body + more, ctx.observation_chars) or "(empty file)", False


def write_file(ctx: ToolContext, path: str, content: str) -> tuple[str, bool]:
    p = ctx.path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {path}", False


def edit_file(ctx: ToolContext, path: str, old: str, new: str,
              replace_all: bool = False) -> tuple[str, bool]:
    p = ctx.path(path)
    if not p.is_file():
        return f"no such file: {path}", True
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        return f"`old` was not found in {path}; read the file and copy the text exactly", True
    if count > 1 and not replace_all:
        return f"`old` occurs {count} times in {path}; add context or set replace_all", True
    p.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1),
                 encoding="utf-8")
    return f"edited {path} ({count if replace_all else 1} replacement)", False


def load_skill(ctx: ToolContext, tree: dict[str, str], name: str) -> tuple[str, bool]:
    body = tree.get(f"skills/{name}/SKILL.md")
    if body is None:
        names = ", ".join(harness_tree.skill_names(tree)) or "none"
        return f"no skill named {name!r}; available: {names}", True
    scripts = sorted(r for r in tree if r.startswith(f"skills/{name}/scripts/"))
    extra = ""
    if scripts:
        extra = "\n\nHelper scripts (run with bash):\n" + "\n".join(
            f"- python $FH_HARNESS/{r}" for r in scripts
        )
    return body + extra, False


def harness_tools(tree: dict[str, str]) -> dict[str, dict]:
    """name -> tool definition for every valid tool in the harness tree."""
    out = {}
    for name in harness_tree.tool_names(tree):
        try:
            spec = harness_tree.tool_spec(tree[f"tools/{name}.py"])
        except (ValueError, SyntaxError):
            continue
        out[name] = {
            "name": name,
            "description": str(spec.get("description", "")),
            "input_schema": spec.get("input_schema", {"type": "object", "properties": {}}),
        }
    return out


def run_harness_tool(ctx: ToolContext, name: str, data: dict) -> tuple[str, bool]:
    script = ctx.harness_dir / "tools" / f"{name}.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(script)], input=json.dumps(data), cwd=ctx.workspace,
            env=ctx.environ(), capture_output=True, text=True, timeout=ctx.timeout,
        )
    except subprocess.TimeoutExpired:
        return f"tool {name} timed out after {ctx.timeout}s", True
    out = proc.stdout
    if proc.returncode != 0:
        out = f"tool {name} exited {proc.returncode}\n{proc.stdout}\n[stderr]\n{proc.stderr}"
    return clip(out or "(no output)", ctx.observation_chars), proc.returncode != 0


Handler = Callable[[ToolContext, dict], tuple[str, bool]]


def toolbox(tree: dict[str, str], *, finish: dict, builtins=BUILTINS) -> tuple[list[dict], dict]:
    """The tool definitions for one role, and name -> handler for everything but `finish`."""
    handlers: dict[str, Handler] = {
        "bash": lambda c, d: run_bash(c, d["command"], d.get("timeout_s")),
        "read_file": lambda c, d: read_file(c, d["path"], d.get("offset", 1), d.get("limit", 2000)),
        "write_file": lambda c, d: write_file(c, d["path"], d["content"]),
        "edit_file": lambda c, d: edit_file(c, d["path"], d["old"], d["new"],
                                            bool(d.get("replace_all"))),
        "skill": lambda c, d: load_skill(c, tree, d["name"]),
    }
    defs = [t for t in builtins]
    names = {t["name"] for t in defs}
    handlers = {k: v for k, v in handlers.items() if k in names}
    if "skill" in names and not harness_tree.skill_names(tree):
        defs = [t for t in defs if t["name"] != "skill"]
        handlers.pop("skill", None)
    for name, spec in harness_tools(tree).items():
        defs.append(spec)
        handlers[name] = lambda c, d, _n=name: run_harness_tool(c, _n, d)
    defs.append(finish)
    return defs, handlers
