# Design

fusion-harness is the hybrid hypothesised from comparing two harnesses. dsh-superagent is a
runtime that decides what an agent may do in a live team and survives everything, but never
learns. Harness² is an offline loop that learns harness edits from trajectories, but assumes
replayable benchmark tasks and has no notion of people, permissions or follow-ups. Here the
runtime is the data source and the learning loop runs inside it, with sparse human feedback
filling the role a benchmark grader plays in Harness².

## Concepts

| Concept | Rule it keeps |
|---|---|
| Task | queued → running → done or waiting; a person's answer, correction or follow-up requeues it |
| Attempt | one worker session under one harness Version; always verified, never trusted |
| Verdict | pass/fail with evidence and a failure signature (timings, addresses and temp paths stripped) |
| Version | an immutable harness tree, content-addressed per domain, with weighted feedback counts |
| Feedback | answer, approve, reject or follow-up; recorded once per source (comment id, reaction id) |
| Lesson | one cue, lesson, evidence and confidence entry in a domain library, read by the improver |
| Signal | a delivered result or a request for a person, sent through the outbox |

The kernel (Python) is not editable by the improver: verification, the attempt budget, stall
detection, triage, scoring, the outbox and channel code. Everything the model reads as
instructions is the harness tree, and all of that is editable.

## The task loop

```
run(task):
  version = the version the task last ran, if it is resuming after an escalation;
            otherwise the domain's best-scoring version
  if the task was reopened by a correction: refine the harness first
  repeat up to FH_MAX_ATTEMPTS:
    attempt   worker session: always-on harness text as the system prompt; the task, task notes,
              the last three attempts and anything people said as the first message
    verify    the check command, or a verifier session that inspects the workspace
    pass      → deliver, score the version +0.25, consolidate and reflect, stop
    blocker   → triage once per distinct question: resolvable → retry with the hint;
                genuine → signal and wait
    provider rejects credentials or model → signal and wait
    same signature FH_STALL_LIMIT times → signal (stuck) and wait
    otherwise → refine the harness (or probe candidates), continue
  budget exhausted → signal and wait
```

Every role is a session on one executor: an append-only tool loop with step budgets, schema
validation of every tool input before it runs, and a trajectory in the shared grammar of
Harness² (`[SAY]`, `[ACTION]`, `[OBSERVATION]`, `[ERROR]`). Workers, verifiers, improvers, triage
and reflection differ only in system prompt, tools and finish schema.

### Why triage exists

The worker is the party most likely to give up too early ("I need a token") or push on too long.
dsh-superagent answers this with prompts alone; here an independent session with read access to
the workspace checks the claim first. It either finds the way forward (a config example to copy,
a token file the worker missed) and returns it as a hint, or sharpens the question. Each
distinct question is triaged once per run, so a worker that asks the same thing again after a
hint is escalated rather than looped.

## Refinement (task-level recursion)

The improver's working directory is an evidence tree:

```
task.md          the task as the worker received it
harness/         the harness, edited in place
task_notes.md    notes that only later attempts of this task read
attempts/        every attempt: worker status, verdict evidence, full trajectory
feedback/        what people said, the strongest evidence
library.md       lessons from earlier tasks in this domain
candidates/      (compose and consolidate) other harnesses with their probes
```

The mission comes from Harness² with three changes:

1. **Human feedback ranks above every trajectory contrast.** Harness² forbids grader signals in
   the improver's context, because they are answer keys. A person's correction is not an answer
   key to a benchmark; it is the requirement, so it leads.
2. **Task facts and domain procedure are separated.** Harness² writes workspace facts into the
   harness because the harness is delivered for that one task. Here the harness is shared by
   later tasks, so verified workspace facts go to `task_notes.md`, scoped to the task.
3. **Tools are first-class and editable.** Harness²'s plugins and sub-agents became harness
   tools: `tools/<name>.py` with a literal `TOOL = {...}` schema, parsed without executing the
   file, exposed to the worker as real tools, and validated input-by-input at call time. The
   improver is told when to refine an existing tool (it returned noise, failed, or was worked
   around) and when to create one (an operation done by hand twice).

With `FH_PROBE_WIDTH=1` (the default) each refinement is one proposal, and the next attempt is
its probe, on-policy, as in Harness²'s sequential mode. With a width of N, N candidates over the
INV, DOM and COMM dimensions are probed in workspace copies. A passing probe is adopted outright,
workspace included. Otherwise a COMPOSE session merges the candidates from their probes, as in
Harness²'s parallel mode. COMM is new: dsh-superagent's lesson that human attention is the
scarce resource, turned into a proposal dimension.

## Learning across tasks (harnessing-level recursion)

Versions are scored by a Beta(1,1) posterior. Approvals and rejections weigh 1; a passing check
with nobody looking weighs 0.25. Selection picks the highest posterior mean, newest on ties.
Derived versions inherit the parent's positive counts but not its negatives, so a refinement made
to fix a rejection is tried on its merits instead of starting below the seed.

After every delivery a CONSOLIDATE session edits the domain's **current best** version, not the
task's branch. It folds in what people said, plus the edits of the delivered harness when that
harness is not the best one. This keeps learning on one main line instead of scattering it
across branches. The demo exposed the alternative failure: an answer learned on a side branch
was never seen by the next task.

Reflection then writes at most one lesson, Harness² style: per-edit verdicts (helped, hurt, no
observed effect, confounded), then one cue/lesson pair that must rest on non-confounded rows.
Duplicate lessons are not stored.

## Runtime discipline (from dsh-superagent)

- **Outbox.** Every message is stored under a stable key before it is sent and retried with
  backoff, so a crash neither loses a signal nor sends it twice. A kind with no configured
  channel fails at once instead of retrying forever.
- **Attention.** Per run the agent speaks at most once, with a result or one question. State
  changes are labels, which notify nobody. Summaries are written for the person, and the
  technical record stays in `details` and the trajectory.
- **Gate.** Only allowed logins can create tasks or give feedback. dsh-superagent's full
  Mandate and admission-table machinery is deliberately left out (safety second); the
  allowed-actor list is the one piece that keeps a public issue tracker from steering the agent.
- **Idempotence.** Tasks are keyed by their source issue, feedback by comment or reaction id,
  versions by content.

## Lineage

| Part | From | Change |
|---|---|---|
| Harness tree, validators, autotrigger | Harness² `core/tree.py` | one surface; tools instead of plugins and sub-agents; tool-header parsing |
| Evidence tree and mission | Harness² `core/context.py`, `core/prompts.py` | feedback first; task notes; tool refinement guidance |
| INV/DOM proposals, compose | Harness² `core/recursion.py` | added COMM; probes adopt a passing workspace |
| Library lessons | Harness² `core/library.py`, `experience` prompt | stored in SQLite; deduplicated |
| Trajectory grammar | Harness² | unchanged |
| Task stages, outbox, idempotent intake | dsh-superagent Engine, Effects, Outbox | much smaller: one process, one store |
| Escalation (`human_action`) | dsh-superagent handoff prompts | plus independent triage before anyone is asked |
| Communication policy | dsh-superagent team prompts | two message types and labels |
| Version scoring by feedback | new | |
| Consolidation onto the best version | new | |
| Stall detection by failure signature | new | |

## Open questions

These are hypotheses, not results.

1. **Does feedback separate versions fast enough?** With one approval or rejection per few
   tasks, a domain may run a mediocre version for a while. Thompson sampling instead of the
   posterior mean would explore more and exploit less; it is a one-line change worth measuring.
2. **Do consolidated harnesses stay lean?** Each consolidation can add always-on text. The
   12,000-char cap is a hard stop, not a pressure; a periodic "prune" consolidation that must
   cut as much as it adds may be needed.
3. **Is triage worth its cost?** It is one extra session per distinct blocker. Measure the
   share of escalations it prevents, and the share of its "resolvable" calls that then pass.
4. **Transfer.** Harness²'s per-task gains mix a better harness with more test-time compute.
   The quantity that matters here is held-out: do tasks that start on a consolidated version
   need fewer attempts and fewer questions than tasks that start on the seed?
5. **Poisoning.** A comment from an allowed person flows into the harness through
   consolidation. That is intended, but a careless answer becomes a durable rule. A review step
   for consolidated versions (shadow-run before promotion) is the obvious next guardrail if
   this matters for your use.
