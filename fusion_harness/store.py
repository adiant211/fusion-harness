"""SQLite persistence: Tasks, Attempts, harness Versions, Feedback, Lessons and the outbox.

One process writes; every change is one short transaction. Outgoing messages are stored before
they are sent (the outbox), so a crash never loses a signal and a retry never duplicates one.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .domain import (
    Attempt,
    Feedback,
    FeedbackKind,
    Finish,
    FinishStatus,
    Lesson,
    Status,
    Task,
    Verdict,
    Version,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, title TEXT, body TEXT, domain TEXT, workspace TEXT, check_cmd TEXT,
  source TEXT, source_ref TEXT, status TEXT, wait_reason TEXT, question TEXT,
  escalation_ref TEXT, delivery_ref TEXT, version_id TEXT, notes TEXT, inputs TEXT,
  attempts INTEGER, created_at REAL, updated_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS tasks_source ON tasks(source, source_ref) WHERE source_ref != '';
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, n INTEGER, version_id TEXT, stop TEXT,
  finish TEXT, passed INTEGER, evidence TEXT, signature TEXT, method TEXT, run_dir TEXT,
  error TEXT, tokens_in INTEGER, tokens_out INTEGER, created_at REAL
);
CREATE TABLE IF NOT EXISTS versions (
  id TEXT PRIMARY KEY, domain TEXT, parent TEXT, origin TEXT, tree TEXT, notes TEXT,
  pos REAL, neg REAL, created_at REAL
);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, text TEXT, actor TEXT,
  source_ref TEXT, version_id TEXT, processed INTEGER DEFAULT 0, created_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS feedback_source ON feedback(source_ref) WHERE source_ref != '';
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT, task_id TEXT, cue TEXT, lesson TEXT,
  evidence TEXT, confidence TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS outbox (
  key TEXT PRIMARY KEY, kind TEXT, payload TEXT, status TEXT, attempts INTEGER,
  last_error TEXT, next_at REAL, result TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path: Path | str):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    # ---------------------------------------------------------------- tasks

    def add_task(self, task: Task) -> Task:
        now = time.time()
        task.created_at = task.created_at or now
        task.updated_at = now
        with self.tx() as db:
            db.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                self._task_row(task),
            )
        return task

    def save_task(self, task: Task) -> None:
        task.updated_at = time.time()
        with self.tx() as db:
            db.execute("DELETE FROM tasks WHERE id=?", (task.id,))
            db.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                self._task_row(task),
            )

    @staticmethod
    def _task_row(t: Task) -> tuple:
        return (
            t.id, t.title, t.body, t.domain, t.workspace, t.check, t.source, t.source_ref,
            t.status.value, t.wait_reason, t.question, t.escalation_ref, t.delivery_ref,
            t.version_id, t.notes, json.dumps(t.inputs), t.attempts, t.created_at, t.updated_at,
        )

    @staticmethod
    def _task(r: sqlite3.Row) -> Task:
        return Task(
            id=r["id"], title=r["title"], body=r["body"], domain=r["domain"],
            workspace=r["workspace"], check=r["check_cmd"], source=r["source"],
            source_ref=r["source_ref"], status=Status(r["status"]), wait_reason=r["wait_reason"],
            question=r["question"], escalation_ref=r["escalation_ref"],
            delivery_ref=r["delivery_ref"], version_id=r["version_id"], notes=r["notes"],
            inputs=json.loads(r["inputs"] or "[]"), attempts=r["attempts"],
            created_at=r["created_at"], updated_at=r["updated_at"],
        )

    def task(self, task_id: str) -> Task | None:
        r = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task(r) if r else None

    def task_by_source(self, source: str, ref: str) -> Task | None:
        r = self.db.execute(
            "SELECT * FROM tasks WHERE source=? AND source_ref=?", (source, ref)
        ).fetchone()
        return self._task(r) if r else None

    def task_by_escalation(self, ref: str) -> Task | None:
        r = self.db.execute("SELECT * FROM tasks WHERE escalation_ref=?", (ref,)).fetchone()
        return self._task(r) if r else None

    def tasks(self, status: Status | None = None) -> list[Task]:
        if status is None:
            rows = self.db.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM tasks WHERE status=? ORDER BY created_at", (status.value,)
            ).fetchall()
        return [self._task(r) for r in rows]

    # ------------------------------------------------------------- attempts

    def add_attempt(self, a: Attempt) -> Attempt:
        finish = None
        if a.finish:
            finish = json.dumps({**a.finish.__dict__, "status": a.finish.status.value})
        v = a.verdict
        with self.tx() as db:
            cur = db.execute(
                "INSERT INTO attempts (task_id, n, version_id, stop, finish, passed, evidence, "
                "signature, method, run_dir, error, tokens_in, tokens_out, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    a.task_id, a.n, a.version_id, a.stop, finish,
                    None if v is None else int(v.passed), v.evidence if v else "",
                    v.signature if v else "", v.method if v else "", a.run_dir, a.error,
                    a.tokens_in, a.tokens_out, time.time(),
                ),
            )
            a.id = cur.lastrowid
        return a

    def attempts(self, task_id: str) -> list[Attempt]:
        rows = self.db.execute(
            "SELECT * FROM attempts WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()
        out = []
        for r in rows:
            finish = None
            if r["finish"]:
                d = json.loads(r["finish"])
                d["status"] = FinishStatus(d["status"])
                finish = Finish(**d)
            verdict = None
            if r["passed"] is not None:
                verdict = Verdict(bool(r["passed"]), r["evidence"], r["signature"], r["method"])
            traj = Path(r["run_dir"]) / "trajectory.md"
            out.append(
                Attempt(
                    task_id=r["task_id"], n=r["n"], version_id=r["version_id"], finish=finish,
                    verdict=verdict, stop=r["stop"], run_dir=r["run_dir"], error=r["error"],
                    trajectory=traj.read_text(encoding="utf-8") if traj.is_file() else "",
                    tokens_in=r["tokens_in"], tokens_out=r["tokens_out"], id=r["id"],
                )
            )
        return out

    # ------------------------------------------------------------- versions

    def add_version(self, v: Version) -> Version:
        """Insert a version; an identical tree for the same domain returns the stored one."""
        existing = self.version(v.id)
        if existing:
            return existing
        v.created_at = v.created_at or time.time()
        with self.tx() as db:
            db.execute(
                "INSERT INTO versions VALUES (?,?,?,?,?,?,?,?,?)",
                (v.id, v.domain, v.parent, v.origin, json.dumps(v.tree, sort_keys=True),
                 v.notes, v.pos, v.neg, v.created_at),
            )
        return v

    @staticmethod
    def _version(r: sqlite3.Row) -> Version:
        return Version(
            id=r["id"], domain=r["domain"], tree=json.loads(r["tree"]), parent=r["parent"],
            origin=r["origin"], notes=r["notes"], pos=r["pos"], neg=r["neg"],
            created_at=r["created_at"],
        )

    def version(self, version_id: str) -> Version | None:
        r = self.db.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
        return self._version(r) if r else None

    def versions(self, domain: str) -> list[Version]:
        rows = self.db.execute(
            "SELECT * FROM versions WHERE domain=? ORDER BY created_at", (domain,)
        ).fetchall()
        return [self._version(r) for r in rows]

    def domains(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT DISTINCT domain FROM versions ORDER BY 1")]

    def reward(self, version_id: str, *, pos: float = 0.0, neg: float = 0.0) -> None:
        with self.tx() as db:
            db.execute(
                "UPDATE versions SET pos = pos + ?, neg = neg + ? WHERE id=?",
                (pos, neg, version_id),
            )

    # ------------------------------------------------------------- feedback

    def add_feedback(self, fb: Feedback) -> Feedback | None:
        """Record a signal once; a repeated source_ref (the same comment or reaction) is None."""
        try:
            with self.tx() as db:
                cur = db.execute(
                    "INSERT INTO feedback (task_id, kind, text, actor, source_ref, version_id, "
                    "created_at) VALUES (?,?,?,?,?,?,?)",
                    (fb.task_id, fb.kind.value, fb.text, fb.actor, fb.source_ref,
                     fb.version_id, time.time()),
                )
                fb.id = cur.lastrowid
        except sqlite3.IntegrityError:
            return None
        return fb

    def pending_feedback(self) -> list[Feedback]:
        rows = self.db.execute("SELECT * FROM feedback WHERE processed=0 ORDER BY id").fetchall()
        return [self._feedback(r) for r in rows]

    def feedback_for(self, task_id: str) -> list[Feedback]:
        rows = self.db.execute(
            "SELECT * FROM feedback WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()
        return [self._feedback(r) for r in rows]

    @staticmethod
    def _feedback(r: sqlite3.Row) -> Feedback:
        return Feedback(
            task_id=r["task_id"], kind=FeedbackKind(r["kind"]), text=r["text"], actor=r["actor"],
            source_ref=r["source_ref"], version_id=r["version_id"], id=r["id"],
        )

    def mark_processed(self, feedback_id: int) -> None:
        with self.tx() as db:
            db.execute("UPDATE feedback SET processed=1 WHERE id=?", (feedback_id,))

    # -------------------------------------------------------------- lessons

    def add_lesson(self, lesson: Lesson) -> Lesson:
        with self.tx() as db:
            cur = db.execute(
                "INSERT INTO lessons (domain, task_id, cue, lesson, evidence, confidence, "
                "created_at) VALUES (?,?,?,?,?,?,?)",
                (lesson.domain, lesson.task_id, lesson.cue, lesson.lesson, lesson.evidence,
                 lesson.confidence, time.time()),
            )
            lesson.id = cur.lastrowid
        return lesson

    def lessons(self, domain: str) -> list[Lesson]:
        rows = self.db.execute(
            "SELECT * FROM lessons WHERE domain=? ORDER BY id", (domain,)
        ).fetchall()
        return [
            Lesson(domain=r["domain"], task_id=r["task_id"], cue=r["cue"], lesson=r["lesson"],
                   evidence=r["evidence"], confidence=r["confidence"], id=r["id"])
            for r in rows
        ]

    # --------------------------------------------------------------- outbox

    def enqueue(self, key: str, kind: str, payload: dict) -> bool:
        """Queue one outgoing message by a stable key; queuing the same key again is a no-op."""
        try:
            with self.tx() as db:
                db.execute(
                    "INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?)",
                    (key, kind, json.dumps(payload), "pending", 0, "", 0.0, "", time.time()),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def due(self, now: float | None = None) -> list[sqlite3.Row]:
        now = time.time() if now is None else now
        return self.db.execute(
            "SELECT * FROM outbox WHERE status='pending' AND next_at <= ? ORDER BY created_at",
            (now,),
        ).fetchall()

    def outbox_item(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM outbox WHERE key=?", (key,)).fetchone()

    def outbox_items(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM outbox ORDER BY created_at").fetchall()

    def settle(self, key: str, result: dict) -> None:
        with self.tx() as db:
            db.execute(
                "UPDATE outbox SET status='sent', result=?, last_error='' WHERE key=?",
                (json.dumps(result), key),
            )

    def defer(self, key: str, error: str, *, delay: float, give_up: bool = False) -> None:
        with self.tx() as db:
            db.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error=?, next_at=?, "
                "status=? WHERE key=?",
                (error[:2000], time.time() + delay, "failed" if give_up else "pending", key),
            )

    # ------------------------------------------------------------------- kv

    def get(self, key: str, default: str = "") -> str:
        r = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set(self, key: str, value: str) -> None:
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, value))
