"""fusion-harness: a self-improving agent harness.

Tasks loop until a check passes. Between attempts the harness (prompts, skills, tools,
scripts) is refined from the attempts' own trajectories. Sparse human feedback scores the
harness versions that produced each result, and the loop stops to ask a person only when it
is genuinely blocked.
"""

__version__ = "0.1.0"
