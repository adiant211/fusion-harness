"""Settings, read from the environment (FH_*) and overridable from the command line."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    """Everything the kernel reads. Defaults are chosen so most deployments set only a model."""

    home: Path = Path(".fusion")
    model: str = ""
    improver_model: str = ""
    effort: str = "high"
    thinking: str = "adaptive"
    fallbacks: str = ""
    max_tokens: int = 32_000

    max_steps: int = 60
    max_attempts: int = 6
    stall_limit: int = 2
    probe_width: int = 1
    command_timeout: int = 600
    observation_chars: int = 20_000

    workspace_root: Path = Path(".fusion/work")
    prepare: str = ""

    github_repo: str = ""
    allowed_actors: tuple[str, ...] = ()
    task_label: str = "fh:task"
    webhook_url: str = ""
    poll_interval: int = 60

    extra: dict = field(default_factory=dict, compare=False)

    @property
    def db_path(self) -> Path:
        return self.home / "state.sqlite"

    @property
    def runs_dir(self) -> Path:
        return self.home / "runs"

    def improver(self) -> str:
        return self.improver_model or self.model

    def with_overrides(self, **values) -> "Settings":
        known = {f.name for f in fields(self)}
        return replace(self, **{k: v for k, v in values.items() if k in known and v is not None})

    @classmethod
    def from_env(cls) -> "Settings":
        home = Path(_env("FH_HOME", ".fusion"))
        allowed = tuple(a.strip() for a in _env("FH_ALLOWED").split(",") if a.strip())
        return cls(
            home=home,
            model=_env("FH_MODEL"),
            improver_model=_env("FH_IMPROVER_MODEL"),
            effort=_env("FH_EFFORT", "high"),
            thinking=_env("FH_THINKING", "adaptive"),
            fallbacks=_env("FH_FALLBACKS"),
            max_tokens=_int("FH_MAX_TOKENS", 32_000),
            max_steps=_int("FH_MAX_STEPS", 60),
            max_attempts=_int("FH_MAX_ATTEMPTS", 6),
            stall_limit=_int("FH_STALL_LIMIT", 2),
            probe_width=_int("FH_PROBE_WIDTH", 1),
            command_timeout=_int("FH_COMMAND_TIMEOUT", 600),
            observation_chars=_int("FH_OBSERVATION_CHARS", 20_000),
            workspace_root=Path(_env("FH_WORKSPACE_ROOT", str(home / "work"))),
            prepare=_env("FH_PREPARE"),
            github_repo=_env("FH_GITHUB_REPO"),
            allowed_actors=allowed,
            task_label=_env("FH_TASK_LABEL", "fh:task"),
            webhook_url=_env("FH_WEBHOOK_URL"),
            poll_interval=_int("FH_POLL_INTERVAL", 60),
        )


HARNESS_CREDENTIALS = frozenset(
    {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "FH_WEBHOOK_URL"}
)


def scrub_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for commands the agent runs: everything except the harness's own model
    credentials, which the work never needs. Other credentials (a `gh` login, a cloud CLI)
    pass through so the agent can do real work; this is hygiene, not a sandbox.
    """
    out = {k: v for k, v in os.environ.items() if k not in HARNESS_CREDENTIALS}
    out.update(extra or {})
    return out
