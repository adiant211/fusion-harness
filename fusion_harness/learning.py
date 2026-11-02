"""Harnessing-level recursion: which harness Version a domain runs, and how sparse human
feedback moves it.

Each Version keeps a Beta(1, 1) posterior over "this harness produces results people accept",
updated with weighted signals: an approval or rejection counts 1, a check passing with nobody
looking counts 0.25. A domain runs its highest-scoring Version; ties go to the newest, so a fresh
refinement gets tried. A Version derived from another inherits its parent's positive evidence
but not its negatives: it was made to fix what went wrong, so it gets the benefit of the doubt
until feedback says otherwise.
"""

from __future__ import annotations

from . import tree as harness_tree
from .domain import FEEDBACK_WEIGHT, Feedback, FeedbackKind, Status, Version
from .store import Store


def seed(store: Store, domain: str, tree: dict[str, str] | None = None) -> Version:
    tree = tree if tree is not None else harness_tree.base_tree()
    return store.add_version(
        Version(id=harness_tree.fingerprint(domain, tree), domain=domain, tree=tree, origin="seed")
    )


def current(store: Store, domain: str) -> Version:
    versions = store.versions(domain)
    if not versions:
        return seed(store, domain)
    return max(versions, key=lambda v: (round(v.score, 9), v.created_at))


def derive(store: Store, parent: Version, tree: dict[str, str], *, origin: str,
           notes: str = "") -> Version:
    parent = store.version(parent.id) or parent  # inherit the parent's evidence as stored now
    vid = harness_tree.fingerprint(parent.domain, tree)
    if vid == parent.id:
        return parent
    existing = store.version(vid)
    if existing:
        return existing
    return store.add_version(
        Version(id=vid, domain=parent.domain, tree=tree, parent=parent.id, origin=origin,
                notes=notes[:4000], pos=parent.pos, neg=0.0)
    )


def apply(store: Store, fb: Feedback) -> str:
    """Turn one feedback item into state changes; return what happened, for the log."""
    task = store.task(fb.task_id)
    if task is None:
        store.mark_processed(fb.id)
        return f"feedback {fb.id} names an unknown task"
    who = fb.actor or "a person"
    weight = FEEDBACK_WEIGHT[fb.kind]
    action = ""
    if fb.kind is FeedbackKind.APPROVE:
        if task.version_id and task.status is Status.DONE:
            store.reward(task.version_id, pos=weight)
            action = f"approved: harness {task.version_id} +{weight}"
        else:
            action = "approval ignored: nothing was delivered yet"
    elif fb.kind is FeedbackKind.REJECT:
        if task.version_id and task.status is Status.DONE:
            store.reward(task.version_id, neg=weight)
        if fb.text.strip():
            task.inputs.append(f"[correction from {who}] {fb.text.strip()}")
            task.status = Status.QUEUED
            action = f"rejected: harness {task.version_id or '-'} -{weight}; task reopened"
        else:
            # A bare thumbs-down scores the harness but carries nothing to act on; rerunning
            # the same task without new information would only repeat the work.
            action = f"rejected: harness {task.version_id or '-'} -{weight}"
    elif fb.kind is FeedbackKind.ANSWER and task.status is Status.WAITING:
        task.inputs.append(f"[answer from {who} to: {task.question}] {fb.text}")
        task.status = Status.QUEUED
        task.wait_reason = ""
        action = "answered: task resumes"
    else:
        task.inputs.append(f"[message from {who}] {fb.text}")
        if task.status in (Status.DONE, Status.WAITING):
            task.status = Status.QUEUED
            task.wait_reason = ""
        action = "follow-up queued for the task"
    store.save_task(task)
    store.mark_processed(fb.id)
    return action


def apply_pending(store: Store) -> list[str]:
    return [apply(store, fb) for fb in store.pending_feedback()]
