"""The domain: a few concepts and the rules each keeps.

Task       a unit of work with a goal, a workspace and, ideally, a check that says when it is done.
Attempt    one run of the worker against a Task under one harness Version.
Verdict    whether an Attempt finished the Task, with the evidence and a failure signature.
Version    an immutable harness tree for one domain, scored by the feedback its results earned.
Feedback   a sparse human signal: an answer, an approval, a rejection or a follow-up.
Lesson     a transferable, evidence-backed note in a domain's library, read by the improver.
Signal     the message sent when a Task cannot continue without a person.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Status(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    DONE = "done"


class WaitReason(str, Enum):
    QUESTION = "question"
    STUCK = "stuck"
    BUDGET = "budget"


class FinishStatus(str, Enum):
    DONE = "done"
    NEED_HUMAN = "need_human"
    IMPOSSIBLE = "impossible"


BLOCKER_CATEGORIES = ("missing_access", "missing_info", "decision", "external")


class FeedbackKind(str, Enum):
    ANSWER = "answer"
    APPROVE = "approve"
    REJECT = "reject"
    FOLLOWUP = "followup"


# How much one signal moves a Version's score. Human judgement dominates; a check passing
# with nobody looking is weak evidence that the harness helped.
FEEDBACK_WEIGHT = {
    FeedbackKind.APPROVE: 1.0,
    FeedbackKind.REJECT: 1.0,
    FeedbackKind.ANSWER: 0.0,
    FeedbackKind.FOLLOWUP: 0.0,
}
VERIFIED_WEIGHT = 0.25


@dataclass
class Task:
    id: str
    title: str
    body: str
    domain: str = "default"
    workspace: str = ""
    check: str = ""
    source: str = "local"
    source_ref: str = ""
    status: Status = Status.QUEUED
    wait_reason: str = ""
    question: str = ""
    escalation_ref: str = ""
    delivery_ref: str = ""
    version_id: str = ""
    notes: str = ""
    inputs: list[str] = field(default_factory=list)
    attempts: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    def prompt(self) -> str:
        parts = [f"# {self.title}", "", self.body.strip()]
        if self.check:
            parts += [
                "",
                "## Done when",
                f"This command exits 0 in the workspace: `{self.check}`",
            ]
        return "\n".join(parts).strip()


@dataclass
class Finish:
    status: FinishStatus
    summary: str
    details: str = ""
    question: str = ""
    category: str = ""
    tried: str = ""

    @classmethod
    def from_input(cls, data: dict) -> "Finish":
        try:
            status = FinishStatus(str(data.get("status", "")).strip())
        except ValueError:
            status = FinishStatus.DONE
        return cls(
            status=status,
            summary=str(data.get("summary", "")).strip(),
            details=str(data.get("details", "")).strip(),
            question=str(data.get("question", "")).strip(),
            category=str(data.get("category", "")).strip(),
            tried=str(data.get("tried", "")).strip(),
        )


@dataclass
class Verdict:
    passed: bool
    evidence: str
    signature: str
    method: str = "check"


@dataclass
class Attempt:
    task_id: str
    n: int
    version_id: str
    finish: Finish | None
    verdict: Verdict | None
    stop: str
    run_dir: str
    trajectory: str = ""
    error: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    id: int | None = None


@dataclass
class Version:
    id: str
    domain: str
    tree: dict[str, str]
    parent: str = ""
    origin: str = "seed"
    notes: str = ""
    pos: float = 0.0
    neg: float = 0.0
    created_at: float = 0.0

    @property
    def score(self) -> float:
        """Posterior mean of a Beta(1, 1) prior updated with weighted feedback."""
        return (1.0 + self.pos) / (2.0 + self.pos + self.neg)


@dataclass
class Feedback:
    task_id: str
    kind: FeedbackKind
    text: str = ""
    actor: str = ""
    source_ref: str = ""
    version_id: str = ""
    id: int | None = None


@dataclass
class Lesson:
    domain: str
    task_id: str
    cue: str
    lesson: str
    evidence: str
    confidence: str = ""
    id: int | None = None
