"""The agent loop.

Ties planner, router and broker together. Deliberately small: every interesting
decision has already been made by the time control reaches here.

Three rules the loop enforces that a planner cannot override:

* A step budget. Long horizons are where agents fail, so the ceiling is explicit
  rather than emergent.
* ``reconciliation_required`` stops everything. When the runtime has lost track
  of state, continuing is worse than stopping -- no retry, no improvising around
  it.
* A denial does not end the run, but a repeated identical denial does. A planner
  that keeps asking for the same forbidden thing is either stuck or being
  driven, and neither improves with another attempt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .broker import Broker
from .planner.base import Done, Observation, Planner, Trajectory
from .router import NoAdapter, Router
from .types import ActionRequest, Outcome

__all__ = ["Agent", "AgentLimits"]


@dataclass(frozen=True, slots=True)
class AgentLimits:
    max_steps: int = 40
    max_repeated_denials: int = 3
    """Identical denied requests tolerated before the run is abandoned."""


@dataclass
class Agent:
    planner: Planner
    router: Router
    broker: Broker
    limits: AgentLimits = field(default_factory=AgentLimits)

    def __post_init__(self) -> None:
        # The broker has no router by design (I1), so it cannot run a declared
        # inverse on its own. The agent owns both, so this is where they meet --
        # and routing the inverse back through submit() is what gives it a scope
        # check and an audit entry rather than a privileged side channel.
        if self.broker.compensator is None:
            self.broker.compensator = self._compensate

    def _compensate(self, request: ActionRequest) -> Outcome:
        routed = self.router.route(request)
        return self.broker.submit(routed.invocation, routed.execute)

    def run(self, goal: str, goal_id: str = "task") -> Trajectory:
        trajectory = Trajectory(goal=goal, goal_id=goal_id)
        observations: list[Observation] = []
        denials: dict[tuple[str, str], int] = {}

        for _ in range(self.limits.max_steps):
            step = self.planner.next_action(goal, observations, self.broker.scopes)

            if isinstance(step, Done):
                trajectory.finished = step
                return trajectory

            trajectory.steps.append(step)

            try:
                routed = self.router.route(step)
            except NoAdapter as exc:
                observations.append(
                    Observation(
                        request=step,
                        status="unroutable",
                        error=str(exc),
                        detail="no adapter on any tier could prepare this",
                    )
                )
                trajectory.observations = observations
                continue

            outcome = self.broker.submit(routed.invocation, routed.execute)
            trajectory.outcomes.append(outcome)
            observations.append(Observation.of(outcome))
            trajectory.observations = observations

            if outcome.halted:
                trajectory.finished = Done(
                    summary=(
                        "halted: the runtime lost track of state and stopped rather "
                        f"than continuing -- {outcome.error}"
                    ),
                    succeeded=False,
                )
                return trajectory

            if outcome.status == "denied":
                key = _denial_key(step)
                denials[key] = denials.get(key, 0) + 1
                if denials[key] >= self.limits.max_repeated_denials:
                    trajectory.finished = Done(
                        summary=(
                            f"abandoned: {step.operation} was denied "
                            f"{denials[key]} times without changing"
                        ),
                        succeeded=False,
                    )
                    return trajectory

        trajectory.finished = Done(
            summary=f"step budget exhausted after {self.limits.max_steps} steps",
            succeeded=False,
        )
        return trajectory


def _denial_key(request: ActionRequest) -> tuple[str, str]:
    return request.operation, repr(sorted(request.params.items()))
