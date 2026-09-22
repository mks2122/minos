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
from .trace import Event, Observer
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

    observer: Observer | None = None
    """Called as the run proceeds. See :mod:`minos.trace`.

    Watching must never change the outcome, so every call is wrapped: a broken
    observer is ignored rather than allowed to abort a task.
    """

    def _emit(self, kind: str, step: int, text: str = "", **data: object) -> None:
        if self.observer is None:
            return
        try:
            self.observer(Event(kind=kind, step=step, text=text, data=dict(data)))
        except Exception:
            self.observer = None  # once is a bug; twice is noise

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

        for index in range(self.limits.max_steps):
            number = index + 1
            step = self.planner.next_action(goal, observations, self.broker.scopes)

            # Whatever the planner said while deciding. Shown, never acted on.
            thinking = str(getattr(self.planner, "last_thinking", "") or "")
            if thinking:
                self._emit("thinking", number, thinking)

            if isinstance(step, Done):
                trajectory.finished = step
                self._emit(
                    "finish",
                    number,
                    step.summary,
                    succeeded=step.succeeded,
                )
                return trajectory

            trajectory.steps.append(step)
            self._emit("plan", number, step.operation, params=step.params, intent=step.intent)

            try:
                routed = self.router.route(step)
            except NoAdapter as exc:
                self._emit("result", number, str(exc), status="unroutable")
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

            self._emit(
                "route",
                number,
                f"{routed.invocation.tier} / {routed.invocation.adapter}",
                tier=str(routed.invocation.tier),
                adapter=routed.invocation.adapter,
                reason=routed.invocation.tier_reason,
                effect=str(routed.invocation.contract.effect_class),
            )

            outcome = self.broker.submit(routed.invocation, routed.execute)
            self._emit(
                "verdict",
                number,
                outcome.decision.rationale,
                verdict=outcome.decision.verdict,
            )
            # An admitted action can succeed while the thing it ran fails -- a
            # sandboxed script is the case that matters. Showing "ok" there is
            # how a watcher misses the model retrying the same broken script.
            inner_failed = getattr(outcome.result, "ok", None) is False
            if inner_failed:
                detail = _last_line(
                    getattr(outcome.result, "detail", "") or getattr(outcome.result, "stderr", "")
                )
            else:
                detail = outcome.error or (outcome.observed.detail if outcome.observed else "")

            self._emit(
                "result",
                number,
                detail,
                status="script failed" if inner_failed else outcome.status,
                checkpoint=outcome.checkpoint_id,
                reversed_=(outcome.reversal.succeeded if outcome.reversal else None),
            )
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
                self._emit("finish", number, trajectory.finished.summary, succeeded=False)
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
                    self._emit("finish", number, trajectory.finished.summary, succeeded=False)
                    return trajectory

        trajectory.finished = Done(
            summary=f"step budget exhausted after {self.limits.max_steps} steps",
            succeeded=False,
        )
        self._emit("finish", self.limits.max_steps, trajectory.finished.summary, succeeded=False)
        return trajectory


def _last_line(text: object) -> str:
    """The final line of a traceback, which is the part that says what broke."""
    lines = str(text or "").strip().splitlines()
    return lines[-1].strip() if lines else "the script failed"


def _denial_key(request: ActionRequest) -> tuple[str, str]:
    return request.operation, repr(sorted(request.params.items()))
