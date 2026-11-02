"""GitHub as a Source and a Channel, through the `gh` CLI.

Intake:   open issues labelled `fh:task` (default) by an allowed person become Tasks. A label
          `domain:<name>` picks the domain; a ```check fenced block in the body is the check.
Feedback: comments by allowed people on a Task's issue (or on its escalation issue) become
          answers, follow-ups, `/approve` or `/reject <why>`; 👍/👎 reactions on the delivery
          comment become approvals and rejections.
Channel:  comments, escalation issues and state labels, all through the outbox.

The agent marks its own comments with a hidden marker instead of relying on a separate bot
account, so it works under the operator's own `gh` login too.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
from typing import Callable

from .config import Settings
from .domain import Feedback, FeedbackKind, Status, Task
from .outbox import Sender
from .signals import MARKER
from .store import Store

Runner = Callable[[list[str], str | None], str]

_CHECK = re.compile(r"```check\s*\n(.*?)\n```", re.S)


def _gh(args: list[str], stdin: str | None = None) -> str:
    proc = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {proc.stderr.strip()[:500]}")
    return proc.stdout


class GitHub:
    def __init__(self, repo: str, run: Runner | None = None):
        if "/" not in repo:
            raise ValueError(f"FH_GITHUB_REPO must be owner/name, got {repo!r}")
        self.repo = repo
        self.run = run or _gh

    def api(self, path: str, *, method: str = "GET", body: dict | None = None):
        args = ["api", "-X", method, path]
        if body is not None:
            args += ["--input", "-"]
        out = self.run(args, json.dumps(body) if body is not None else None)
        return json.loads(out) if out.strip() else {}

    def open_tasks(self, label: str) -> list[dict]:
        q = urllib.parse.quote(label)
        items = self.api(f"repos/{self.repo}/issues?labels={q}&state=open&per_page=100"
                         f"&sort=created&direction=asc")
        return [i for i in items if "pull_request" not in i]

    def comments(self, number: int) -> list[dict]:
        return self.api(f"repos/{self.repo}/issues/{number}/comments?per_page=100")

    def reactions(self, comment_id: str) -> list[dict]:
        return self.api(f"repos/{self.repo}/issues/comments/{comment_id}/reactions?per_page=100")

    def comment(self, number: int, body: str) -> dict:
        r = self.api(f"repos/{self.repo}/issues/{number}/comments", method="POST",
                     body={"body": body})
        return {"comment_id": r.get("id")}

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict:
        r = self.api(f"repos/{self.repo}/issues", method="POST",
                     body={"title": title, "body": body, "labels": labels})
        return {"number": r.get("number")}

    def set_labels(self, number: int, add: list[str], remove: list[str]) -> dict:
        if add:
            self.api(f"repos/{self.repo}/issues/{number}/labels", method="POST",
                     body={"labels": add})
        for label in remove:
            try:
                self.api(f"repos/{self.repo}/issues/{number}/labels/"
                         f"{urllib.parse.quote(label, safe='')}", method="DELETE")
            except RuntimeError:
                pass  # the label was not on the issue
        return {}

    def senders(self) -> dict[str, Sender]:
        def check(repo: str) -> None:
            if repo != self.repo:
                raise RuntimeError(f"message for {repo} but this client serves {self.repo}")

        def comment(p: dict) -> dict:
            check(p["repo"])
            return self.comment(p["issue"], p["body"])

        def issue(p: dict) -> dict:
            check(p["repo"])
            return self.create_issue(p["title"], p["body"], p.get("labels", []))

        def labels(p: dict) -> dict:
            check(p["repo"])
            return self.set_labels(p["issue"], p.get("add", []), p.get("remove", []))

        return {"gh_comment": comment, "gh_issue": issue, "gh_labels": labels}


def owner_of(repo: str) -> str:
    return repo.split("/", 1)[0]


def classify(text: str, task: Task) -> tuple[FeedbackKind, str]:
    body = text.strip()
    low = body.lower()
    if low.startswith("/approve"):
        return FeedbackKind.APPROVE, body[len("/approve"):].strip()
    if low.startswith("/reject"):
        return FeedbackKind.REJECT, body[len("/reject"):].strip()
    if task.status is Status.WAITING:
        return FeedbackKind.ANSWER, body
    return FeedbackKind.FOLLOWUP, body


def poll(store: Store, gh: GitHub, settings: Settings, add_task) -> dict:
    """One intake pass: new Tasks, new comments, new reactions. Idempotent by source refs."""
    allowed = set(settings.allowed_actors or (owner_of(gh.repo),))
    new_tasks = new_feedback = 0

    for issue in gh.open_tasks(settings.task_label):
        ref = f"{gh.repo}#{issue['number']}"
        if store.task_by_source("github", ref) or store.task_by_escalation(ref):
            continue
        if issue.get("user", {}).get("login") not in allowed:
            continue
        labels = [lb["name"] if isinstance(lb, dict) else lb for lb in issue.get("labels", [])]
        domain = next((lb.split(":", 1)[1] for lb in labels if lb.startswith("domain:")),
                      "default")
        body = issue.get("body") or ""
        m = _CHECK.search(body)
        add_task(issue["title"], _CHECK.sub("", body).strip() or issue["title"],
                 check=m.group(1).strip() if m else "", domain=domain, source="github",
                 source_ref=ref, task_id=f"{gh.repo.split('/')[1]}-{issue['number']}")
        new_tasks += 1

    for task in store.tasks():
        threads = []
        if task.source == "github" and task.source_ref.startswith(gh.repo + "#"):
            threads.append(int(task.source_ref.rpartition("#")[2]))
        if task.escalation_ref.startswith(gh.repo + "#"):
            threads.append(int(task.escalation_ref.rpartition("#")[2]))
        for number in threads:
            key = f"gh:cursor:{gh.repo}#{number}"
            cursor = int(store.get(key, "0"))
            last = cursor
            for c in gh.comments(number):
                cid = int(c["id"])
                if cid <= cursor:
                    continue
                last = max(last, cid)
                body = c.get("body") or ""
                who = c.get("user", {}).get("login", "")
                if MARKER in body or who not in allowed or not body.strip():
                    continue
                current = store.task(task.id)
                kind, text = classify(body, current)
                if store.add_feedback(Feedback(task_id=task.id, kind=kind, text=text, actor=who,
                                               source_ref=f"gh-comment:{cid}",
                                               version_id=current.version_id)):
                    new_feedback += 1
            if last != cursor:
                store.set(key, str(last))
        if task.delivery_ref and task.status is Status.DONE:
            for r in gh.reactions(task.delivery_ref):
                who = r.get("user", {}).get("login", "")
                kind = {"+1": FeedbackKind.APPROVE, "-1": FeedbackKind.REJECT}.get(r.get("content"))
                if kind is None or who not in allowed:
                    continue
                if store.add_feedback(Feedback(task_id=task.id, kind=kind, actor=who,
                                               source_ref=f"gh-reaction:{r['id']}",
                                               version_id=task.version_id)):
                    new_feedback += 1
    return {"tasks": new_tasks, "feedback": new_feedback}
