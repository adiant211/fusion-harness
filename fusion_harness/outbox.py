"""Send what the outbox holds: oldest first, each kind through its channel, with backoff.

A message that failed is retried after 5 s, doubling up to 5 minutes; after 8 failures it is
marked failed and shows in `fh status`. A kind with no configured channel fails at once, since
retrying cannot fix a missing configuration.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable

from .store import Store

Sender = Callable[[dict], dict]
MAX_ATTEMPTS = 8


def file_sender(path: Path, *, echo: bool = True) -> Sender:
    def send(payload: dict) -> dict:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        if echo:
            if payload.get("event") == "needs_human":
                print(f"[needs human] {payload['task']}: {payload['question']}\n"
                      f"  answer with: {payload['answer']}", file=sys.stderr)
            elif payload.get("event") == "done":
                print(f"[done] {payload['task']}: {payload['summary']}", file=sys.stderr)
        return {}

    return send


def webhook_sender(url: str, timeout: float = 15.0) -> Sender:
    def send(payload: dict) -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "fusion-harness"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"status": resp.status}

    return send


def flush(store: Store, senders: dict[str, Sender], *, now: float | None = None) -> dict:
    sent = failed = 0
    for row in store.due(now):
        key, kind = row["key"], row["kind"]
        payload = json.loads(row["payload"])
        sender = senders.get(kind)
        if sender is None:
            store.defer(key, f"no channel configured for {kind!r}", delay=0, give_up=True)
            failed += 1
            continue
        try:
            result = sender(payload) or {}
        except Exception as exc:
            tries = row["attempts"] + 1
            store.defer(key, f"{type(exc).__name__}: {exc}", delay=min(300.0, 5.0 * 2 ** (tries - 1)),
                        give_up=tries >= MAX_ATTEMPTS)
            failed += 1
            continue
        store.settle(key, result)
        _after_send(store, payload, result)
        sent += 1
    return {"sent": sent, "failed": failed, "at": time.time()}


def _after_send(store: Store, payload: dict, result: dict) -> None:
    """Remember where a message landed, so replies and reactions find their task."""
    task_id = payload.get("task_id")
    if not task_id:
        return
    task = store.task(task_id)
    if task is None:
        return
    if payload.get("purpose") == "delivery" and result.get("comment_id"):
        task.delivery_ref = str(result["comment_id"])
        store.save_task(task)
    elif "number" in result and payload.get("repo"):
        task.escalation_ref = f"{payload['repo']}#{result['number']}"
        store.save_task(task)
