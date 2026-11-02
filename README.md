# fusion-harness

A self-improving agent harness. It loops a task until a check passes, refines its own prompts,
skills and tools from what each attempt did, learns from the little feedback people give, and
interrupts a person only when it genuinely cannot continue.

It fuses two research harnesses:

- **[Harness²](https://github.com/google-research/harness2)** (Google Research) contributes the
  *learning loop*: the harness as a validated file tree, evidence-cited edits proposed from
  trajectories, Propose–Probe–Compose refinement, and a per-domain library of lessons.
- **[dsh-superagent](https://github.com/deepseek-ai/dsh-superagent-prototype)** (DeepSeek)
  contributes the *runtime discipline*: a durable task state machine, an outbox that never loses
  or duplicates a message, structured "ask one concrete question" escalation, and the rule that
  the agent speaks only when a person must act or can use a result.

What neither had, and this adds: **sparse human feedback as the reward signal**. Every result
records the harness version that produced it; approvals, rejections and answers score that
version, and the domain runs whichever version people have trusted most.

```
            ┌──────────────── task loop (per task) ────────────────┐
  task ───▶ │ attempt ─▶ verify ──pass──▶ deliver ───────────────────┼──▶ result + 👍/👎
            │   ▲          │fail                                    │
            │   └─ refine ◀┘ (improver edits prompts/skills/tools)  │
            │          blocker? ─▶ triage ─resolvable─▶ hint ─┐     │
            │                       └─genuine─▶ SIGNAL (issue)│     │
            └─────────────────────────────────────────────────┴─────┘
                    ▲ answer resumes                    │ feedback scores the version
            ┌───────┴────── harnessing level (per domain) ▼────────────┐
            │ versions scored by feedback · consolidate what people     │
            │ taught into the best harness · library of lessons         │
            └───────────────────────────────────────────────────────────┘
```

## Try it without a model

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
fh demo
```

The demo runs the whole system with a scripted model (no credentials, no cost):

1. A bug fix fails its check; the improver adds a verify-before-done rule **and upgrades the
   existing `find_text` tool** with a `context` option; the next attempt uses it and passes.
2. An upload needs a token nobody gave the agent. Triage confirms the blocker is real, one
   signal goes out, a person answers, the task resumes and passes, and the answer is
   consolidated into the domain's `memory.md`.
3. The next upload task reads that memory and finishes **without asking**.
4. A person approves one result and rejects another with a correction; the rejected task
   reopens, the harness learns the rule, and the version scores decide what runs next.

## Run real tasks

The shipped provider is Claude through the official Anthropic SDK (streaming, automatic prompt
caching, adaptive thinking). Set a model ID from
[Anthropic's model overview](https://docs.anthropic.com/en/docs/about-claude/models):

```bash
export ANTHROPIC_API_KEY=...
export FH_MODEL=<a Claude model id>
fh run "Make the test suite pass" --workspace ./myrepo --check "pytest -q"
```

`fh run` loops until the check exits 0, or stops with a question. Then:

```bash
fh status                                   # tasks, harness versions and their scores
fh feedback <task> --answer "use staging"   # answer a question; the task resumes
fh feedback <task> --approve                # this result was right
fh feedback <task> --reject "totals are off by one"   # reopens it with the correction
fh work                                     # run whatever is queued
fh harness --export ./h                     # look at what the harness has become
```

Without `--check`, a separate verifier session judges the workspace itself (never the worker's
claims). A check is better: it is the loop's ground truth.

### On GitHub

```bash
export FH_GITHUB_REPO=owner/name      # gh must be logged in with access to it
export FH_PREPARE='git clone https://github.com/$FH_GITHUB_REPO .'   # optional
fh watch
```

- Open an issue labelled `fh:task`. A `domain:<name>` label picks the harness domain; a fenced
  ` ```check ` block in the body is the check.
- Progress shows as labels (`fh:working`, `fh:done`, `fh:needs-human`), which notify nobody.
- Each run ends in at most one comment: the result, or one concrete question.
- Teach it: 👍/👎 on the result comment, `/approve`, `/reject <what is wrong>`, or just reply.
- Local tasks that get stuck open a `[needs human]` issue; replying there resumes them.
- Only `FH_ALLOWED` logins (default: the repo owner) can create tasks or give feedback. The
  agent tags its own comments with a hidden marker, so it can run under your own `gh` login.

Set `FH_WEBHOOK_URL` to also receive every signal as JSON (`done`, `needs_human`). Every signal
is appended to `.fusion/signals.jsonl` too.

## When it stops

The loop runs until the check passes. It stops and sends a signal only when:

| Reason | How it is decided |
|---|---|
| Needs a person | The worker reports a blocker (missing access, missing information, a decision, an external failure) **and** an independent triage session confirms nobody but a person can resolve it. A resolvable "blocker" comes back to the worker as a hint instead. |
| Provider failure | The model provider rejects the credentials or the model. |
| Stuck | The same failure signature repeats `FH_STALL_LIMIT` times (default 3) even though the harness was refined in between. |
| Budget | `FH_MAX_ATTEMPTS` attempts (default 6) without passing. |

Each signal carries one concrete question, why it was asked and what was already tried. An
answer resumes the task where it stopped, on the harness it had refined to.

## How it learns

**Within a task** (Harness² task-level recursion). After a failed attempt the improver gets an
evidence tree: the task, every attempt with its full trajectory and verdict, anything people
said, the domain's lessons, and the current harness. It edits the harness in place and must cite
the trajectory moment or feedback item behind each edit; "keep" is a valid answer. Facts about
this one workspace go to task-local notes, not the shared harness. The kernel validates the
result (every skill has a trigger line, every tool has a valid schema, every referenced script
exists, always-on text stays under 12,000 chars) and gives an invalid proposal one retry. With
`FH_PROBE_WIDTH=2` or more, it proposes INV (how the agent works), DOM (what a practitioner
knows) and COMM (when to ask a person) candidates, probes each in a copy of the workspace, adopts
a probe that passes, and otherwise composes one harness from the evidence.

**Across tasks** (harnessing-level recursion, scored by people). Every harness is an immutable,
content-addressed version. Each keeps a Beta(1,1) posterior over "people accept what this
produces":

| Signal | Effect on the version that produced the result |
|---|---|
| 👍, `/approve` | +1 |
| 👎, `/reject` | −1 (with text, the task also reopens with the correction) |
| check passed, nobody looked | +0.25 |
| answer to a question | no score; consolidated into the harness |

The domain runs its highest-scoring version; ties go to the newest, so fresh refinements get
tried. A derived version inherits its parent's positive evidence but not its negatives. After a
delivery, a consolidation pass folds what people taught (answers, corrections) and any in-task
refinements into the domain's best harness, so the same question is not asked twice. A
reflection pass writes at most one evidence-backed lesson to the domain library.

## The harness

| Component | Cost | What it holds |
|---|---|---|
| `system.md` | always on | role and loop contract |
| `guardrails.md` | always on | standing rules and skill trigger lines |
| `memory.md` | always on | durable facts, mostly learned from people |
| `tools.md` | always on | how to use the tools |
| `skills/<name>/SKILL.md` | on demand | procedures with a trigger |
| `tools/<name>.py` | its description | tools the model calls directly; **the improver can create and rewrite them** |
| `scripts/<name>.py` | on demand | deterministic helpers run through bash |

The seed harness is about 1,900 always-on characters. dsh-superagent's always-on prompt for a
worker is about 31,500.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `FH_MODEL` | (required) | worker model ID |
| `FH_IMPROVER_MODEL` | `FH_MODEL` | improver, verifier, triage and reflection model |
| `FH_EFFORT` | `high` | `output_config.effort` |
| `FH_THINKING` | `adaptive` | `adaptive`, or `off` to omit the parameter |
| `FH_FALLBACKS` | (off) | `default` enables server-side refusal fallbacks (beta) |
| `FH_MAX_ATTEMPTS` | 6 | attempts per run before a budget signal |
| `FH_MAX_STEPS` | 60 | tool calls per attempt |
| `FH_STALL_LIMIT` | 3 | identical failures before a stuck signal |
| `FH_PROBE_WIDTH` | 1 | candidates probed per refinement |
| `FH_COMMAND_TIMEOUT` | 600 | seconds per shell command |
| `FH_HOME` | `.fusion` | state: SQLite, runs, signals |
| `FH_PREPARE` | | shell command that fills a new task workspace |
| `FH_GITHUB_REPO` | | `owner/name` for GitHub intake, feedback and signals |
| `FH_ALLOWED` | repo owner | comma-separated logins allowed to task and teach it |
| `FH_WEBHOOK_URL` | | also POST every signal here |

## Safety posture

Safety comes second here, by design: the model provider's safeguards are the main line. The
harness adds only cheap hygiene. Commands run as your user without approval, inside the task
workspace by default. The harness's own model credentials are removed from the agent's
environment; other credentials, such as a `gh` login, pass through so it can do real work. Only
allowed people can task or teach it. Text from issues and tools is framed as data. There is no
sandbox; run it somewhere you would let a capable colleague run commands.

## Status

This is a research prototype. The test suite (61 tests) and the demo exercise every path
offline with scripted models. It has not yet been benchmarked against live models, and the
learning dynamics (how fast versions separate, whether lessons transfer) are hypotheses to
measure, not results. See [docs/DESIGN.md](docs/DESIGN.md) for the design, the lineage of each
part and the open questions.

## License

Apache-2.0. Adapted parts keep their upstream notices; see [NOTICE](NOTICE) and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
