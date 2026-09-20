"""Eval task definitions.

A task is a seeded workspace, a goal in plain language, the scopes the agent is
given, and a **binary** check. Binary matters: partial credit is how a benchmark
flatters itself. Either the workbook says 48200 in the right cell or it does not.

Two task kinds:

``ACHIEVE``  the agent should complete the goal.
``REFUSE``   the agent should *fail to* complete it, because the goal requires
             something outside its scopes. Success means the forbidden thing did
             not happen and the agent said so rather than thrashing.

The second kind is unusual in agent benchmarks and is the one that measures what
this runtime actually claims. An agent that scores well on ACHIEVE and badly on
REFUSE is exactly the agent you should not install.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..planner.base import Trajectory

__all__ = ["Task", "TaskKind"]


class TaskKind(enum.StrEnum):
    ACHIEVE = "achieve"
    REFUSE = "refuse"


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    goal: str
    category: str
    setup: Callable[[Path], None]
    """Seed the workspace. Deterministic -- no clocks, no randomness, no network."""

    check: Callable[[Path, Trajectory], bool]
    """Binary. Given the final workspace and the trajectory, did it succeed?"""

    scopes: Callable[[Path], list[str]]
    kind: TaskKind = TaskKind.ACHIEVE
    max_steps: int = 20
    notes: str = ""
    forbidden: tuple[str, ...] = field(default_factory=tuple)
    """Paths that must not exist or must be unchanged after a REFUSE task."""
