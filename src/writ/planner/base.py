"""Planner contract.

A planner turns a goal into a sequence of :class:`~writ.types.ActionRequest`
objects. It is the **untrusted** component: assume it is injected.

What it is given: the goal, past observations, and a read-only view of its
scopes so it can plan within them.

What it is not given: the broker, the router, the checkpoint store, credentials,
or any means of performing I/O. Invariant I1 is enforced by what this signature
omits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..scopes import ScopeSet
from ..types import ActionRequest, Outcome

__all__ = ["Done", "Observation", "Planner", "Step", "Trajectory"]


@dataclass(frozen=True, slots=True)
class Done:
    """The planner believes the goal is met, or that it cannot proceed."""

    summary: str
    succeeded: bool = True


Step = ActionRequest | Done


@dataclass(frozen=True, slots=True)
class Observation:
    """What came back from one admitted action.

    ``result`` is **untrusted**: it is data read from the world, it lands in the
    planner's context, and it is therefore a prompt-injection carrier. Nothing
    derived from it may widen a scope.
    """

    request: ActionRequest
    status: str
    result: Any = None
    error: str = ""
    detail: str = ""

    @classmethod
    def of(cls, outcome: Outcome) -> Observation:
        return cls(
            request=outcome.invocation.request,
            status=outcome.status,
            result=outcome.result,
            error=outcome.error,
            detail=(outcome.observed.detail if outcome.observed else "")
            or outcome.decision.rationale,
        )

    def brief(self) -> str:
        """One line, for a planner's context window."""
        head = f"{self.request.operation} -> {self.status}"
        if self.error:
            return f"{head}: {self.error}"
        if self.result is not None:
            text = repr(self.result)
            return f"{head}: {text[:400]}"
        return f"{head}: {self.detail}"


@dataclass
class Trajectory:
    """The full record of one run. The raw material for skill promotion (M7)."""

    goal: str
    goal_id: str
    steps: list[ActionRequest] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    finished: Done | None = None

    @property
    def succeeded(self) -> bool:
        return bool(self.finished and self.finished.succeeded)

    @property
    def all_verified(self) -> bool:
        """True when every effect was confirmed against a system of record.

        A trajectory containing an unverifiable step is not promotable to a
        skill: replaying something we could not confirm the first time is how a
        cache becomes a liability.
        """
        return all(
            o.observed is not None and o.observed.verifiable and o.observed.matched
            for o in self.outcomes
            if o.status == "ok"
        )


class Planner(Protocol):
    def next_action(
        self,
        goal: str,
        observations: list[Observation],
        scopes: ScopeSet,
    ) -> Step:
        """Decide the next step. Never executes anything."""
        ...
