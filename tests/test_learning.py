import time

from fusion_harness import learning, tree
from fusion_harness.domain import Feedback, FeedbackKind, Status, Task, Version


def add_done_task(store, version_id, status=Status.DONE):
    t = store.add_task(Task(id="t1", title="t", body="b", status=status, version_id=version_id))
    return t


def test_seed_on_first_use_and_score_math(store):
    v = learning.current(store, "d")
    assert v.origin == "seed" and v.score == 0.5
    assert Version(id="x", domain="d", tree={}, pos=1, neg=0).score == 2 / 3


def test_child_inherits_positive_evidence_only_and_ties_go_to_newest(store):
    seed = learning.current(store, "d")
    store.reward(seed.id, pos=1.0, neg=1.0)
    time.sleep(0.01)
    t2 = dict(seed.tree, **{"memory.md": "learned"})
    child = learning.derive(store, seed, t2, origin="refine")
    assert (child.pos, child.neg, child.parent) == (1.0, 0.0, seed.id)
    assert learning.current(store, "d").id == child.id
    assert learning.derive(store, seed, seed.tree, origin="refine").id == seed.id
    assert learning.derive(store, seed, t2, origin="other").id == child.id


def test_derive_reads_the_parent_as_stored_not_a_stale_copy(store):
    seed = learning.current(store, "d")
    store.reward(seed.id, pos=0.75)
    child = learning.derive(store, seed, dict(seed.tree, **{"memory.md": "m"}), origin="x")
    assert child.pos == 0.75  # `seed` in memory still says 0


def test_approve_and_reject_score_delivered_versions(store):
    v = learning.current(store, "d")
    add_done_task(store, v.id)
    store.add_feedback(Feedback(task_id="t1", kind=FeedbackKind.APPROVE))
    store.add_feedback(Feedback(task_id="t1", kind=FeedbackKind.REJECT, text="wrong total"))
    actions = learning.apply_pending(store)
    assert "approved" in actions[0] and "reopened" in actions[1]
    v = store.version(v.id)
    assert (v.pos, v.neg) == (1.0, 1.0)
    t = store.task("t1")
    assert t.status is Status.QUEUED and t.inputs == ["[correction from a person] wrong total"]
    assert store.pending_feedback() == []


def test_bare_thumbs_down_scores_but_does_not_reopen(store):
    v = learning.current(store, "d")
    add_done_task(store, v.id)
    store.add_feedback(Feedback(task_id="t1", kind=FeedbackKind.REJECT))
    learning.apply_pending(store)
    assert store.version(v.id).neg == 1.0 and store.task("t1").status is Status.DONE


def test_feedback_on_undelivered_work_does_not_score(store):
    v = learning.current(store, "d")
    add_done_task(store, v.id, status=Status.WAITING)
    store.add_feedback(Feedback(task_id="t1", kind=FeedbackKind.APPROVE))
    assert "ignored" in learning.apply_pending(store)[0]
    assert store.version(v.id).pos == 0


def test_answer_resumes_and_followup_reopens(store):
    store.add_task(Task(id="w", title="t", body="b", status=Status.WAITING, question="Which?",
                        wait_reason="question"))
    store.add_feedback(Feedback(task_id="w", kind=FeedbackKind.ANSWER, text="That one", actor="ana"))
    store.add_task(Task(id="d", title="t", body="b", status=Status.DONE))
    store.add_feedback(Feedback(task_id="d", kind=FeedbackKind.FOLLOWUP, text="also do X"))
    learning.apply_pending(store)
    w, d = store.task("w"), store.task("d")
    assert w.status is Status.QUEUED and w.inputs == ["[answer from ana to: Which?] That one"]
    assert w.wait_reason == ""
    assert d.status is Status.QUEUED and d.inputs == ["[message from a person] also do X"]


def test_feedback_is_recorded_once_per_source_ref(store):
    store.add_task(Task(id="t", title="t", body="b"))
    fb = Feedback(task_id="t", kind=FeedbackKind.APPROVE, source_ref="gh-reaction:1")
    assert store.add_feedback(fb) is not None
    assert store.add_feedback(Feedback(task_id="t", kind=FeedbackKind.APPROVE,
                                       source_ref="gh-reaction:1")) is None


def test_versions_are_content_addressed_per_domain(store):
    t = tree.base_tree()
    a = learning.seed(store, "a", t)
    assert learning.seed(store, "a", t).id == a.id
    assert learning.seed(store, "b", t).id != a.id
