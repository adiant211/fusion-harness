"""Did an attempt finish the task? A check command decides when the task has one; otherwise a
verifier session judges the workspace itself, never the worker's claims.

Every failing verdict carries a signature: a stable fingerprint of HOW it failed. The loop
compares signatures across attempts to tell progress from a stall.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from . import prompts
from .config import Settings, scrub_env
from .domain import Finish, FinishStatus, Task, Verdict
from .executor import run_session
from .model import Model
from .tools import BASH, READ_FILE, ToolContext, clip, finish_tool

VERIFIER_FINISH = finish_tool(
    "Record the verdict.",
    {
        "passed": {"type": "boolean"},
        "evidence": {"type": "string", "description": "What you checked and what you saw."},
        "unmet": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Short, stable labels for each unmet requirement; empty if passed.",
        },
    },
    ["passed", "evidence"],
)

_NOISE = [
    (re.compile(r"\b\d+(\.\d+)?\s*(ms|s|sec|secs|seconds|minutes)\b"), "<t>"),
    (re.compile(r"0x[0-9a-f]+"), "<addr>"),
    (re.compile(r"/tmp/[^\s'\"]+"), "<tmp>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[t ][\d:.]+z?\b"), "<ts>"),
    (re.compile(r"\s+"), " "),
]


def signature(text: str) -> str:
    """Fingerprint a failure, ignoring timings, addresses, temp paths and timestamps."""
    s = text.lower()
    for rx, sub in _NOISE:
        s = rx.sub(sub, s)
    return hashlib.sha1(s.strip()[-3000:].encode()).hexdigest()[:12]


def run_check(command: str, workspace: Path, timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["bash", "-c", command], cwd=workspace, env=scrub_env(), capture_output=True,
            text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, f"check timed out after {timeout}s"
    return proc.returncode, (proc.stdout + "\n" + proc.stderr).strip()


def verify(
    task: Task,
    finish: Finish | None,
    *,
    workspace: Path,
    settings: Settings,
    model: Model,
    run_dir: Path,
) -> Verdict:
    if finish is not None and finish.status is not FinishStatus.DONE:
        return Verdict(
            passed=False,
            evidence=f"the worker stopped with status={finish.status.value}: {finish.summary}",
            signature=f"worker:{finish.status.value}",
            method="worker",
        )
    if task.check:
        code, out = run_check(task.check, workspace, settings.command_timeout)
        if code == 0:
            return Verdict(True, clip(out, 4000) or "check passed", "pass", "check")
        return Verdict(False, f"`{task.check}` exited {code}\n{clip(out, 6000)}",
                       signature(out or str(code)), "check")

    claim = ""
    if finish is not None:
        claim = f"\n\n## The worker's claim (verify it, do not trust it)\n{finish.summary}\n{finish.details}"
    session = run_session(
        model,
        system=prompts.VERIFIER_SYSTEM,
        prompt=task.prompt() + claim,
        tree={},
        ctx=ToolContext(workspace=workspace, harness_dir=run_dir, timeout=settings.command_timeout,
                        observation_chars=settings.observation_chars),
        finish=VERIFIER_FINISH,
        builtins=(BASH, READ_FILE),
        max_steps=min(30, settings.max_steps),
        run_dir=run_dir / "verifier",
    )
    if not session.finish:
        return Verdict(False, f"the verifier reached no verdict ({session.stop}: {session.error})",
                       "verifier:" + session.stop, "review")
    passed = bool(session.finish.get("passed"))
    unmet = [str(u).strip().lower() for u in session.finish.get("unmet") or [] if str(u).strip()]
    evidence = str(session.finish.get("evidence", "")).strip()
    if passed:
        return Verdict(True, evidence, "pass", "review")
    sig = "unmet:" + "|".join(sorted(set(unmet))) if unmet else signature(evidence)
    detail = evidence + ("\nUnmet: " + "; ".join(unmet) if unmet else "")
    return Verdict(False, detail, sig, "review")
