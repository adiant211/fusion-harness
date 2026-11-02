"""What the agent says to people, and where. Following dsh-superagent's rule: speak only when a
person must act or can use a result. There are exactly two messages -- a delivered result and a
request for help -- plus labels, which change state without notifying anyone.

Every message is queued in the outbox under a stable key before it is sent.
"""

from __future__ import annotations

import time

from .config import Settings
from .domain import Task
from .store import Store

MARKER = "<!-- fusion-harness -->"

CATEGORY_TEXT = {
    "missing_access": "needs access or a credential it does not have",
    "missing_info": "needs information it cannot find",
    "decision": "needs a decision that is yours to make",
    "external": "is blocked by something outside its control",
    "stuck": "keeps failing the same way after refining its own approach",
    "budget": "used its attempt budget without finishing",
}

LABELS = ("fh:working", "fh:done", "fh:needs-human")


def _issue(task: Task) -> tuple[str, int] | None:
    ref = task.source_ref if task.source == "github" else ""
    if "#" not in ref:
        return None
    repo, _, number = ref.rpartition("#")
    return repo, int(number)


class Signals:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings

    def _labels(self, task: Task, key: str, add: str) -> None:
        issue = _issue(task)
        if issue:
            self.store.enqueue(
                f"labels:{task.id}:{key}", "gh_labels",
                {"repo": issue[0], "issue": issue[1], "add": [add],
                 "remove": [x for x in LABELS if x != add]},
            )

    def _everywhere(self, key: str, event: dict) -> None:
        self.store.enqueue(f"{key}:file", "file", event)
        if self.settings.webhook_url:
            self.store.enqueue(f"{key}:webhook", "webhook", event)

    def working(self, task: Task) -> None:
        self._labels(task, f"working:{task.attempts}", "fh:working")

    def deliver(self, task: Task, summary: str) -> None:
        key = f"deliver:{task.id}:{task.attempts}"
        issue = _issue(task)
        if issue:
            body = (
                f"**Done:** {summary}\n\n"
                "<sub>Automated agent. React 👍 or 👎 to this comment, or reply "
                "`/reject <what is wrong>`, to teach it.</sub>\n" + MARKER
            )
            self.store.enqueue(key, "gh_comment", {"repo": issue[0], "issue": issue[1],
                               "body": body, "task_id": task.id, "purpose": "delivery"})
            self._labels(task, f"done:{task.attempts}", "fh:done")
        self._everywhere(key, {"event": "done", "task": task.id, "title": task.title,
                               "summary": summary, "at": time.time(),
                               "feedback": f"fh feedback {task.id} --approve | --reject TEXT"})

    def escalate(self, task: Task, *, question: str, category: str, tried: str) -> None:
        key = f"escalate:{task.id}:{task.attempts}"
        why = CATEGORY_TEXT.get(category, "cannot continue without a person")
        lines = [f"**Needs a person:** {question}", "", f"- The agent {why}."]
        if tried.strip():
            lines.append(f"- Already tried: {tried.strip()[:600]}")
        issue = _issue(task)
        if issue:
            lines += ["", "Reply here with the answer and the task resumes on its own.", MARKER]
            self.store.enqueue(key, "gh_comment", {"repo": issue[0], "issue": issue[1],
                               "body": "\n".join(lines), "task_id": task.id,
                               "purpose": "escalation"})
            self._labels(task, f"needs:{task.attempts}", "fh:needs-human")
        elif self.settings.github_repo:
            lines += ["", f"Task `{task.id}`: {task.title}", "",
                      "Reply here with the answer and the task resumes on its own.", MARKER]
            self.store.enqueue(key, "gh_issue", {
                "repo": self.settings.github_repo,
                "title": f"[needs human] {task.title}"[:200],
                "body": "\n".join(lines), "labels": ["fh:needs-human"], "task_id": task.id,
            })
        self._everywhere(key, {"event": "needs_human", "task": task.id, "title": task.title,
                               "question": question, "category": category, "tried": tried,
                               "at": time.time(),
                               "answer": f"fh feedback {task.id} --answer TEXT"})
