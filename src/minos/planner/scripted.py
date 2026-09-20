"""Scripted planners.

Not a toy. A deterministic planner is how the rest of the runtime gets tested
without spending money or inheriting model variance, and it is what the eval
harness uses to establish that a task is *achievable* before measuring whether a
model achieves it. If a scripted planner cannot complete a task, a failing model
score tells you nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..scopes import ScopeSet
from .base import Done, Observation, Step

__all__ = ["CallablePlanner", "ScriptedPlanner"]


@dataclass
class ScriptedPlanner:
    """Replays a fixed list of steps, in order."""

    script: list[Step]
    _index: int = field(default=0, init=False)

    def next_action(self, goal: str, observations: list[Observation], scopes: ScopeSet) -> Step:
        if self._index >= len(self.script):
            return Done(summary="script exhausted", succeeded=True)
        step = self.script[self._index]
        self._index += 1
        return step

    def reset(self) -> None:
        self._index = 0


@dataclass
class CallablePlanner:
    """Wraps a function, for tests that need to react to observations."""

    decide: Callable[[str, list[Observation], ScopeSet], Step]

    def next_action(self, goal: str, observations: list[Observation], scopes: ScopeSet) -> Step:
        return self.decide(goal, observations, scopes)
