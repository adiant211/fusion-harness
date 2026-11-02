"""An offline walk-through of the whole loop with a scripted model: no credentials, no cost.

1. A bug fix fails its check; the improver refines the harness (a verify-before-done rule, and a
   new `context` option on the existing `find_text` tool); the next attempt passes.
2. An upload needs a token nobody gave the agent: triage confirms it is a real blocker, a signal
   goes out, a person answers, the task resumes and passes, and the answer is consolidated into
   the domain's memory.
3. The next upload task reads that memory and finishes without asking.
4. A person rejects the first fix with a correction; the task reopens, the harness learns the
   rule, and the fix is redone. Feedback scores decide which harness the domain runs next.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
import tempfile
import textwrap
from pathlib import Path

from . import learning, prompts
from .config import Settings
from .domain import Feedback, FeedbackKind
from .model import ScriptedModel
from .store import Store

STATS = '''\
def mean(xs):
    """Arithmetic mean of a list of numbers."""
    total = sum(x for x in xs if x > 0)
    return total / len(xs)


def median(xs):
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2
'''

TEST_STATS = '''\
from stats import mean, median

assert mean([2, 4]) == 3, mean([2, 4])
assert mean([1, -1, 3]) == 1.0, f"mean([1, -1, 3]) returned {mean([1, -1, 3])}"
assert median([3, 1, 2]) == 2
print("all checks passed")
'''

UPLOAD = '''\
import sys

args = sys.argv[1:]
if "--token" not in args:
    sys.exit("usage: python upload.py --token TOKEN FILE")
token = args[args.index("--token") + 1]
if token != "demo-token":
    sys.exit("upload rejected: invalid token")
with open("upload.log", "a") as f:
    f.write(f"status: uploaded {args[-1]}\\n")
print("uploaded")
'''

REFINED_FIND_TEXT = '''\
TOOL = {
    "name": "find_text",
    "description": (
        "Search workspace files for a regular expression. Returns `path:line: text` for each "
        "match (at most 200). Pass `context` to also get that many lines around each match, "
        "which usually saves reading the whole file."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "A Python regular expression."},
            "path": {"type": "string", "description": "File or directory (default '.')."},
            "glob": {"type": "string", "description": "Only file names matching this glob."},
            "context": {"type": "integer", "description": "Lines of context around a match."},
        },
        "required": ["pattern"],
    },
}

import fnmatch
import json
import os
import re
import sys

SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".fusion"}


def files(root, glob):
    if os.path.isfile(root):
        yield root
        return
    for d, dirs, names in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP)
        for n in sorted(names):
            if fnmatch.fnmatch(n, glob):
                yield os.path.join(d, n)


def main():
    args = json.load(sys.stdin)
    rx = re.compile(args["pattern"])
    ctx = max(0, int(args.get("context") or 0))
    out = []
    for path in files(args.get("path") or ".", args.get("glob") or "*"):
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(lines):
            if rx.search(line):
                lo, hi = max(0, i - ctx), min(len(lines), i + ctx + 1)
                for j in range(lo, hi):
                    mark = ":" if j == i else "-"
                    out.append(f"{os.path.relpath(path)}{mark}{j + 1}{mark} {lines[j]}")
                if len(out) >= 200:
                    print("\\n".join(out))
                    return
    print("\\n".join(out) if out else "no matches")


if __name__ == "__main__":
    main()
'''

VERIFY_RULE = ("- Run the task's check command yourself before calling `finish` with status=done; "
               "a claim without a passing check is not done.")
EMPTY_RULE = "- Numeric helpers return 0.0 for empty input instead of raising (a person's correction)."
TOKEN_FACT = ("- Upload tokens: the operator puts the report-upload token in `token.txt` in the task "
              "workspace. Read it from there; do not ask for it.")


def _tool(name: str, **inp) -> dict:
    return {"type": "tool_use", "name": name, "input": inp}


def _say(text: str) -> dict:
    return {"type": "text", "text": text}


def _first_user(messages: list[dict]) -> str:
    c = messages[0]["content"]
    return c if isinstance(c, str) else json.dumps(c)


def demo_script(system: str, messages: list[dict], tools: list[dict]) -> list[dict]:
    step = sum(1 for m in messages if m["role"] == "assistant")
    task = _first_user(messages)

    if system == prompts.IMPROVER_SYSTEM:
        if "CONSOLIDATE WHAT PEOPLE TAUGHT" in task:
            said = re.search(r"people said \(([^)]*)\)", task)
            kinds = said.group(1) if said else ""
            edits = []
            if "answer" in kinds:
                edits.append(_tool("bash", command=f"grep -qF 'token.txt' harness/memory.md || "
                                   f"printf '%s\\n' {shlex.quote(TOKEN_FACT)} >> harness/memory.md"))
            if "reject" in kinds:
                edits.append(_tool("bash", command=f"grep -qF 'Numeric helpers' harness/memory.md || "
                                   f"printf '%s\\n' {shlex.quote(EMPTY_RULE)} >> harness/memory.md"))
            if "candidate harnesses and their probes" in task:
                edits.append(_tool("bash", command=(
                    "cp candidates/delivered/harness/guardrails.md harness/guardrails.md && "
                    "cp candidates/delivered/harness/tools/find_text.py harness/tools/")))
            plan = [_tool("bash", command="ls feedback candidates 2>/dev/null; cat harness/memory.md"),
                    *edits,
                    _tool("finish", status="changed" if edits else "keep", notes=(
                        "memory.md: what people answered or corrected, so nobody is asked twice"))]
        elif "feedback/" in task:
            plan = [
                _tool("read_file", path="feedback/01-reject.md"),
                _tool("bash", command=f"printf '%s\\n' {shlex.quote(EMPTY_RULE)} >> harness/memory.md"),
                _tool("finish", status="changed", notes=(
                    "memory.md: empty-input convention -- the person rejected the fix because "
                    "mean([]) raised ZeroDivisionError")),
            ]
        else:
            plan = [
                _tool("read_file", path="attempts/attempt-01.md"),
                _tool("bash", command=f"printf '%s\\n' {shlex.quote(VERIFY_RULE)} >> harness/guardrails.md"),
                _tool("write_file", path="harness/tools/find_text.py", content=REFINED_FIND_TEXT),
                _tool("write_file", path="task_notes.md",
                      content="The averaging code is mean() in stats.py; test_stats.py is the check.\n"),
                _tool("finish", status="changed", notes=(
                    "guardrails.md: verify-before-done -- attempt 1 finished 'done' without running "
                    "test_stats.py, which then failed (mean([1, -1, 3]) returned 1.33)\n"
                    "tools/find_text.py: added `context` -- attempt 1 read all of stats.py just to "
                    "see one function")),
            ]
        return [plan[min(step, len(plan) - 1)]]

    if system == prompts.TRIAGE_SYSTEM:
        plan = [
            _tool("bash", command="ls -a; env | grep -i token || echo 'no token in the environment'"),
            _tool("finish", resolvable=False, question=(
                "Which upload token should the report uploader use? There is none in the "
                "workspace, the environment or ~/.config/report/token.")),
        ]
        return [plan[min(step, len(plan) - 1)]]

    if system == prompts.REFLECT_SYSTEM:
        if "upload" in task.lower():
            lesson = dict(
                cue="A task needs a credential the agent was never given",
                lesson="Record where people put credentials in memory.md once answered, so the "
                       "question is asked once per domain, not once per task",
                evidence="attempt 1 escalated for the token; after the answer, attempt 2 passed",
                confidence="medium: one task, but the contrast is direct")
        else:
            lesson = dict(
                cue="The worker reports done without running the task's check",
                lesson="Put a verify-before-done rule in guardrails.md naming the check command",
                evidence="attempt 1 claimed done and failed test_stats.py; attempt 2 ran the check "
                         "first, found the real bug in mean() and passed",
                confidence="medium: one contrast; the find_text change is confounded with it")
        return [_tool("finish", entry=True, **lesson,
                      edits="guardrails.md verify rule: helped; tools/find_text context: confounded")]

    # The worker.
    if "Fix the averaging bug" in task:
        if "[correction from" in task:
            plan = [
                _say("A person says mean([]) must return 0.0."),
                _tool("edit_file", path="stats.py", old="return total / max(len(xs), 1)",
                      new="return total / len(xs) if xs else 0.0"),
                _tool("bash", command="python test_stats.py && python -c "
                      "\"from stats import mean; assert mean([]) == 0.0; print('empty ok')\""),
                _tool("finish", status="done", summary="mean() now returns 0.0 for an empty list.",
                      details="stats.py: guard the empty case; test_stats.py and an empty-list check pass"),
            ]
        elif "Run the task's check command yourself" in system:
            plan = [
                _tool("bash", command="python test_stats.py"),
                _tool("find_text", pattern="def mean", path="stats.py", context=4),
                _tool("edit_file", path="stats.py", old="total = sum(x for x in xs if x > 0)",
                      new="total = sum(xs)"),
                _tool("bash", command="python test_stats.py"),
                _tool("finish", status="done",
                      summary="mean() now counts negative numbers; the tests pass.",
                      details="stats.py: mean summed only positive values; test_stats.py passes"),
            ]
        else:
            plan = [
                _tool("read_file", path="stats.py"),
                _tool("edit_file", path="stats.py", old="return total / len(xs)",
                      new="return total / max(len(xs), 1)"),
                _tool("finish", status="done", summary="Fixed the averaging bug.",
                      details="guarded the division in mean()"),
            ]
    elif "report" in task.lower():
        knows = "[answer from" in task or "token.txt" in system
        if knows:
            plan = [
                _tool("bash", command="cat token.txt"),
                _tool("bash", command="python upload.py --token \"$(cat token.txt)\" report.md"),
                _tool("finish", status="done", summary="The report is uploaded.",
                      details="upload.py accepted the token from token.txt"),
            ]
        else:
            plan = [
                _tool("bash", command="ls -a && python upload.py report.md"),
                _tool("bash", command="test -f ~/.config/report/token && cat ~/.config/report/token "
                                      "|| echo 'no token file'"),
                _tool("finish", status="need_human", category="missing_access",
                      summary="The upload needs a token I do not have.",
                      question="Where is the upload token for report uploads?",
                      tried="Ran upload.py (it requires --token); looked for ~/.config/report/token "
                            "and in the workspace."),
            ]
    else:
        plan = [_tool("finish", status="impossible", summary="The demo script does not know this task.")]
    return [plan[min(step, len(plan) - 1)]]


def _section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def main(root: Path | None = None) -> int:
    from .loop import Runner

    root = Path(root) if root else Path(tempfile.mkdtemp(prefix="fh-demo-"))
    settings = Settings(home=root / ".fusion", model="scripted", workspace_root=root / "work",
                        max_attempts=4, stall_limit=3)
    store = Store(settings.db_path)
    model = ScriptedModel(demo_script)
    log = lambda m: print("  " + m, flush=True)
    runner = Runner(settings, store, model, log=log)

    def workspace(name: str, files: dict[str, str]) -> str:
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        for rel, text in files.items():
            (d / rel).write_text(text, encoding="utf-8")
        return str(d)

    _section("1. A bug fix that fails, a harness that refines itself")
    a = runner.add("Fix the averaging bug",
                   "Fix the averaging bug in stats.py: mean() gives wrong results for some inputs.",
                   workspace=workspace("fix-mean", {"stats.py": STATS, "test_stats.py": TEST_STATS}),
                   check="python test_stats.py", domain="pyops")
    runner.run(a.id)
    runner.flush()

    _section("2. A genuine blocker: escalate, get an answer, resume")
    b_ws = workspace("upload-1", {"report.md": "# Week 1\nAll green.\n", "upload.py": UPLOAD})
    b = runner.add("Upload this week's report", "Upload report.md with upload.py.",
                   workspace=b_ws, check="grep -q 'status: uploaded' upload.log", domain="pyops")
    runner.run(b.id)
    runner.flush()
    print(f"  task status: {store.task(b.id).status.value} -- {store.task(b.id).question}")
    print("  (a person answers, and puts the token where they said)")
    Path(b_ws, "token.txt").write_text("demo-token\n", encoding="utf-8")
    store.add_feedback(Feedback(task_id=b.id, kind=FeedbackKind.ANSWER, actor="operator",
                                text="I put the upload token in token.txt in the workspace; "
                                     "use that file for uploads from now on."))
    runner.run_queued()

    _section("3. The next upload: the answer is already in memory")
    c = runner.add("Upload next week's report", "Upload report.md with upload.py.",
                   workspace=workspace("upload-2", {"report.md": "# Week 2\nAll green.\n",
                                                    "upload.py": UPLOAD, "token.txt": "demo-token\n"}),
                   check="grep -q 'status: uploaded' upload.log", domain="pyops")
    runner.run(c.id)
    runner.flush()
    asked = [x for x in store.attempts(c.id) if x.finish and x.finish.status.value == "need_human"]
    print(f"  questions asked on this task: {len(asked)}")

    _section("4. Sparse feedback: approve one result, correct another")
    store.add_feedback(Feedback(task_id=c.id, kind=FeedbackKind.APPROVE, actor="operator"))
    store.add_feedback(Feedback(task_id=a.id, kind=FeedbackKind.REJECT, actor="operator",
                                text="mean([]) must return 0.0, not raise ZeroDivisionError."))
    runner.run_queued()

    _section("Where the harness ended up")
    best = learning.current(store, "pyops")
    for v in store.versions("pyops"):
        mark = "*" if v.id == best.id else " "
        print(f"  {mark} {v.id}  {v.origin:12} score {v.score:.3f}  (+{v.pos:.2f} / -{v.neg:.2f})")
    print("\n  library lessons:")
    for le in store.lessons("pyops"):
        print(f"  - when {le.cue.lower()}: {le.lesson}")
    signals = (settings.home / "signals.jsonl").read_text(encoding="utf-8").splitlines()
    print(f"\n  signals sent: {len(signals)} "
          f"({sum('needs_human' in s for s in signals)} asking for a person)")
    print(f"  model turns: {model.calls}; everything is under {root}")
    print("\n  memory.md of the domain's best harness:")
    print(textwrap.indent(best.tree.get("memory.md", "").strip(), "    "))
    statuses = {t.id: t.status.value for t in store.tasks()}
    ok = all(s == "done" for s in statuses.values())
    print(f"\n  tasks: {statuses}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else None))
