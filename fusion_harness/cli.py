"""`fh`: run tasks, give feedback, watch GitHub, inspect what the harness learned."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import learning
from . import model as model_mod
from . import tree as harness_tree
from .config import Settings
from .domain import Feedback, FeedbackKind, Status
from .loop import Runner
from .store import Store


def _settings(args) -> Settings:
    s = Settings.from_env()
    over = {}
    if getattr(args, "home", None):
        over["home"] = Path(args.home)
        over["workspace_root"] = Path(args.home) / "work"
    for name in ("model", "max_attempts", "probe_width", "github_repo"):
        v = getattr(args, name, None)
        if v is not None:
            over[name] = v
    return s.with_overrides(**over)


def _runner(s: Settings, store: Store, *, senders=None) -> Runner:
    worker = model_mod.build(s)
    improver = model_mod.build(s, improver=True) if s.improver() != s.model else worker
    return Runner(s, store, worker, improver, senders=senders, log=lambda m: print(m, file=sys.stderr))


def _print_task(store: Store, task_id: str) -> None:
    t = store.task(task_id)
    print(f"{t.id}: {t.status.value}" + (f" ({t.wait_reason})" if t.wait_reason else ""))
    if t.question:
        print(f"  question: {t.question}")
        print(f"  answer with: fh feedback {t.id} --answer '...'")
    if t.status is Status.DONE:
        print(f"  harness: {t.version_id}   teach it: fh feedback {t.id} --approve | --reject '...'")


def cmd_run(args, *, run: bool = True) -> int:
    s = _settings(args)
    store = Store(s.db_path)
    title = args.title or " ".join(args.task)[:80]
    body = " ".join(args.task)
    if args.file:
        body = Path(args.file).read_text(encoding="utf-8")
        title = args.title or body.strip().splitlines()[0].lstrip("# ")[:80]
    if not run:
        task = Runner(s, store, model=None).add(title, body, workspace=args.workspace or "",
                                                check=args.check or "", domain=args.domain)
        print(task.id)
        return 0
    runner = _runner(s, store)
    task = runner.add(title, body, workspace=args.workspace or "", check=args.check or "",
                      domain=args.domain)
    runner.run(task.id)
    runner.flush()
    _print_task(store, task.id)
    return 0 if store.task(task.id).status is Status.DONE else 2


def cmd_work(args) -> int:
    s = _settings(args)
    store = Store(s.db_path)
    done = _runner(s, store).run_queued()
    for t in done:
        _print_task(store, t.id)
    if not done:
        print("nothing queued")
    return 0


def cmd_feedback(args) -> int:
    s = _settings(args)
    store = Store(s.db_path)
    task = store.task(args.task_id)
    if task is None:
        print(f"no task {args.task_id}", file=sys.stderr)
        return 1
    if args.approve:
        kind, text = FeedbackKind.APPROVE, args.approve if isinstance(args.approve, str) else ""
    elif args.reject is not None:
        kind, text = FeedbackKind.REJECT, args.reject
    elif args.answer is not None:
        kind, text = FeedbackKind.ANSWER, args.answer
    else:
        kind, text = FeedbackKind.FOLLOWUP, args.say or ""
    store.add_feedback(Feedback(task_id=task.id, kind=kind, text=text, actor=args.actor,
                                version_id=task.version_id))
    for action in learning.apply_pending(store):
        print(action)
    if args.run:
        return cmd_work(args)
    return 0


def cmd_watch(args) -> int:
    from .github import GitHub, poll

    s = _settings(args)
    store = Store(s.db_path)
    gh = GitHub(s.github_repo) if s.github_repo else None
    runner = _runner(s, store, senders=gh.senders() if gh else None)
    print(f"watching {s.github_repo or '(local tasks only)'} every {s.poll_interval}s",
          file=sys.stderr)
    while True:
        if gh:
            try:
                got = poll(store, gh, s, runner.add)
                if got["tasks"] or got["feedback"]:
                    print(f"intake: {got}", file=sys.stderr)
            except Exception as exc:
                print(f"poll failed: {exc}", file=sys.stderr)
        runner.run_queued()
        if args.once:
            return 0
        time.sleep(s.poll_interval)


def cmd_status(args) -> int:
    s = _settings(args)
    store = Store(s.db_path)
    if args.task_id:
        t = store.task(args.task_id)
        if t is None:
            print(f"no task {args.task_id}")
            return 1
        _print_task(store, t.id)
        for a in store.attempts(t.id):
            v = a.verdict
            print(f"  attempt {a.n}: harness {a.version_id} stop={a.stop} "
                  f"{'PASS' if v and v.passed else 'FAIL'} tokens={a.tokens_in}/{a.tokens_out}")
        for fb in store.feedback_for(t.id):
            print(f"  feedback {fb.kind.value} from {fb.actor or '-'}: {fb.text[:100]}")
        return 0
    print("tasks:")
    for t in store.tasks():
        extra = f" -- {t.question[:90]}" if t.status is Status.WAITING else ""
        print(f"  {t.status.value:8} {t.id}  [{t.domain}] {t.title[:60]}{extra}")
    for d in store.domains():
        best = learning.current(store, d)
        print(f"domain {d}: {len(store.versions(d))} harness versions, "
              f"{len(store.lessons(d))} lessons")
        for v in store.versions(d):
            mark = "*" if v.id == best.id else " "
            print(f"  {mark} {v.id} {v.origin:12} score={v.score:.3f} "
                  f"(+{v.pos:.2f}/-{v.neg:.2f}) parent={v.parent or '-'}")
    stuck = [r for r in store.outbox_items() if r["status"] != "sent"]
    if stuck:
        print("outbox:")
        for r in stuck:
            print(f"  {r['status']:7} {r['kind']:10} {r['key']} {r['last_error'][:80]}")
    return 0


def cmd_harness(args) -> int:
    s = _settings(args)
    store = Store(s.db_path)
    v = store.version(args.version) if args.version else learning.current(store, args.domain)
    if v is None:
        print(f"no version {args.version}")
        return 1
    if args.export:
        harness_tree.write_tree(v.tree, Path(args.export))
        print(f"wrote harness {v.id} to {args.export}")
        return 0
    print(f"harness {v.id} [{v.domain}] origin={v.origin} score={v.score:.3f}")
    print(json.dumps(harness_tree.validate(v.tree)))
    for rel in sorted(v.tree):
        print(f"  {rel} ({len(v.tree[rel])} chars)")
    if v.notes:
        print("notes:\n" + v.notes)
    return 0


def cmd_demo(args) -> int:
    from .demo import main as demo_main

    return demo_main(Path(args.dir) if args.dir else None)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fh", description=__doc__)
    p.add_argument("--home", help="state directory (FH_HOME, default .fusion)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def task_args(sp):
        sp.add_argument("task", nargs="*", help="what to do")
        sp.add_argument("--file", help="read the task description from a file")
        sp.add_argument("--title")
        sp.add_argument("--workspace", help="directory to work in (default: a fresh one)")
        sp.add_argument("--check", help="shell command that exits 0 when the task is done")
        sp.add_argument("--domain", default="default", help="harness/library partition")
        sp.add_argument("--model")
        sp.add_argument("--max-attempts", dest="max_attempts", type=int)
        sp.add_argument("--probe-width", dest="probe_width", type=int)

    sp = sub.add_parser("run", help="add a task and loop on it until done or blocked")
    task_args(sp)
    sp.set_defaults(fn=cmd_run)
    sp = sub.add_parser("add", help="queue a task without running it")
    task_args(sp)
    sp.set_defaults(fn=lambda a: cmd_run(a, run=False))

    sp = sub.add_parser("work", help="process feedback and run every queued task")
    sp.add_argument("--model")
    sp.set_defaults(fn=cmd_work)

    sp = sub.add_parser("feedback", help="answer, approve, reject or follow up on a task")
    sp.add_argument("task_id")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--approve", nargs="?", const=True)
    g.add_argument("--reject", metavar="WHAT_IS_WRONG")
    g.add_argument("--answer", metavar="TEXT")
    g.add_argument("--say", metavar="TEXT", help="a follow-up request on the task")
    sp.add_argument("--actor", default="operator")
    sp.add_argument("--run", action="store_true", help="run queued tasks afterwards")
    sp.add_argument("--model")
    sp.set_defaults(fn=cmd_feedback)

    sp = sub.add_parser("watch", help="poll GitHub, run tasks, send signals, repeat")
    sp.add_argument("--repo", dest="github_repo", help="owner/name (FH_GITHUB_REPO)")
    sp.add_argument("--once", action="store_true")
    sp.add_argument("--model")
    sp.set_defaults(fn=cmd_watch)

    sp = sub.add_parser("status", help="tasks, harness versions and their scores")
    sp.add_argument("task_id", nargs="?")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("harness", help="show or export a harness version")
    sp.add_argument("--domain", default="default")
    sp.add_argument("--version")
    sp.add_argument("--export", metavar="DIR")
    sp.set_defaults(fn=cmd_harness)

    sp = sub.add_parser("demo", help="run the offline demo (no model or credentials)")
    sp.add_argument("--dir")
    sp.set_defaults(fn=cmd_demo)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
