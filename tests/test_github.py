import json

from conftest import Roles, finish, make_runner

from fusion_harness.domain import FeedbackKind, Status
from fusion_harness.github import GitHub, poll
from fusion_harness.signals import MARKER


class FakeGH:
    """A tiny in-memory GitHub behind the `gh api` command line."""

    def __init__(self):
        self.issues = {}
        self.comments = {}
        self.reactions = {}
        self.labels = {}
        self.calls = []
        self.next_id = 1000

    def issue(self, number, title, body, user="owner", labels=("fh:task",)):
        self.issues[number] = {"number": number, "title": title, "body": body,
                               "user": {"login": user},
                               "labels": [{"name": x} for x in labels]}
        self.comments.setdefault(number, [])

    def human_comment(self, number, body, user="owner"):
        self.next_id += 1
        self.comments[number].append({"id": self.next_id, "body": body, "user": {"login": user}})
        return self.next_id

    def __call__(self, args, stdin):
        assert args[0] == "api"
        method, path = args[2], args[3]
        body = json.loads(stdin) if stdin else None
        self.calls.append((method, path, body))
        parts = path.split("?")[0].split("/")
        if method == "GET" and parts[3] == "issues" and len(parts) == 4:
            return json.dumps(list(self.issues.values()))
        if parts[3] == "issues" and len(parts) == 4 and method == "POST":
            n = max(self.issues, default=0) + 1
            self.issue(n, body["title"], body["body"], user="agent", labels=body["labels"])
            return json.dumps({"number": n})
        if parts[3] == "issues" and parts[-1] == "comments":
            n = int(parts[4])
            if method == "GET":
                return json.dumps(self.comments.get(n, []))
            self.next_id += 1
            self.comments.setdefault(n, []).append(
                {"id": self.next_id, "body": body["body"], "user": {"login": "owner"}})
            return json.dumps({"id": self.next_id})
        if parts[-1] == "reactions":
            return json.dumps(self.reactions.get(parts[-2], []))
        if "labels" in parts:
            n = int(parts[4])
            if method == "POST":
                self.labels.setdefault(n, set()).update(body["labels"])
            else:
                self.labels.setdefault(n, set()).discard(parts[-1].replace("%3A", ":"))
            return ""
        raise AssertionError(f"unexpected gh call {method} {path}")


def setup(settings, store, worker):
    gh_fake = FakeGH()
    gh = GitHub("owner/repo", run=gh_fake)
    s = settings.with_overrides(github_repo="owner/repo")
    r = make_runner(s, store, Roles(worker), senders=gh.senders())
    return gh_fake, gh, s, r


def test_intake_filters_by_label_author_and_parses_check_and_domain(settings, store):
    fake, gh, s, r = setup(settings, store, lambda *a: finish(status="done", summary="ok"))
    fake.issue(1, "Fix it", "Please fix.\n```check\ntrue\n```", labels=("fh:task", "domain:web"))
    fake.issue(2, "Spam", "do my homework", user="stranger")
    assert poll(store, gh, s, r.add) == {"tasks": 1, "feedback": 0}
    assert poll(store, gh, s, r.add)["tasks"] == 0  # idempotent
    [t] = store.tasks()
    assert (t.id, t.domain, t.check, t.body) == ("repo-1", "web", "true", "Please fix.")
    assert t.source_ref == "owner/repo#1"


def test_delivery_comment_labels_and_reaction_feedback(settings, store):
    fake, gh, s, r = setup(settings, store, lambda *a: finish(status="done", summary="Fixed it."))
    fake.issue(1, "Fix it", "```check\ntrue\n```")
    poll(store, gh, s, r.add)
    r.run_queued()
    t = store.task("repo-1")
    assert t.status is Status.DONE and t.delivery_ref
    bot = fake.comments[1][-1]
    assert bot["body"].startswith("**Done:** Fixed it.") and MARKER in bot["body"]
    assert fake.labels[1] == {"fh:done"}

    fake.reactions[t.delivery_ref] = [{"id": 7, "content": "+1", "user": {"login": "owner"}},
                                      {"id": 8, "content": "-1", "user": {"login": "stranger"}}]
    assert poll(store, gh, s, r.add)["feedback"] == 1  # own marker comment skipped, stranger ignored
    assert poll(store, gh, s, r.add)["feedback"] == 0
    r.process_feedback()
    assert store.version(t.version_id).pos == 1.25


def test_escalation_on_issue_then_answer_by_comment(settings, store):
    def worker(step, system, prompt):
        if "[answer from owner" in prompt:
            return finish(status="done", summary="Used staging.")
        return finish(status="need_human", summary="unsure", question="Staging or prod?",
                      category="decision")

    fake, gh, s, r = setup(settings, store, worker)
    fake.issue(3, "Deploy", "Deploy the app.\n```check\ntrue\n```")
    poll(store, gh, s, r.add)
    r.run_queued()
    t = store.task("repo-3")
    assert t.status is Status.WAITING
    assert "**Needs a person:** sharpened question?" in fake.comments[3][-1]["body"]
    assert fake.labels[3] == {"fh:needs-human"}

    fake.human_comment(3, "Use staging.")
    assert poll(store, gh, s, r.add)["feedback"] == 1
    [fb] = store.feedback_for(t.id)
    assert fb.kind is FeedbackKind.ANSWER and fb.actor == "owner"
    r.run_queued()
    assert store.task("repo-3").status is Status.DONE
    fake.human_comment(3, "/reject wrong environment")
    poll(store, gh, s, r.add)
    assert store.feedback_for(t.id)[-1].kind is FeedbackKind.REJECT


def test_local_task_escalates_by_opening_an_issue(settings, store, tmp_path):
    def worker(step, system, prompt):
        if "[answer from" in prompt:
            return finish(status="done", summary="ok")
        return finish(status="need_human", summary="x", question="Key?", category="missing_access")

    fake, gh, s, r = setup(settings, store, worker)
    (tmp_path / "w").mkdir()
    t = r.add("local job", workspace=str(tmp_path / "w"), check="true")
    r.run(t.id)
    r.flush()
    t = store.task(t.id)
    [issue] = fake.issues.values()
    assert issue["title"] == "[needs human] local job" and t.escalation_ref == "owner/repo#1"
    assert poll(store, gh, s, r.add)["tasks"] == 0  # the escalation issue is not a new task
    fake.human_comment(1, "Use the vault key.")
    poll(store, gh, s, r.add)
    r.run_queued()
    assert store.task(t.id).status is Status.DONE
