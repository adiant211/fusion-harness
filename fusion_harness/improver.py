"""Task-level recursion: propose a harness edit from the evidence, optionally probe several
candidates and compose one (Harness² Propose-Probe-Compose), plus post-task consolidation of
what people taught and the reflection that writes the domain library.

The improver is an ordinary session on the shared executor whose working directory is an
evidence tree; it edits `harness/` and `task_notes.md` in place, and the kernel validates the
result before it can run. An invalid proposal gets one retry with the validator's message;
after that the current harness is kept.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import prompts
from . import tree as harness_tree
from .config import Settings
from .domain import Attempt, Feedback, Lesson, Task
from .executor import run_session
from .model import Model
from .tools import BASH, EDIT_FILE, READ_FILE, WRITE_FILE, ToolContext, finish_tool

STAGE_ATTEMPTS = 2

IMPROVER_FINISH = finish_tool(
    prompts.IMPROVER_FINISH_DESC,
    {"status": {"type": "string", "enum": ["changed", "keep"]}, "notes": {"type": "string"}},
    ["status", "notes"],
)

REFLECT_FINISH = finish_tool(
    prompts.REFLECT_FINISH_DESC,
    {
        "entry": {"type": "boolean"},
        "cue": {"type": "string"},
        "lesson": {"type": "string"},
        "evidence": {"type": "string"},
        "confidence": {"type": "string"},
        "edits": {"type": "string"},
    },
    ["entry"],
)


@dataclass
class Proposal:
    tree: dict[str, str]
    task_notes: str
    notes: str = ""
    changed: bool = False
    error: str = ""
    label: str = ""


@dataclass
class Candidate:
    label: str
    proposal: Proposal
    attempt: Attempt | None = None
    extra: dict = field(default_factory=dict)


def _attempt_md(a: Attempt) -> str:
    lines = [f"# Attempt {a.n} (harness {a.version_id})", ""]
    if a.finish:
        lines += [f"- worker status: {a.finish.status.value}", f"- summary: {a.finish.summary}"]
        if a.finish.question:
            lines.append(f"- question: {a.finish.question}")
    lines.append(f"- stopped: {a.stop}" + (f" ({a.error})" if a.error else ""))
    if a.verdict:
        lines += [
            f"- verdict: {'PASSED' if a.verdict.passed else 'FAILED'} by {a.verdict.method}",
            "",
            "## Verdict evidence",
            a.verdict.evidence,
        ]
    lines += ["", "## Trajectory", a.trajectory or "(no trajectory recorded)"]
    return "\n".join(lines)


def build_context(
    root: Path,
    *,
    task: Task,
    tree: dict[str, str],
    attempts: list[Attempt],
    feedback: list[Feedback],
    lessons: list[Lesson],
    candidates: list[Candidate] = (),
) -> str:
    """Write the evidence tree; return the layout listing for the mission (only what exists)."""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    (root / "task.md").write_text(task.prompt(), encoding="utf-8")
    harness_tree.write_tree(tree, root / "harness")
    (root / "task_notes.md").write_text(task.notes or "", encoding="utf-8")
    layout = [
        f"(task: {task.title})",
        "task.md            the task as the agent received it",
        "harness/           the harness you may edit",
        "task_notes.md      notes only later attempts of THIS task read (you may edit)",
    ]
    if attempts:
        d = root / "attempts"
        d.mkdir()
        for a in attempts:
            (d / f"attempt-{a.n:02d}.md").write_text(_attempt_md(a), encoding="utf-8")
        layout.append(f"attempts/          {len(attempts)} attempts with verdicts and FULL trajectories")
    if feedback:
        d = root / "feedback"
        d.mkdir()
        for i, fb in enumerate(feedback, 1):
            (d / f"{i:02d}-{fb.kind.value}.md").write_text(
                f"# {fb.kind.value} from {fb.actor or 'a person'}\n\n{fb.text}\n", encoding="utf-8"
            )
        kinds = ", ".join(sorted({fb.kind.value for fb in feedback}))
        layout.append(f"feedback/          {len(feedback)} things people said ({kinds}) -- "
                      f"strongest evidence")
    if lessons:
        rows = ["# Library: lessons from earlier tasks in this domain", ""]
        for le in lessons:
            rows += [f"## {le.cue}", f"- lesson: {le.lesson}", f"- evidence: {le.evidence}",
                     f"- confidence: {le.confidence}", ""]
        (root / "library.md").write_text("\n".join(rows), encoding="utf-8")
        layout.append(f"library.md         {len(lessons)} lessons from earlier tasks (hypotheses here)")
    if candidates:
        d = root / "candidates"
        for c in candidates:
            harness_tree.write_tree(c.proposal.tree, d / c.label / "harness")
            probe = _attempt_md(c.attempt) if c.attempt else "(not probed)"
            (d / c.label / "probe.md").write_text(
                f"# Candidate {c.label}\n\n## Its notes\n{c.proposal.notes}\n\n{probe}",
                encoding="utf-8",
            )
        layout.append(f"candidates/        {len(candidates)} candidate harnesses and their probes")
    return "\n".join(layout)


def propose(
    model: Model,
    settings: Settings,
    *,
    task: Task,
    tree: dict[str, str],
    attempts: list[Attempt],
    feedback: list[Feedback],
    lessons: list[Lesson],
    dim: str,
    root: Path,
    candidates: list[Candidate] = (),
) -> Proposal:
    layout = build_context(root, task=task, tree=tree, attempts=attempts, feedback=feedback,
                           lessons=lessons, candidates=candidates)
    mission = prompts.improve(dim, layout)
    ctx = ToolContext(workspace=root, harness_dir=root / "harness",
                      timeout=settings.command_timeout,
                      observation_chars=settings.observation_chars)
    err = ""
    for n in range(1, STAGE_ATTEMPTS + 1):
        prompt = mission
        if err:
            prompt += (f"\n\n# YOUR PREVIOUS ATTEMPT WAS REJECTED\n{err}\n\nFix it in `harness/` "
                       f"(your earlier edits are still there) and finish again.")
        session = run_session(
            model, system=prompts.IMPROVER_SYSTEM, prompt=prompt, tree={}, ctx=ctx,
            finish=IMPROVER_FINISH, builtins=(BASH, READ_FILE, WRITE_FILE, EDIT_FILE),
            max_steps=min(40, settings.max_steps), run_dir=root.parent / f"{root.name}.s{n}",
        )
        if session.stop == "error":
            return Proposal(tree=tree, task_notes=task.notes, error=session.error, label=dim)
        rejected: list[str] = []
        new = harness_tree.read_tree(root / "harness", rejected=rejected)
        if rejected:
            err = ("these files are off the editable surface and were not applied: "
                   + ", ".join(sorted(rejected)[:8])
                   + ". Names are lowercase letters, digits, `_` or `-`; see the surface table.")
            continue
        harness_tree.autotrigger(new)
        try:
            harness_tree.validate(new)
        except ValueError as exc:
            err = str(exc)
            harness_tree.write_tree(new, root / "harness")
            continue
        notes_path = root / "task_notes.md"
        task_notes = notes_path.read_text(encoding="utf-8") if notes_path.is_file() else task.notes
        return Proposal(
            tree=new,
            task_notes=task_notes,
            notes=str((session.finish or {}).get("notes", "")).strip(),
            changed=not harness_tree.unchanged(tree, new),
            label=dim,
        )
    return Proposal(tree=tree, task_notes=task.notes, error=err[:1000], label=dim)


def reflect(
    model: Model,
    settings: Settings,
    *,
    task: Task,
    tree: dict[str, str],
    attempts: list[Attempt],
    feedback: list[Feedback],
    root: Path,
) -> Lesson | None:
    """Distil at most one transferable lesson for the domain library, or none."""
    layout = build_context(root, task=task, tree=tree, attempts=attempts, feedback=feedback,
                           lessons=[])
    prompt = (
        "Write the lesson for this domain's library from the record below. `harness/` is the "
        "harness that produced the final result; each attempt names the harness version it ran "
        "under.\n\n" + prompts.EVIDENCE_IS_DATA + "\n\n# THE RECORD\n" + layout
    )
    session = run_session(
        model, system=prompts.REFLECT_SYSTEM, prompt=prompt, tree={},
        ctx=ToolContext(workspace=root, harness_dir=root / "harness"),
        finish=REFLECT_FINISH, builtins=(BASH, READ_FILE), max_steps=min(25, settings.max_steps),
        run_dir=root.parent / f"{root.name}.s1",
    )
    f = session.finish or {}
    if not f.get("entry") or not all(str(f.get(k, "")).strip() for k in ("cue", "lesson", "evidence")):
        return None
    return Lesson(
        domain=task.domain, task_id=task.id, cue=str(f["cue"]).strip(),
        lesson=str(f["lesson"]).strip(), evidence=str(f["evidence"]).strip(),
        confidence=str(f.get("confidence", "")).strip(),
    )
