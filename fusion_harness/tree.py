# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Modified for fusion-harness: adapted from harness2/core/tree.py. The two framework surfaces
# became one built-in surface; sub-agents, plugins and hooks were replaced by harness-defined
# tools (`tools/<name>.py`), which the executor exposes to the model and the improver may
# create or rewrite; added tool-header parsing and version fingerprints.

"""The harness tree: the editable surface, read/write/validate/diff.

A harness is a flat mapping of relative path -> text. The edit contract is
omitted-is-inherited: a proposal only needs to touch what it changes.

    system.md, guardrails.md, memory.md, tools.md     always on: in every request
    skills/<name>/SKILL.md (+ skills/<name>/scripts/*) loaded on demand by the `skill` tool
    tools/<name>.py                                   a tool the model can call directly
    scripts/<name>.py                                 a helper run through bash
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

Tree = dict[str, str]

ALWAYS_ON = ("system.md", "guardrails.md", "memory.md", "tools.md")
ALWAYS_ON_MAX = 12_000
BUILTIN_TOOLS = frozenset({"bash", "read_file", "write_file", "edit_file", "skill", "finish"})

_NAME = r"[a-z0-9][a-z0-9_\-]*"
_SKILL = re.compile(rf"skills/({_NAME})/SKILL\.md")
_SKILL_SCRIPT = re.compile(rf"skills/{_NAME}/scripts/[A-Za-z0-9_.\-]+")
_TOOL = re.compile(r"tools/([a-z][a-z0-9_]*)\.py")
_SCRIPT = re.compile(r"scripts/[a-z0-9_][a-z0-9_]*\.py")
_SCRIPT_REF = re.compile(r"((?:skills/[\w\-]+/)?scripts/[\w.\-]+\.py)")

TRIGGER_LINE = "When {desc}, load the `{name}` skill."


def normalize_rel(rel: str) -> str:
    r = str(rel).strip().replace("\\", "/")
    if r.startswith("/") or ".." in r.split("/"):
        raise ValueError(f"path escapes the harness: {rel!r}")
    while r.startswith("./"):
        r = r[2:]
    return r.strip("/")


def is_allowed(rel: str) -> bool:
    try:
        rel = normalize_rel(rel)
    except ValueError:
        return False
    if rel in ALWAYS_ON:
        return True
    return bool(
        _SKILL.fullmatch(rel)
        or _SKILL_SCRIPT.fullmatch(rel)
        or _TOOL.fullmatch(rel)
        or _SCRIPT.fullmatch(rel)
    )


def skill_names(tree: Tree) -> list[str]:
    return sorted({m.group(1) for rel in tree if (m := _SKILL.fullmatch(rel))})


def tool_names(tree: Tree) -> list[str]:
    return sorted({m.group(1) for rel in tree if (m := _TOOL.fullmatch(rel))})


def read_tree(d: Path | str, *, rejected: list[str] | None = None) -> Tree:
    """rel -> text for every file on the editable surface; off-surface paths go to `rejected`."""
    d = Path(d)
    out: Tree = {}
    if not d.is_dir():
        return out
    for p in sorted(d.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        rel = p.relative_to(d).as_posix()
        if not is_allowed(rel):
            if rejected is not None:
                rejected.append(rel)
            continue
        out[rel] = p.read_text(encoding="utf-8", errors="replace")
    return out


def write_tree(tree: Tree, dest: Path | str) -> Path:
    """Materialise exactly `tree` (editable files already in `dest` are replaced)."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for p in sorted(dest.rglob("*"), reverse=True):
        if p.is_file() and is_allowed(p.relative_to(dest).as_posix()):
            p.unlink()
    for a in ALWAYS_ON:
        if a not in tree:
            (dest / a).write_text("", encoding="utf-8")
    for rel, content in tree.items():
        p = dest / normalize_rel(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return dest


def always_on_text(tree: Tree) -> str:
    return "\n\n".join(tree.get(a, "").strip() for a in ALWAYS_ON if tree.get(a, "").strip())


def frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    out: dict[str, str] = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                out[k.strip()] = v.strip()
    return out


def autotrigger(tree: Tree) -> list[str]:
    """Give every skill a trigger line in guardrails.md if no always-on file names it."""
    fixed = []
    for name in skill_names(tree):
        if f"`{name}`" in always_on_text(tree) or name in always_on_text(tree):
            continue
        fm = frontmatter(tree.get(f"skills/{name}/SKILL.md", ""))
        desc = fm.get("description", "").rstrip(".") or f"the {name} workflow applies"
        desc = desc[0].lower() + desc[1:] if desc else desc
        line = TRIGGER_LINE.format(name=name, desc=desc)
        if len(always_on_text(tree)) + len(line) + 2 > ALWAYS_ON_MAX:
            continue
        cur = tree.get("guardrails.md", "").rstrip()
        tree["guardrails.md"] = (cur + "\n" + line + "\n") if cur else line + "\n"
        fixed.append(name)
    return fixed


def tool_spec(src: str) -> dict:
    """Parse the `TOOL = {...}` literal of a harness tool without executing the file."""
    module = ast.parse(src)
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "TOOL" for t in node.targets
        ):
            spec = ast.literal_eval(node.value)
            if not isinstance(spec, dict):
                raise ValueError("TOOL must be a dict literal")
            return spec
    raise ValueError("no `TOOL = {...}` assignment at module level")


def _check_tool(rel: str, name: str, src: str, errs: list[str]) -> None:
    try:
        compile(src, rel, "exec")
    except SyntaxError as exc:
        errs.append(f"{rel} does not compile: {exc.msg} (line {exc.lineno})")
        return
    try:
        spec = tool_spec(src)
    except (ValueError, SyntaxError) as exc:
        errs.append(
            f"{rel}: {exc}. A tool starts with TOOL = {{'name': '{name}', 'description': ..., "
            f"'input_schema': {{'type': 'object', 'properties': {{...}}}}}} and reads its "
            f"JSON arguments from stdin"
        )
        return
    if spec.get("name") != name:
        errs.append(f"{rel}: TOOL['name'] must be {name!r}, the file's name")
    if name in BUILTIN_TOOLS:
        errs.append(f"{rel}: {name!r} is a built-in tool name; choose another")
    if not str(spec.get("description", "")).strip():
        errs.append(f"{rel}: TOOL needs a non-empty 'description' -- the model reads it")
    schema = spec.get("input_schema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        errs.append(f"{rel}: TOOL['input_schema'] must be a JSON schema with type 'object'")


def validate(tree: Tree, *, always_on_max: int = ALWAYS_ON_MAX) -> dict:
    """Raise ValueError listing every problem (fed back verbatim to the improver)."""
    errs: list[str] = []
    body = always_on_text(tree)
    if len(body) > always_on_max:
        errs.append(
            f"always-on text is {len(body)} chars, over the {always_on_max} cap: it is paid for "
            f"on every request; move procedures into skills or tools"
        )
    if not tree.get("system.md", "").strip():
        errs.append("system.md is empty: it is the agent's role and loop contract")

    for name in skill_names(tree):
        text = tree[f"skills/{name}/SKILL.md"]
        fm = frontmatter(text)
        if not fm.get("description"):
            errs.append(f"skills/{name}/SKILL.md needs frontmatter with a `description:`")
        if fm.get("name") and fm["name"] != name:
            errs.append(f"skills/{name}/SKILL.md frontmatter name must be {name!r}")
        if name not in body:
            errs.append(
                f"skill {name!r} is never named in always-on text, so the agent will never load "
                f"it; add a line such as: When <situation>, load the `{name}` skill."
            )

    for rel, src in tree.items():
        m = _TOOL.fullmatch(rel)
        if m:
            _check_tool(rel, m.group(1), src, errs)
        elif (_SCRIPT.fullmatch(rel) or _SKILL_SCRIPT.fullmatch(rel)) and rel.endswith(".py"):
            try:
                compile(src, rel, "exec")
            except SyntaxError as exc:
                errs.append(f"{rel} does not compile: {exc.msg} (line {exc.lineno})")

    have = {rel for rel in tree if _SCRIPT.fullmatch(rel) or _SKILL_SCRIPT.fullmatch(rel)}
    for rel, src in tree.items():
        if not (rel in ALWAYS_ON or _SKILL.fullmatch(rel)):
            continue
        for ref in set(_SCRIPT_REF.findall(src)):
            if ref not in have:
                errs.append(f"{rel} refers to `{ref}` but that file is not in the harness")

    if errs:
        raise ValueError(" | ".join(errs))
    return {
        "always_on_chars": len(body),
        "files": len(tree),
        "skills": len(skill_names(tree)),
        "tools": len(tool_names(tree)),
    }


def diff_summary(before: Tree, after: Tree) -> dict:
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "modified": sorted(r for r in set(before) & set(after) if before[r] != after[r]),
        "always_on_delta": len(always_on_text(after)) - len(always_on_text(before)),
    }


def unchanged(before: Tree, after: Tree) -> bool:
    d = diff_summary(before, after)
    return not (d["added"] or d["removed"] or d["modified"])


def fingerprint(domain: str, tree: Tree) -> str:
    blob = json.dumps({"domain": domain, "tree": tree}, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def base_tree() -> Tree:
    """The seed harness shipped with the package."""
    return read_tree(Path(__file__).parent / "base_harness")
