import json
import time

from fusion_harness import cli, demo
from fusion_harness.outbox import MAX_ATTEMPTS, flush


def test_outbox_is_idempotent_and_backs_off(store):
    assert store.enqueue("k1", "thing", {"n": 1})
    assert not store.enqueue("k1", "thing", {"n": 2})
    fails = {"n": 0}

    def sender(p):
        fails["n"] += 1
        if fails["n"] < 3:
            raise RuntimeError("network down")
        return {"ok": True}

    assert flush(store, {"thing": sender})["failed"] == 1
    assert flush(store, {"thing": sender})["sent"] == 0  # not due yet
    for _ in range(2):
        flush(store, {"thing": sender}, now=time.time() + 3600)
    row = store.outbox_item("k1")
    assert row["status"] == "sent" and json.loads(row["result"]) == {"ok": True}
    assert fails["n"] == 3


def test_outbox_gives_up_and_unknown_kinds_fail_fast(store):
    store.enqueue("k2", "thing", {})
    for _ in range(MAX_ATTEMPTS):
        flush(store, {"thing": lambda p: (_ for _ in ()).throw(RuntimeError("x"))},
              now=time.time() + 10_000)
    assert store.outbox_item("k2")["status"] == "failed"
    store.enqueue("k3", "carrier_pigeon", {})
    flush(store, {})
    assert store.outbox_item("k3")["status"] == "failed"


def test_cli_add_feedback_status(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("FH_MODEL", raising=False)
    home = str(tmp_path / "home")
    assert cli.main(["--home", home, "add", "do", "the", "thing", "--check", "true"]) == 0
    task_id = capsys.readouterr().out.strip()
    assert task_id.startswith("do-the-thing-")
    assert cli.main(["--home", home, "feedback", task_id, "--say", "also this"]) == 0
    assert "follow-up queued" in capsys.readouterr().out
    assert cli.main(["--home", home, "status"]) == 0
    assert task_id in capsys.readouterr().out
    assert cli.main(["--home", home, "work"]) == 1  # no model configured
    assert "FH_MODEL" in capsys.readouterr().err


def test_demo_runs_offline(tmp_path, capsys):
    assert demo.main(tmp_path) == 0
    out = capsys.readouterr().out
    assert "questions asked on this task: 0" in out
    assert "token.txt" in out and "Numeric helpers" in out
