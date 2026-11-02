"""The task loop: attempt, verify, and between failures refine the harness, until the task is
done or genuinely cannot continue without a person.

    queued -> running -> (attempt -> verify)+ -> done
                                   +-> need a person -> triage -> waiting   (a signal goes out)
    waiting --answer--> queued        done --reject/follow-up--> queued

The loop stops to ask only for one of four reasons, each named in the signal:
  * the worker reports a blocker AND an independent triage pass confirms nobody but a person
    can resolve it (a resolvable "blocker" comes back as a hint instead);
  * the model provider rejects the credentials or model;
  * the same failure signature repeats `stall_limit` times despite refinement (stuck);
  * the attempt budget runs out.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from . import improver, learning, prompts, signals
from . import tree as harness_tree
from .config import Settings, scrub_env
from .domain import (
    VERIFIED_WEIGHT,
    Attempt,
    Finish,
    FinishStatus,
    Status,
    Task,
    Verdict,
    Version,
    WaitReason,
)
from .executor import Session, run_session
from .model import Model
from .outbox import Sender, file_sender, flush, webhook_sender
from .store import Store
from .tools import BASH, READ_FILE, WORKER_FINISH, ToolContext, clip, finish_tool
from .verify import signature, verify

TRIAGE_FINISH = finish_tool(
    "Record the triage.",
    {
        "resolvable": {"type": "boolean"},
        "approach": {"type": "string", "description": "If resolvable: what to do instead."},
        "question": {"type": "string", "description": "If not: the sharpened question."},
    },
    ["resolvable"],
)


def slug(text: str, n: int = 32) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:n].strip("-") or "task"


class PrepareError(RuntimeError):
    pass


class Runner:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        model: Model,
        improver_model: Model | None = None,
        *,
        senders: dict[str, Sender] | None = None,
        log=print,
    ):
        self.s = settings
        self.store = store
        self.model = model
        self.improver = improver_model or model
        self.signals = signals.Signals(store, settings)
        self.senders = {"file": file_sender(settings.home / "signals.jsonl")}
        if settings.webhook_url:
            self.senders["webhook"] = webhook_sender(settings.webhook_url)
        self.senders.update(senders or {})
        self.log = log

    # ------------------------------------------------------------ public API

    def add(self, title: str, body: str = "", *, workspace: str = "", check: str = "",
            domain: str = "default", source: str = "local", source_ref: str = "",
            task_id: str = "") -> Task:
        tid = task_id or f"{slug(title)}-{os.urandom(2).hex()}"
        return self.store.add_task(Task(
            id=tid, title=title.strip(), body=(body or title).strip(), domain=domain or "default",
            workspace=str(Path(workspace).resolve()) if workspace else "", check=check.strip(),
            source=source, source_ref=source_ref,
        ))

    def process_feedback(self) -> list[str]:
        actions = learning.apply_pending(self.store)
        for a in actions:
            self.log(f"feedback: {a}")
        return actions

    def flush(self) -> dict:
        return flush(self.store, self.senders)

    def run_queued(self, limit: int | None = None) -> list[Task]:
        self.process_feedback()
        out = []
        for task in self.store.tasks(Status.QUEUED)[:limit]:
            out.append(self.run(task.id))
            self.flush()
        self.flush()
        return out

    def run(self, task_id: str) -> Task:
        task = self.store.task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status is not Status.QUEUED:
            return task
        task.status = Status.RUNNING
        self.store.save_task(task)
        self.signals.working(task)
        self.log(f"[{task.id}] running: {task.title}")
        try:
            workspace = self._workspace(task)
        except PrepareError as exc:
            return self._escalate(task, None, question=f"Preparing the workspace failed: {exc}. "
                                  f"Please fix FH_PREPARE or the workspace, then reply.",
                                  category="external", tried="", reason=WaitReason.QUESTION)

        # A task resuming after an escalation continues on the harness it had refined to. New
        # tasks, and delivered tasks reopened by feedback, run the domain's best version, so
        # what they learn lands on the main line rather than on a stale branch.
        delivered_before = any(a.verdict and a.verdict.passed for a in self.store.attempts(task.id))
        own = self.store.version(task.version_id) if task.version_id else None
        version = own if (own and not delivered_before) else \
            learning.current(self.store, task.domain)
        inputs, task.inputs = list(task.inputs), []
        self.store.save_task(task)

        if any(i.startswith("[correction") for i in inputs):
            version, _ = self._improve(task, version, workspace, inputs, origin="correction")

        triaged: set[str] = set()
        sigs: list[str] = []
        hint = ""
        attempts_budget = max(1, self.s.max_attempts)
        for i in range(attempts_budget):
            att, session = self._attempt(task, version, workspace, inputs, hint)
            hint = ""
            if att.verdict and att.verdict.passed:
                return self._deliver(task, version, att)
            if session.needs_human:
                return self._escalate(task, version, question=f"The model provider needs attention: "
                                      f"{session.error[:300]}", category="missing_access",
                                      tried="", reason=WaitReason.QUESTION)
            f = att.finish
            if f and f.status in (FinishStatus.NEED_HUMAN, FinishStatus.IMPOSSIBLE):
                question = f.question or f.summary
                key = re.sub(r"\W+", " ", question.lower()).strip()
                if key not in triaged:
                    triaged.add(key)
                    tri = self._triage(task, att, workspace)
                    if tri.get("resolvable") and str(tri.get("approach", "")).strip():
                        hint = str(tri["approach"]).strip()
                        self.log(f"[{task.id}] triage: resolvable without a person")
                        continue
                    question = str(tri.get("question") or "").strip() or question
                category = f.category or ("decision" if f.status is FinishStatus.IMPOSSIBLE
                                          else "missing_info")
                return self._escalate(task, version, question=question, category=category,
                                      tried=f.tried, reason=WaitReason.QUESTION)
            outage = bool(att.verdict and att.verdict.method == "error")
            sigs.append(att.verdict.signature if att.verdict else "none")
            limit = max(2, self.s.stall_limit)
            if len(sigs) >= limit and len(set(sigs[-limit:])) == 1:
                return self._escalate(task, version, question=self._stuck_question(att, limit),
                                      category="external" if outage else "stuck",
                                      tried=self._tried(task), reason=WaitReason.STUCK)
            if i == attempts_budget - 1:
                break
            if outage:
                continue  # the provider failed; editing the harness cannot fix that
            version, passed = self._improve(task, version, workspace, inputs, origin="refine")
            if passed is not None:
                return self._deliver(task, version, passed)
        return self._escalate(
            task, version,
            question=(f"I used all {attempts_budget} attempts without passing. The last "
                      f"failure: {self._first_line(att)}. Should I keep going, change "
                      f"approach, or drop the task?"),
            category="budget", tried=self._tried(task), reason=WaitReason.BUDGET,
        )

    # ------------------------------------------------------------- attempts

    def _workspace(self, task: Task) -> Path:
        if task.workspace:
            p = Path(task.workspace)
            p.mkdir(parents=True, exist_ok=True)
            return p
        p = (self.s.workspace_root / task.id).resolve()
        fresh = not p.exists()
        p.mkdir(parents=True, exist_ok=True)
        if fresh and self.s.prepare:
            env = scrub_env({"FH_TASK_ID": task.id, "FH_GITHUB_REPO": self.s.github_repo})
            proc = subprocess.run(["sh", "-c", self.s.prepare], cwd=p, env=env,
                                  capture_output=True, text=True, timeout=600)
            if proc.returncode != 0:
                shutil.rmtree(p, ignore_errors=True)
                raise PrepareError(clip(proc.stdout + proc.stderr, 1500))
        task.workspace = str(p)
        self.store.save_task(task)
        return p

    def _worker_prompt(self, task: Task, inputs: list[str], hint: str) -> str:
        parts = [task.prompt()]
        if task.notes.strip():
            parts += ["## Notes on this workspace from earlier attempts", task.notes.strip()]
        earlier = self.store.attempts(task.id)[-3:]
        if earlier:
            rows = ["## Earlier attempts on this task"]
            for a in earlier:
                said = f"worker said {a.finish.status.value}: {a.finish.summary}" if a.finish \
                    else f"stopped: {a.stop}"
                rows.append(f"- Attempt {a.n}: {said}")
                if a.verdict and not a.verdict.passed:
                    rows.append("  Verdict: FAILED\n  " + clip(a.verdict.evidence, 1500)
                                .replace("\n", "\n  "))
            parts.append("\n".join(rows))
        if inputs:
            parts += ["## From a person (this outranks your own plans)",
                      "\n".join(f"- {x}" for x in inputs)]
        if hint:
            parts += ["## Suggested approach (from a review of your blocker)", hint]
        return "\n\n".join(parts)

    def _attempt(self, task: Task, version: Version, workspace: Path, inputs: list[str],
                 hint: str, *, label: str = "") -> tuple[Attempt, Session]:
        n = task.attempts + 1
        run_dir = self.s.runs_dir / task.id / (f"attempt-{n:02d}" + (f"-{label}" if label else ""))
        harness_dir = run_dir / "harness"
        harness_tree.write_tree(version.tree, harness_dir)
        system = harness_tree.always_on_text(version.tree) + "\n\n" + prompts.WORKER_FRAME.format(
            workspace=workspace, harness_dir=harness_dir)
        ctx = ToolContext(workspace=workspace, harness_dir=harness_dir,
                          timeout=self.s.command_timeout,
                          observation_chars=self.s.observation_chars,
                          env={"FH_TASK_ID": task.id})
        session = run_session(self.model, system=system,
                              prompt=self._worker_prompt(task, inputs, hint), tree=version.tree,
                              ctx=ctx, finish=WORKER_FINISH, max_steps=self.s.max_steps,
                              run_dir=run_dir)
        finish = Finish.from_input(session.finish) if session.finish else None
        if session.stop == "error" and finish is None:
            verdict = Verdict(False, f"the attempt failed: {session.error[:1000]}",
                              signature("error " + session.error[:200]), "error")
        else:
            verdict = verify(task, finish, workspace=workspace, settings=self.s,
                             model=self.improver, run_dir=run_dir)
        att = Attempt(task_id=task.id, n=n, version_id=version.id, finish=finish, verdict=verdict,
                      stop=session.stop, run_dir=str(run_dir), trajectory=session.rendered(),
                      error=session.error, tokens_in=session.usage.input_tokens,
                      tokens_out=session.usage.output_tokens)
        self.store.add_attempt(att)
        task.attempts = n
        self.store.save_task(task)
        self.log(f"[{task.id}] attempt {n} ({version.id}): "
                 f"{'passed' if verdict.passed else 'failed'} -- {self._first_line(att)}")
        return att, session

    # ------------------------------------------------------------ improvement

    def _improve(self, task: Task, version: Version, workspace: Path, inputs: list[str], *,
                 origin: str) -> tuple[Version, Attempt | None]:
        """Refine the harness from this task's evidence. With probe_width > 1, probe several
        candidates in copies of the workspace; a probe that passes is adopted outright."""
        attempts = self.store.attempts(task.id)
        feedback = [f for f in self.store.feedback_for(task.id) if f.text]
        lessons = self.store.lessons(task.domain)
        base = self.s.runs_dir / task.id / f"improve-{task.attempts:02d}-{origin}"
        common = dict(task=task, tree=version.tree, attempts=attempts, feedback=feedback,
                      lessons=lessons)
        width = max(1, self.s.probe_width)
        if width == 1:
            p = improver.propose(self.improver, self.s, dim="ALL", root=base / "ctx", **common)
            return self._adopt(task, version, p, origin), None

        dims = (["INV", "DOM", "COMM"] * width)[:width]
        cands = []
        for k, dim in enumerate(dims, 1):
            p = improver.propose(self.improver, self.s, dim=dim, root=base / f"ctx-{dim.lower()}{k}",
                                 **common)
            cands.append(improver.Candidate(label=f"{dim.lower()}{k}", proposal=p))
        for c in cands:
            if not c.proposal.changed:
                continue
            probe_ws = base / f"probe-{c.label}"
            shutil.rmtree(probe_ws, ignore_errors=True)
            shutil.copytree(workspace, probe_ws, symlinks=True)
            v = learning.derive(self.store, version, c.proposal.tree, origin="probe",
                                notes=c.proposal.notes)
            saved_notes, task.notes = task.notes, c.proposal.task_notes
            att, _ = self._attempt(task, v, probe_ws, inputs, "", label=c.label)
            c.attempt = att
            if att.verdict and att.verdict.passed:
                _sync(probe_ws, workspace)
                self.store.save_task(task)
                return v, att
            task.notes = saved_notes
        p = improver.propose(self.improver, self.s, dim="COMPOSE", root=base / "ctx-compose",
                             candidates=cands, **{**common, "attempts": self.store.attempts(task.id)})
        return self._adopt(task, version, p, origin), None

    def _adopt(self, task: Task, version: Version, p: improver.Proposal, origin: str) -> Version:
        task.notes = p.task_notes
        self.store.save_task(task)
        if p.error:
            self.log(f"[{task.id}] improver kept the harness: {p.error[:200]}")
        if not p.changed:
            return version
        v = learning.derive(self.store, version, p.tree, origin=origin, notes=p.notes)
        d = harness_tree.diff_summary(version.tree, p.tree)
        self.log(f"[{task.id}] harness {version.id} -> {v.id} ({origin}): "
                 f"+{d['added']} ~{d['modified']} -{d['removed']}")
        return v

    def _triage(self, task: Task, att: Attempt, workspace: Path) -> dict:
        f = att.finish
        prompt = "\n\n".join([
            task.prompt(),
            "## The worker's blocker",
            f"question: {f.question}\ncategory: {f.category}\ntried: {f.tried}\nsummary: {f.summary}",
            "## The end of its trajectory",
            att.trajectory[-6000:],
            prompts.EVIDENCE_IS_DATA,
        ])
        run_dir = Path(att.run_dir) / "triage"
        session = run_session(
            self.improver, system=prompts.TRIAGE_SYSTEM, prompt=prompt, tree={},
            ctx=ToolContext(workspace=workspace, harness_dir=Path(att.run_dir) / "harness",
                            timeout=self.s.command_timeout),
            finish=TRIAGE_FINISH, builtins=(BASH, READ_FILE), max_steps=12, run_dir=run_dir,
        )
        return session.finish or {"resolvable": False}

    # ------------------------------------------------------------ endings

    def _deliver(self, task: Task, version: Version, att: Attempt) -> Task:
        task.status = Status.DONE
        task.version_id = version.id
        task.wait_reason = task.question = ""
        self.store.save_task(task)
        self.store.reward(version.id, pos=VERIFIED_WEIGHT)
        summary = (att.finish.summary if att.finish and att.finish.summary
                   else f"The task passes its check (`{task.check}`)." if task.check
                   else "The task is complete.")
        self.signals.deliver(task, summary)
        self.log(f"[{task.id}] done after {task.attempts} attempt(s) with harness {version.id}")
        self._learn(task, version)
        return self.store.task(task.id)

    def _learn(self, task: Task, version: Version) -> None:
        """After a delivery: harnessing-level recursion. Consolidate what people taught, and any
        in-task refinements the domain's best harness lacks, into that best harness; then write
        a lesson to the domain library when the record holds a contrast worth keeping."""
        attempts = self.store.attempts(task.id)
        people = [f for f in self.store.feedback_for(task.id) if f.text]
        champion = learning.current(self.store, task.domain)
        base = self.s.runs_dir / task.id / f"learn-{task.attempts:02d}"
        if people or champion.id != version.id:
            delivered = []
            if champion.id != version.id:
                delivered = [improver.Candidate(
                    label="delivered",
                    proposal=improver.Proposal(tree=version.tree, task_notes="",
                                               notes=version.notes),
                    attempt=attempts[-1] if attempts else None)]
            p = improver.propose(self.improver, self.s, task=task, tree=champion.tree,
                                 attempts=attempts[-3:], feedback=people,
                                 lessons=self.store.lessons(task.domain), dim="CONSOLIDATE",
                                 root=base / "consolidate", candidates=delivered)
            if p.changed:
                v = learning.derive(self.store, champion, p.tree, origin="consolidate",
                                    notes=p.notes)
                self.log(f"[{task.id}] consolidated into the domain harness: {champion.id} -> {v.id}")
        if len(attempts) >= 2 or people:
            lesson = improver.reflect(self.improver, self.s, task=task, tree=version.tree,
                                      attempts=attempts, feedback=people, root=base / "reflect")
            known = {(le.cue, le.lesson) for le in self.store.lessons(task.domain)}
            if lesson and (lesson.cue, lesson.lesson) not in known:
                self.store.add_lesson(lesson)
                self.log(f"[{task.id}] library lesson: {lesson.cue}")

    def _escalate(self, task: Task, version: Version | None, *, question: str, category: str,
                  tried: str, reason: WaitReason) -> Task:
        task.status = Status.WAITING
        if version is not None:
            task.version_id = version.id
        task.wait_reason = reason.value
        task.question = question.strip()
        self.store.save_task(task)
        self.signals.escalate(task, question=task.question, category=category, tried=tried)
        self.log(f"[{task.id}] waiting for a person ({category}): {task.question}")
        return self.store.task(task.id)

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _first_line(att: Attempt) -> str:
        text = att.verdict.evidence if att.verdict else att.error
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        return clip(" / ".join(lines[:3]), 300) or att.stop

    def _stuck_question(self, att: Attempt, limit: int) -> str:
        return (f"The last {limit} attempts failed the same way even after I refined my own "
                f"approach: {self._first_line(att)}. What am I missing?")

    def _tried(self, task: Task) -> str:
        rows = []
        for a in self.store.attempts(task.id)[-4:]:
            if a.finish:
                rows.append(f"attempt {a.n}: {a.finish.summary}")
        return "; ".join(rows)[:600]


def _sync(src: Path, dst: Path) -> None:
    """Make `dst` hold exactly what `src` holds (used to adopt a passing probe's workspace)."""
    for p in sorted(dst.rglob("*"), reverse=True):
        rel = p.relative_to(dst)
        if not (src / rel).exists() and not (src / rel).is_symlink():
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
    shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
