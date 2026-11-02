from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from fusion_harness import prompts
from fusion_harness.config import Settings
from fusion_harness.loop import Runner
from fusion_harness.model import ScriptedModel
from fusion_harness.store import Store

Plan = Callable[[int, str, str], list[dict]]


def tool(name: str, **inp) -> dict:
    return {"type": "tool_use", "name": name, "input": inp}


def finish(**inp) -> list[dict]:
    return [tool("finish", **inp)]


def keep(step, system, prompt):
    return finish(status="keep", notes="no change")


def no_lesson(step, system, prompt):
    return finish(entry=False)


def not_resolvable(step, system, prompt):
    return finish(resolvable=False, question="sharpened question?")


class Roles:
    """Route each session to a per-role plan by its system prompt."""

    def __init__(self, worker: Plan, improver: Plan = keep, triage: Plan = not_resolvable,
                 verifier: Plan | None = None, reflect: Plan = no_lesson):
        self.plans = {"worker": worker, "improver": improver, "triage": triage,
                      "verifier": verifier, "reflect": reflect}
        self.seen: list[tuple[str, str]] = []

    def role(self, system: str) -> str:
        if system == prompts.IMPROVER_SYSTEM:
            return "improver"
        if system == prompts.TRIAGE_SYSTEM:
            return "triage"
        if system == prompts.VERIFIER_SYSTEM:
            return "verifier"
        if system == prompts.REFLECT_SYSTEM:
            return "reflect"
        return "worker"

    def __call__(self, system, messages, tools):
        role = self.role(system)
        step = sum(1 for m in messages if m["role"] == "assistant")
        first = messages[0]["content"]
        self.seen.append((role, first if isinstance(first, str) else ""))
        plan = self.plans[role]
        if plan is None:
            raise AssertionError(f"unexpected {role} session")
        return plan(step, system, first if isinstance(first, str) else "")

    def count(self, role: str) -> int:
        return sum(1 for r, _ in self.seen if r == role)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / ".fusion", model="scripted",
                    workspace_root=tmp_path / "work", max_attempts=4, stall_limit=3,
                    command_timeout=30)


@pytest.fixture
def store(settings: Settings) -> Store:
    s = Store(settings.db_path)
    yield s
    s.close()


def make_runner(settings: Settings, store: Store, roles: Roles, **kw) -> Runner:
    return Runner(settings, store, ScriptedModel(roles), log=lambda m: None, **kw)
