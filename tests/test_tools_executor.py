import json

from conftest import tool

from fusion_harness import tree
from fusion_harness.executor import run_session
from fusion_harness.model import ModelError, ScriptedModel, Turn
from fusion_harness.tools import WORKER_FINISH, ToolContext, clip, toolbox, validate_input


def ctx(tmp_path):
    (tmp_path / "ws").mkdir(exist_ok=True)
    h = tmp_path / "h"
    tree.write_tree(tree.base_tree(), h)
    return ToolContext(workspace=tmp_path / "ws", harness_dir=h, timeout=20)


def test_validate_input():
    schema = WORKER_FINISH["input_schema"]
    assert validate_input(schema, {"status": "done", "summary": "ok"}) == ""
    assert "missing required" in validate_input(schema, {"status": "done"})
    assert "one of" in validate_input(schema, {"status": "maybe", "summary": "x"})
    assert "unknown field" in validate_input(schema, {"status": "done", "summary": "x", "z": 1})
    assert "must be string" in validate_input(schema, {"status": "done", "summary": 3})
    assert validate_input({"properties": {"n": {"type": "integer"}}}, {"n": True}) != ""


def test_builtin_and_harness_tools(tmp_path):
    c = ctx(tmp_path)
    defs, handlers = toolbox(tree.base_tree(), finish=WORKER_FINISH)
    names = [d["name"] for d in defs]
    assert names[-1] == "finish" and "find_text" in names and "skill" in names
    out, err = handlers["write_file"](c, {"path": "a/b.txt", "content": "hello\nworld\n"})
    assert not err
    out, err = handlers["edit_file"](c, {"path": "a/b.txt", "old": "world", "new": "there"})
    assert not err and (c.workspace / "a/b.txt").read_text() == "hello\nthere\n"
    out, err = handlers["edit_file"](c, {"path": "a/b.txt", "old": "absent", "new": "x"})
    assert err and "not found" in out
    out, err = handlers["read_file"](c, {"path": "a/b.txt"})
    assert "2\tthere" in out
    out, err = handlers["bash"](c, {"command": "echo $FH_WORKSPACE; exit 3"})
    assert err and out.startswith("exit 3") and str(c.workspace) in out
    out, err = handlers["find_text"](c, {"pattern": "there"})
    assert not err and "a/b.txt:2:" in out
    out, err = handlers["skill"](c, {"name": "escalate"})
    assert not err and "How to ask" in out
    out, err = handlers["skill"](c, {"name": "nope"})
    assert err and "available" in out


def test_bash_hides_harness_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("GH_TOKEN", "kept")
    c = ctx(tmp_path)
    out, _ = toolbox({}, finish=WORKER_FINISH)[1]["bash"](c, {"command": "env"})
    assert "sk-secret" not in out and "GH_TOKEN=kept" in out


def test_clip_keeps_head_and_tail():
    text = "A" * 100 + "B" * 100
    out = clip(text, 60)
    assert out.startswith("A") and out.endswith("B") and "cut by the harness" in out


def run(tmp_path, script, **kw):
    return run_session(ScriptedModel(script), system="sys", prompt="do it", tree=tree.base_tree(),
                       ctx=ctx(tmp_path), finish=WORKER_FINISH, run_dir=tmp_path / "run", **kw)


def test_session_finishes_and_records_trajectory(tmp_path):
    plan = [[tool("bash", command="echo hi")], [tool("finish", status="done", summary="ok")]]
    s = run(tmp_path, lambda sy, m, t: plan[sum(x["role"] == "assistant" for x in m)])
    assert s.stop == "finished" and s.finish["summary"] == "ok"
    md = (tmp_path / "run" / "trajectory.md").read_text()
    assert '[ACTION] bash {"command": "echo hi"}' in md and "[OBSERVATION] exit 0" in md


def test_invalid_input_is_returned_not_run(tmp_path):
    calls = []

    def script(sy, m, t):
        if len(m) == 1:
            return [tool("write_file", path="x.txt")]  # missing content
        calls.append(m[-1]["content"][0])
        return [tool("finish", status="done", summary="ok")]

    s = run(tmp_path, script)
    assert calls[0]["is_error"] and "INVALID_INPUT" in calls[0]["content"]
    assert not (tmp_path / "ws" / "x.txt").exists() and s.stop == "finished"


def test_max_tokens_tool_calls_are_not_run(tmp_path):
    seen = []

    def script(sy, m, t):
        if len(m) == 1:
            return [tool("write_file", path="big.txt", content="partial", stop_reason="max_tokens")]
        seen.append(m[-1]["content"][0])
        return [tool("finish", status="done", summary="ok")]

    run(tmp_path, script)
    assert seen[0]["is_error"] and "max_tokens" in seen[0]["content"]
    assert not (tmp_path / "ws" / "big.txt").exists()


def test_nudges_then_gives_up_without_finish(tmp_path):
    s = run(tmp_path, lambda sy, m, t: [{"type": "text", "text": "thinking aloud"}])
    assert s.stop == "no_finish" and s.steps == 3


def test_step_budget_and_warning(tmp_path):
    msgs = []

    def script(sy, m, t):
        msgs.append(m[-1])
        return [tool("bash", command="true")]

    s = run(tmp_path, script, max_steps=5)
    assert s.stop == "max_steps"
    assert any(isinstance(x["content"], list) and any(b.get("type") == "text" for b in x["content"])
               for x in msgs)


def test_provider_error_marks_needs_human(tmp_path):
    class Broken:
        name = "broken"

        def turn(self, *a):
            raise ModelError("bad key", needs_human=True)

    s = run_session(Broken(), system="s", prompt="p", tree={}, ctx=ctx(tmp_path),
                    finish=WORKER_FINISH)
    assert s.stop == "error" and s.needs_human


def test_unparseable_stream_is_reissued(tmp_path):
    class Flaky:
        name = "flaky"
        n = 0

        def turn(self, system, messages, tools):
            self.n += 1
            if self.n == 1:
                return Turn(content=None, text="", tool_calls=[], stop_reason="invalid_json",
                            error="bad json")
            return ScriptedModel(lambda *a: [tool("finish", status="done", summary="ok")]).turn(
                system, messages, tools)

    m = Flaky()
    s = run_session(m, system="s", prompt="p", tree={}, ctx=ctx(tmp_path), finish=WORKER_FINISH)
    assert s.stop == "finished" and m.n == 2
    assert json.dumps(s.trajectory).count("bad json") == 1
