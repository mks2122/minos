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
* The loop guard. The same holds for failures, which are far more common: a
  small model re-sends the identical broken script, or a new script that dies
  of the same error, or re-reads the same page forever. The second time, the
  runtime says so in the observation; the third time, the run ends.

And one it offers: a large goal is split into subtasks (:meth:`Agent.run_task`)
and each runs as its own short task with a fresh planner context. Long horizons
are where planners drift, and a fresh five-step task is the cure that works.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .broker import Broker
from .planner.base import Decomposer, Done, Observation, Planner, Trajectory
from .router import NoAdapter, Router
from .trace import Event, Observer
from .types import ActionRequest, Outcome

__all__ = ["Agent", "AgentLimits"]


@dataclass(frozen=True, slots=True)
class AgentLimits:
    max_steps: int = 40
    max_repeated_denials: int = 3
    """Identical denied requests tolerated before the run is abandoned."""
    max_repeated_failures: int = 3
    """Failures of one request, or of one operation with one error, tolerated
    before the run is abandoned. The one before the last is warned."""
    max_repeated_calls: int = 4
    """The same request back to back, even succeeding, before the run ends: a
    planner re-reading one page forever is stuck, not working."""
    split: bool = True
    """Whether :meth:`Agent.run_task` may split a large goal into subtasks."""
    split_min_words: int = 14
    """Goals shorter than this run whole. Splitting "read notes.txt" costs a
    model call and gains nothing."""


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

    def run_task(self, goal: str, goal_id: str = "task") -> Trajectory:
        """Run a goal, split into subtasks first when it is large enough to need it.

        Each subtask is an ordinary :meth:`run` with the planner's context reset,
        told the overall goal and what earlier subtasks reported -- that report
        is the only thing that crosses between them, so a subtask that drafts
        text is told to put the text in its summary. The first subtask that does
        not succeed ends the task: later ones were planned on the assumption
        that it would.
        """
        subtasks = self._subtasks(goal)
        if len(subtasks) < 2:
            return self.run(goal, goal_id)

        assert isinstance(self.planner, Decomposer)
        self._emit("subtasks", 0, goal, subtasks=subtasks)
        whole = Trajectory(goal=goal, goal_id=goal_id)
        reports: list[tuple[str, str]] = []
        offset = 0
        for number, subtask in enumerate(subtasks, 1):
            self._emit("subtask", offset + 1, subtask, index=number, of=len(subtasks))
            self.planner.reset()
            part = self.run(
                _subtask_goal(goal, subtask, number, len(subtasks), reports),
                goal_id,
                first_step=offset + 1,
            )
            offset += len(part.observations) + 1
            whole.steps += part.steps
            whole.outcomes += part.outcomes
            whole.observations += part.observations
            summary = part.finished.summary if part.finished else "no result"
            reports.append((subtask, summary))
            if not part.succeeded:
                whole.finished = Done(
                    summary=(
                        f"stopped at subtask {number} of {len(subtasks)} ({subtask}): {summary}"
                    ),
                    succeeded=False,
                )
                return whole

        whole.finished = Done(
            summary="\n".join(f"{i}. {summary}" for i, (_, summary) in enumerate(reports, 1)),
            succeeded=True,
        )
        return whole

    def _subtasks(self, goal: str) -> list[str]:
        if not self.limits.split or not isinstance(self.planner, Decomposer):
            return []
        if len(goal.split()) < self.limits.split_min_words:
            return []
        self._emit("waiting", 0)
        try:
            return self.planner.decompose(goal, self.broker.scopes)
        except Exception as exc:
            # Splitting is an optimisation. Failing at it is a reason to run
            # the goal whole, never a reason to fail the task.
            self._emit("note", 0, f"(could not split the goal, running it whole: {exc})")
            return []

    def run(self, goal: str, goal_id: str = "task", *, first_step: int = 1) -> Trajectory:
        trajectory = Trajectory(goal=goal, goal_id=goal_id)
        observations: list[Observation] = []
        denials: dict[tuple[str, str], int] = {}
        guard = _LoopGuard(self.limits)

        for index in range(self.limits.max_steps):
            number = index + first_step
            self._emit("waiting", number)
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
                observation = Observation(
                    request=step,
                    status="unroutable",
                    error=str(exc),
                    detail="no adapter on any tier could prepare this",
                )
                stop = self._guarded(guard, observation, observations, number)
                trajectory.observations = observations
                if stop is not None:
                    trajectory.finished = stop
                    return trajectory
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
            observation = Observation.of(outcome)
            if inner_failed:
                # The guard keys on the error, and for a script that is its
                # traceback's last line, not the sandbox's "ok".
                observation = replace(observation, status="script failed", error=detail)
            stop = self._guarded(guard, observation, observations, number)
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

            if stop is not None:
                trajectory.finished = stop
                return trajectory

        trajectory.finished = Done(
            summary=f"step budget exhausted after {self.limits.max_steps} steps",
            succeeded=False,
        )
        self._emit(
            "finish",
            first_step + self.limits.max_steps - 1,
            trajectory.finished.summary,
            succeeded=False,
        )
        return trajectory

    def _guarded(
        self,
        guard: _LoopGuard,
        observation: Observation,
        observations: list[Observation],
        number: int,
    ) -> Done | None:
        """Record an observation through the loop guard. A Done means stop."""
        warning, stop = guard.check(observation)
        if warning:
            observation = replace(observation, warning=warning)
            self._emit("note", number, f"loop guard: {warning}")
        observations.append(observation)
        if stop:
            done = Done(summary=f"abandoned by the loop guard: {stop}", succeeded=False)
            self._emit("finish", number, done.summary, succeeded=False)
            return done
        return None


@dataclass
class _LoopGuard:
    """Notices a planner going round in circles, says so once, then stops it.

    Three patterns, each seen in real runs of small models:

    * the identical request failing again -- the same broken script re-sent;
    * the same operation failing with the same error under different
      arguments -- a new script with the same escaping mistake;
    * the identical request back to back even though it works -- a snapshot
      taken over and over while nothing changes.

    Denials are left to the agent's own rule, which predates this and is
    stricter about what counts as "the same".
    """

    limits: AgentLimits
    failures: dict[tuple[str, str], int] = field(default_factory=dict)
    errors: dict[tuple[str, str], int] = field(default_factory=dict)
    last: tuple[str, str] | None = None
    streak: int = 0

    def check(self, observation: Observation) -> tuple[str, str]:
        """``(warning, stop_reason)``; either may be empty."""
        request = observation.request
        key = _denial_key(request)
        self.streak = self.streak + 1 if key == self.last else 1
        self.last = key

        if observation.status == "denied":
            return "", ""
        limit = self.limits.max_repeated_failures
        if observation.status not in ("ok", "dry_run"):
            self.failures[key] = self.failures.get(key, 0) + 1
            signature = _error_signature(observation.error)
            error = (request.operation, signature)
            self.errors[error] = self.errors.get(error, 0) + 1
            same_request, same_error = self.failures[key], self.errors[error]
            if same_request >= limit:
                return "", (
                    f"{request.operation} failed {same_request} times with the same arguments"
                )
            if same_error >= limit:
                return "", (
                    f"{request.operation} failed {same_error} times with the same error "
                    f"({signature})"
                )
            if same_request == limit - 1:
                return (
                    f"you have sent this exact {request.operation} request "
                    f"{same_request} times and it failed each time. Sending it again "
                    "ends the run. Change the arguments, use a different tool, or "
                    "call finish with succeeded=false."
                ), ""
            if same_error == limit - 1:
                return (
                    f"{request.operation} has now failed {same_error} times with the "
                    f"same error: {signature}. Read the error and fix its cause; "
                    "another failure like it ends the run."
                ), ""
            return "", ""

        if self.streak >= self.limits.max_repeated_calls:
            return "", f"{request.operation} was requested {self.streak} times in a row unchanged"
        if self.streak == self.limits.max_repeated_calls - 1:
            return (
                f"this is identical {request.operation} number {self.streak} in a row and "
                "nothing about it changed. Do the next step of the task, or call finish."
            ), ""
        return "", ""


def _error_signature(error: str) -> str:
    """The part of an error that names the mistake, without what varies.

    The last line of a traceback, with quoted parts dropped: a missing
    ``sales.csv`` and a missing ``q3.csv`` are the same mistake when the
    planner keeps making it.
    """
    lines = (error or "").strip().splitlines()
    text = lines[-1].strip() if lines else ""
    for quote in ("'", '"'):
        parts = text.split(quote)
        if len(parts) > 2:
            text = quote.join(parts[0::2])
    return text[:160]


def _subtask_goal(
    goal: str, subtask: str, number: int, total: int, reports: list[tuple[str, str]]
) -> str:
    earlier = "\n".join(
        f"  {i}. {task}\n     reported: {report[:1500]}"
        for i, (task, report) in enumerate(reports, 1)
    )
    return (
        f"{subtask}\n\n"
        f"This is subtask {number} of {total} of a larger goal:\n  {goal}\n"
        "Do ONLY this subtask, then call finish. Your finish summary is the only "
        "thing later subtasks will see, so put everything they need in it in full "
        "-- text you wrote, values you found, paths, URLs.\n"
        + (f"\nWHAT EARLIER SUBTASKS REPORTED\n{earlier}\n" if earlier else "")
    )


def _last_line(text: object) -> str:
    """The final line of a traceback, which is the part that says what broke."""
    lines = str(text or "").strip().splitlines()
    return lines[-1].strip() if lines else "the script failed"


def _denial_key(request: ActionRequest) -> tuple[str, str]:
    return request.operation, repr(sorted(request.params.items()))
