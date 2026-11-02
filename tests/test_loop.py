from pathlib import Path

from conftest import Roles, finish, keep, make_runner, tool

from fusion_harness import learning
from fusion_harness.domain import Feedback, FeedbackKind, Status
from fusion_harness.model import ModelError


def ws(tmp_path, name="ws", files=None):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    for rel, text in (files or {}).items():
        (d / rel).write_text(text)
    return str(d)


def signals(settings):
    p = settings.home / "signals.jsonl"
    return p.read_text().splitlines() if p.exists() else []


def test_passes_first_try_and_rewards_harness(tmp_path, settings, store):
    def worker(step, system, prompt):
        return [[tool("write_file", path="out.txt", content="ok")],
                finish(status="done", summary="Wrote it.")][min(step, 1)]

    r = make_runner(settings, store, Roles(worker))
    t = r.add("write ok", workspace=ws(tmp_path), check="grep -q ok out.txt")
    t = r.run(t.id)
    r.flush()
    assert t.status is Status.DONE and t.attempts == 1
    v = store.version(t.version_id)
    assert v.origin == "seed" and v.pos == 0.25
    assert '"event": "done"' in signals(settings)[0]


def test_refines_between_failures_and_carries_task_notes(tmp_path, settings, store):
    def worker(step, system, prompt):
        if "Always write 'ok'" in system:
            return [[tool("write_file", path="out.txt", content="ok")],
                    finish(status="done", summary="done")][min(step, 1)]
        return finish(status="done", summary="claimed")

    def improver(step, system, prompt):
        assert "attempts/" in prompt and "task_notes.md" in prompt
        return [[tool("bash", command="echo \"- Always write 'ok' to out.txt.\" >> harness/guardrails.md")],
                [tool("write_file", path="task_notes.md", content="out.txt is the target")],
                finish(status="changed", notes="guardrails: attempt 1 never wrote out.txt")][min(step, 2)]

    roles = Roles(worker, improver)
    r = make_runner(settings, store, roles)
    t = r.add("write ok", workspace=ws(tmp_path), check="grep -q ok out.txt")
    t = r.run(t.id)
    assert t.status is Status.DONE and t.attempts == 2
    assert t.notes == "out.txt is the target"
    v = store.version(t.version_id)
    assert v.origin == "refine" and v.parent and "Always write" in v.tree["guardrails.md"]
    assert learning.current(store, "default").id == v.id
    worker_prompts = [p for role, p in roles.seen if role == "worker"]
    assert "out.txt is the target" in worker_prompts[1] and "Earlier attempts" in worker_prompts[1]


def test_invalid_proposal_gets_one_retry_then_harness_is_kept(tmp_path, settings, store):
    def improver(step, system, prompt):
        return [[tool("write_file", path="harness/skills/lint/SKILL.md", content="no frontmatter")],
                finish(status="changed", notes="x")][step % 2]

    roles = Roles(lambda *a: finish(status="done", summary="no"), improver)
    settings = settings.with_overrides(max_attempts=2)
    r = make_runner(settings, store, roles)
    t = r.add("never passes", workspace=ws(tmp_path), check="false")
    r.run(t.id)
    assert roles.count("improver") == 4  # two sessions per proposal, each 2 turns
    assert len(store.versions("default")) == 1


def test_stall_escalates_as_stuck(tmp_path, settings, store):
    r = make_runner(settings, store, Roles(lambda *a: finish(status="done", summary="claimed")))
    t = r.add("impossible check", workspace=ws(tmp_path), check="echo same failure; false")
    t = r.run(t.id)
    r.flush()
    assert t.status is Status.WAITING and t.wait_reason == "stuck" and t.attempts == 3
    assert "failed the same way" in t.question
    assert '"category": "stuck"' in signals(settings)[-1]


def test_budget_escalation_when_failures_differ(tmp_path, settings, store):
    r = make_runner(settings.with_overrides(max_attempts=2),
                    store, Roles(lambda *a: finish(status="done", summary="claimed")))
    t = r.add("moving target", workspace=ws(tmp_path), check="date +%N; false")
    t = r.run(t.id)
    assert t.status is Status.WAITING and t.wait_reason == "budget"


def test_resolvable_blocker_returns_as_a_hint(tmp_path, settings, store):
    def worker(step, system, prompt):
        if "Suggested approach" in prompt:
            return [[tool("bash", command="cp config.example config.ini")],
                    finish(status="done", summary="Configured.")][min(step, 1)]
        return finish(status="need_human", summary="no config", question="What config do I use?",
                      category="missing_info")

    def triage(step, system, prompt):
        return finish(resolvable=True, approach="Copy config.example to config.ini.")

    roles = Roles(worker, triage=triage)
    r = make_runner(settings, store, roles)
    t = r.add("configure", workspace=ws(tmp_path, files={"config.example": "x"}),
              check="test -f config.ini")
    t = r.run(t.id)
    assert t.status is Status.DONE and roles.count("triage") == 1
    assert not any('"needs_human"' in s for s in signals(settings))


def test_genuine_blocker_escalates_then_answer_resumes(tmp_path, settings, store):
    def worker(step, system, prompt):
        if "[answer from" in prompt:
            return [[tool("bash", command="echo $(cat token) > done.txt")],
                    finish(status="done", summary="Done with the token.")][min(step, 1)]
        return finish(status="need_human", summary="needs token", question="Which token?",
                      category="missing_access", tried="looked in env")

    roles = Roles(worker)
    r = make_runner(settings, store, roles)
    w = ws(tmp_path)
    t = r.add("use the token", workspace=w, check="grep -q secret done.txt")
    t = r.run(t.id)
    r.flush()
    assert t.status is Status.WAITING and t.question == "sharpened question?"
    sig = signals(settings)[-1]
    assert '"needs_human"' in sig and "missing_access" in sig
    assert r.run(t.id).status is Status.WAITING  # nothing happens until a person answers

    Path(w, "token").write_text("secret")
    store.add_feedback(Feedback(task_id=t.id, kind=FeedbackKind.ANSWER, text="It is in ./token",
                                actor="ana"))
    done = r.run_queued()
    assert [x.status for x in done] == [Status.DONE]
    assert any("[answer from ana to: sharpened question?] It is in ./token" in p
               for role, p in roles.seen if role == "worker")


def test_provider_credential_failure_escalates_at_once(tmp_path, settings, store):
    class Broken:
        name = "broken"

        def turn(self, *a):
            raise ModelError("401 invalid x-api-key", needs_human=True)

    from fusion_harness.loop import Runner

    r = Runner(settings, store, Broken(), log=lambda m: None)
    t = r.add("anything", workspace=ws(tmp_path), check="true")
    t = r.run(t.id)
    assert t.status is Status.WAITING and "401" in t.question and t.attempts == 1


def test_probe_width_adopts_a_passing_probe(tmp_path, settings, store):
    def worker(step, system, prompt):
        if "Write ok" in system:
            return [[tool("write_file", path="out.txt", content="ok")],
                    finish(status="done", summary="done")][min(step, 1)]
        return finish(status="done", summary="claimed")

    def improver(step, system, prompt):
        if "YOUR DIMENSION: INV" in prompt:
            return [[tool("bash", command="echo '- Write ok to out.txt.' >> harness/guardrails.md")],
                    finish(status="changed", notes="INV")][min(step, 1)]
        return keep(step, system, prompt)

    roles = Roles(worker, improver)
    r = make_runner(settings.with_overrides(probe_width=2), store, roles)
    w = ws(tmp_path)
    t = r.add("write ok", workspace=w, check="grep -q ok out.txt")
    t = r.run(t.id)
    assert t.status is Status.DONE
    assert Path(w, "out.txt").read_text() == "ok"  # the probe's workspace was adopted
    assert store.version(t.version_id).origin == "probe"


def test_correction_reopens_task_and_refines_first(tmp_path, settings, store):
    def worker(step, system, prompt):
        content = "ok\nTITLE" if "[correction from" in prompt else "ok"
        return [[tool("write_file", path="out.txt", content=content)],
                finish(status="done", summary="written")][min(step, 1)]

    def improver(step, system, prompt):
        if "people said (reject)" in prompt and "CONSOLIDATE" not in prompt:
            return [[tool("bash", command="echo '- Files need a TITLE line.' >> harness/memory.md")],
                    finish(status="changed", notes="memory: a person rejected for no title")][min(step, 1)]
        return keep(step, system, prompt)

    roles = Roles(worker, improver)
    r = make_runner(settings, store, roles)
    t = r.run(r.add("write", workspace=ws(tmp_path), check="grep -q ok out.txt").id)
    first = t.version_id
    store.add_feedback(Feedback(task_id=t.id, kind=FeedbackKind.REJECT,
                                text="It needs a TITLE line.", actor="ana"))
    [t] = r.run_queued()
    assert t.status is Status.DONE and t.version_id != first
    assert store.version(first).neg == 1.0
    assert "TITLE line" in store.version(t.version_id).tree["memory.md"]
    assert (Path(t.workspace) / "out.txt").read_text() == "ok\nTITLE"


def test_prepare_command_creates_workspace(tmp_path, settings, store):
    s = settings.with_overrides(prepare="echo prepared > README && echo $FH_TASK_ID > id")
    r = make_runner(s, store, Roles(lambda *a: finish(status="done", summary="ok")))
    t = r.run(r.add("from prepare", check="grep -q prepared README").id)
    assert t.status is Status.DONE
    assert Path(t.workspace, "id").read_text().strip() == t.id


def test_failed_prepare_escalates(tmp_path, settings, store):
    s = settings.with_overrides(prepare="echo nope >&2; exit 1")
    r = make_runner(s, store, Roles(lambda *a: finish(status="done", summary="ok")))
    t = r.run(r.add("bad prepare", check="true").id)
    assert t.status is Status.WAITING and "nope" in t.question
