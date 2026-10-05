"""Eval harness.

Runs tasks in seeded, throwaway workspaces and reports numbers that are honest
about the things this runtime claims.

The headline is task success, binary. But three secondary metrics are the ones
that would catch the runtime quietly breaking its own promises, so they are
reported at the same level rather than in a footnote:

``unverified_effect_rate``  effects accepted without a system-of-record read.
                            **Target 0.** Anything above it is the runtime
                            taking a screenshot's word for something.
``rollback_success_rate``   reversals that restored *and verified*. Anything
                            below 1.0 is a bug report.
``fallback_rate``           actions that needed something worse than a native
                            typed tool. Rising means the adapter ecosystem is
                            losing to the long tail.

A ``halted`` count is also reported. Halts are not failures of the runtime --
they are the runtime refusing to continue after losing track of state, which is
the correct behaviour -- but a rising halt rate means something upstream is
wrong.
"""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from ..agent import Agent, AgentLimits
from ..audit import AuditLog
from ..broker import Broker, always_deny
from ..checkpoint import FileCheckpointStore
from ..planner.base import Planner, Trajectory
from ..router import Router
from ..scopes import ScopeSet
from ..tiers.base import Adapter
from ..tiers.l1_system import FilesystemAdapter, ProcessAdapter
from ..tiers.l2_adapters import TabularAdapter
from ..types import Tier
from .invariants import check_invariants
from .task import Task

__all__ = [
    "EvalReport",
    "PlannerFactory",
    "TaskResult",
    "VarianceReport",
    "default_adapters",
    "run_suite",
    "run_suite_repeatedly",
]

PlannerFactory = Callable[[Task, Path], Planner]


def default_adapters() -> tuple[Adapter, ...]:
    return (FilesystemAdapter(), ProcessAdapter(), TabularAdapter())


@dataclass(frozen=True, slots=True)
class TaskResult:
    task_id: str
    category: str
    kind: str
    succeeded: bool
    steps: int
    duration_s: float
    tier_counts: dict[str, int]
    fallback_rate: float
    rollback_attempts: int
    rollback_successes: int
    unverified_effects: int
    verified_effects: int
    halted: bool
    audit_intact: bool
    summary: str
    error: str = ""
    violations: tuple[str, ...] = ()
    """Broken runtime promises. A task may fail; the runtime may not."""

    unverified_gui: int = 0
    """Of ``unverified_effects``, those that were L3 input with no files declared:
    a keystroke has no system of record until something is saved."""

    def row(self) -> str:
        mark = "PASS" if self.succeeded else "FAIL"
        return (
            f"  {mark}  {self.task_id:<34} {self.kind:<8} "
            f"{self.steps:>3} steps  {self.duration_s:>6.2f}s"
        )


@dataclass
class EvalReport:
    results: list[TaskResult] = field(default_factory=list)
    planner_name: str = "unknown"
    model: str = "n/a"

    # -- headline ----------------------------------------------------------

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.succeeded)

    @property
    def success_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    # -- the metrics that catch broken promises ---------------------------

    @property
    def unverified_effect_rate(self) -> float:
        total = sum(r.verified_effects + r.unverified_effects for r in self.results)
        if not total:
            return 0.0
        return sum(r.unverified_effects for r in self.results) / total

    @property
    def unverified_outside_gui_rate(self) -> float:
        """The same rate with L3 input set aside. This one has a target of 0."""
        total = sum(r.verified_effects + r.unverified_effects for r in self.results)
        if not total:
            return 0.0
        return sum(r.unverified_effects - r.unverified_gui for r in self.results) / total

    @property
    def rollback_success_rate(self) -> float:
        attempts = sum(r.rollback_attempts for r in self.results)
        if not attempts:
            return 1.0
        return sum(r.rollback_successes for r in self.results) / attempts

    @property
    def fallback_rate(self) -> float:
        counts: dict[str, int] = {}
        for result in self.results:
            for tier, n in result.tier_counts.items():
                counts[tier] = counts.get(tier, 0) + n
        total = sum(counts.values())
        if not total:
            return 0.0
        return (total - counts.get("L1", 0)) / total

    @property
    def halted(self) -> int:
        return sum(1 for r in self.results if r.halted)

    @property
    def audit_breaks(self) -> int:
        return sum(1 for r in self.results if not r.audit_intact)

    @property
    def violations(self) -> list[str]:
        """Every broken runtime promise across the suite.

        Distinct from a failed task. A task may fail -- agents are allowed to be
        bad at things. The runtime is not allowed to break a promise while they
        are, and this is the number that says whether it did.
        """
        found: list[str] = []
        for result in self.results:
            found.extend(f"{result.task_id}: {violation}" for violation in result.violations)
        return found

    def by_category(self) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for result in self.results:
            passed, total = out.get(result.category, (0, 0))
            out[result.category] = (passed + int(result.succeeded), total + 1)
        return dict(sorted(out.items()))

    def by_kind(self) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for result in self.results:
            passed, total = out.get(result.kind, (0, 0))
            out[result.kind] = (passed + int(result.succeeded), total + 1)
        return dict(sorted(out.items()))

    # -- output ------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        durations = [r.duration_s for r in self.results] or [0.0]
        return {
            "planner": self.planner_name,
            "model": self.model,
            "tasks": self.total,
            "passed": self.passed,
            "success_rate": round(self.success_rate, 4),
            "unverified_effect_rate": round(self.unverified_effect_rate, 4),
            "unverified_outside_gui_rate": round(self.unverified_outside_gui_rate, 4),
            "rollback_success_rate": round(self.rollback_success_rate, 4),
            "fallback_rate": round(self.fallback_rate, 4),
            "halted": self.halted,
            "audit_breaks": self.audit_breaks,
            "invariant_violations": len(self.violations),
            "median_duration_s": round(statistics.median(durations), 3),
            "total_duration_s": round(sum(durations), 3),
        }

    def text(self) -> str:
        lines = [
            "",
            f"minos eval -- planner={self.planner_name} model={self.model}",
            "=" * 68,
            "",
        ]
        lines.extend(r.row() for r in self.results)
        lines.append("")
        lines.append(f"  success        {self.passed}/{self.total}  ({self.success_rate:.0%})")
        lines.append("")
        lines.append("  by kind")
        for kind, (passed, total) in self.by_kind().items():
            lines.append(f"    {kind:<12} {passed}/{total}")
        lines.append("")
        lines.append("  by category")
        for category, (passed, total) in self.by_category().items():
            lines.append(f"    {category:<12} {passed}/{total}")
        lines.append("")
        lines.append("  promises the runtime makes")
        unverified = self.unverified_effect_rate
        outside = self.unverified_outside_gui_rate
        if unverified and not outside:
            note = "   (all of it L3 input, which has no system of record; 0 elsewhere)"
        elif outside:
            note = f"   <-- {outside:.1%} outside L3; target is 0"
        else:
            note = "   (target met)"
        lines.append(f"    unverified effects   {unverified:.1%}{note}")
        rollback = self.rollback_success_rate
        lines.append(
            f"    rollback success     {rollback:.1%}"
            + ("" if rollback == 1.0 else "   <-- anything under 100% is a bug")
        )
        lines.append(f"    fallback rate        {self.fallback_rate:.1%}")
        lines.append(f"    halted               {self.halted}")
        lines.append(
            f"    audit chain breaks   {self.audit_breaks}"
            + ("" if not self.audit_breaks else "   <-- investigate immediately")
        )
        violations = self.violations
        lines.append(
            f"    invariant violations {len(violations)}"
            + ("   (all held)" if not violations else "   <-- the runtime broke a promise")
        )
        for violation in violations[:10]:
            lines.append(f"      {violation}")
        if len(violations) > 10:
            lines.append(f"      ... and {len(violations) - 10} more")
        lines.append("")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(
            {"summary": self.summary(), "results": [asdict(r) for r in self.results]},
            indent=2,
        )

    def regressions(self, baseline: dict[str, Any]) -> list[str]:
        """Tasks that passed in the baseline and fail now.

        A suite that only reports an aggregate hides the case where two tasks
        swap places and the headline holds steady.
        """
        was = {r["task_id"]: r["succeeded"] for r in baseline.get("results", [])}
        return sorted(r.task_id for r in self.results if was.get(r.task_id) and not r.succeeded)


def run_task(
    task: Task,
    planner_factory: PlannerFactory,
    adapters: tuple[Adapter, ...] | None = None,
) -> TaskResult:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        workspace.mkdir()
        task.setup(workspace)

        chosen = task.adapters(workspace) if task.adapters else (adapters or default_adapters())
        router = Router(adapters=chosen)
        audit = AuditLog(root / "audit.jsonl")
        store = FileCheckpointStore(root / "checkpoints")
        scopes = task.scopes(workspace)
        broker = Broker(
            scopes=ScopeSet.parse(scopes),
            audit=audit,
            store=store,
            # No approver unless the task names a policy: an unattended eval
            # must never auto-approve an irreversible effect on anything real.
            # A GUI task's policy approves input to its own simulated windows
            # and is recorded by name, so no audit entry claims a human said yes.
            approver=task.approver or always_deny,
        )
        planner = planner_factory(task, workspace)
        agent = Agent(
            planner=planner,
            router=router,
            broker=broker,
            limits=AgentLimits(max_steps=task.max_steps),
        )

        started = time.perf_counter()
        error = ""
        try:
            trajectory = agent.run(task.goal, goal_id=task.id)
        except Exception as exc:
            trajectory = Trajectory(goal=task.goal, goal_id=task.id)
            error = f"{type(exc).__name__}: {exc}"
        finally:
            # An isolated planner is a child process; one per task, closed here.
            close = getattr(planner, "close", None)
            if callable(close):
                close()
        duration = time.perf_counter() - started

        try:
            succeeded = bool(task.check(workspace, trajectory)) and not error
        except Exception as exc:
            succeeded = False
            error = error or f"check raised {type(exc).__name__}: {exc}"

        verified = sum(
            1 for o in trajectory.outcomes if o.observed is not None and o.observed.verifiable
        )
        unverified = sum(
            1 for o in trajectory.outcomes if o.observed is not None and not o.observed.verifiable
        )
        unverified_gui = sum(
            1
            for o in trajectory.outcomes
            if o.observed is not None
            and not o.observed.verifiable
            and o.invocation.tier is Tier.L3_GUI
        )
        attempts = sum(1 for o in trajectory.outcomes if o.reversal is not None)
        successes = sum(
            1 for o in trajectory.outcomes if o.reversal is not None and o.reversal.succeeded
        )

        violations = check_invariants(
            workspace, trajectory, audit, store, granted_scopes=tuple(scopes)
        )

        return TaskResult(
            task_id=task.id,
            category=task.category,
            kind=str(task.kind),
            succeeded=succeeded,
            steps=len(trajectory.steps),
            duration_s=duration,
            tier_counts={str(k): v for k, v in router.stats.routed.items()},
            fallback_rate=round(router.stats.fallback_rate, 4),
            rollback_attempts=attempts,
            rollback_successes=successes,
            unverified_effects=unverified,
            unverified_gui=unverified_gui,
            verified_effects=verified,
            halted=any(o.halted for o in trajectory.outcomes),
            audit_intact=audit.verify() == [],
            summary=(trajectory.finished.summary if trajectory.finished else ""),
            error=error,
            violations=tuple(str(v) for v in violations),
        )


def run_suite(
    tasks: Sequence[Task],
    planner_factory: PlannerFactory,
    *,
    adapters: tuple[Adapter, ...] | None = None,
    planner_name: str = "scripted",
    model: str = "n/a",
) -> EvalReport:
    report = EvalReport(planner_name=planner_name, model=model)
    for task in tasks:
        report.results.append(run_task(task, planner_factory, adapters))
    return report


@dataclass
class VarianceReport:
    """The same suite, run several times. One run is an anecdote.

    Agents are stochastic, so a single score overstates how dependable the
    agent is. This reports the spread, and which tasks moved between runs --
    a task that passes 2 times in 5 is a different fact from one that passes
    5 in 5, and an aggregate hides which is which.
    """

    runs: list[EvalReport] = field(default_factory=list)

    @property
    def scores(self) -> list[int]:
        return [r.passed for r in self.runs]

    @property
    def total(self) -> int:
        return self.runs[0].total if self.runs else 0

    @property
    def mean(self) -> float:
        return statistics.fmean(self.scores) if self.scores else 0.0

    @property
    def stdev(self) -> float:
        return statistics.stdev(self.scores) if len(self.scores) > 1 else 0.0

    @property
    def interval(self) -> tuple[float, float]:
        """An approximate 95% interval for the mean score.

        A normal approximation, which flatters small samples; it is printed
        beside the minimum and maximum so nobody has to trust it alone.
        """
        if len(self.scores) < 2:
            return (self.mean, self.mean)
        half = 1.96 * self.stdev / (len(self.scores) ** 0.5)
        return (max(self.mean - half, 0.0), min(self.mean + half, float(self.total)))

    def per_task(self) -> dict[str, tuple[int, int]]:
        counts: dict[str, tuple[int, int]] = {}
        for report in self.runs:
            for result in report.results:
                passed, seen = counts.get(result.task_id, (0, 0))
                counts[result.task_id] = (passed + int(result.succeeded), seen + 1)
        return counts

    @property
    def unstable(self) -> list[str]:
        """Tasks that passed in some runs and failed in others."""
        return sorted(t for t, (passed, seen) in self.per_task().items() if 0 < passed < seen)

    @property
    def violations(self) -> list[str]:
        return [f"run {i + 1}: {v}" for i, r in enumerate(self.runs) for v in r.violations]

    def text(self) -> str:
        if not self.runs:
            return "no runs"
        first = self.runs[0]
        low, high = self.interval
        lines = [
            "",
            f"minos eval x{len(self.runs)} -- planner={first.planner_name} model={first.model}",
            "=" * 68,
            "",
            "  runs           " + "  ".join(f"{s}/{self.total}" for s in self.scores),
            f"  mean           {self.mean:.2f}/{self.total}  (sd {self.stdev:.2f})",
            f"  range          {min(self.scores)} to {max(self.scores)}",
            f"  ~95% interval  {low:.1f} to {high:.1f}   (normal approximation; see range)",
            "",
            "  per task (passed / runs)",
        ]
        for task_id, (passed, seen) in sorted(self.per_task().items()):
            flag = "   <-- unstable" if 0 < passed < seen else ""
            lines.append(f"    {passed}/{seen}  {task_id}{flag}")
        violations = self.violations
        lines.append("")
        lines.append(
            f"  invariant violations across all runs: {len(violations)}"
            + ("   (all held)" if not violations else "   <-- the runtime broke a promise")
        )
        for violation in violations[:10]:
            lines.append(f"    {violation}")
        lines.append("")
        return "\n".join(lines)

    def to_json(self) -> str:
        low, high = self.interval
        return json.dumps(
            {
                "summary": {
                    "planner": self.runs[0].planner_name if self.runs else "",
                    "model": self.runs[0].model if self.runs else "",
                    "runs": len(self.runs),
                    "tasks": self.total,
                    "scores": self.scores,
                    "mean": round(self.mean, 3),
                    "stdev": round(self.stdev, 3),
                    "interval_95": [round(low, 2), round(high, 2)],
                    "unstable": self.unstable,
                    "invariant_violations": len(self.violations),
                },
                "per_task": {
                    task: {"passed": passed, "runs": seen}
                    for task, (passed, seen) in sorted(self.per_task().items())
                },
                "runs": [json.loads(r.to_json()) for r in self.runs],
            },
            indent=2,
        )


def run_suite_repeatedly(
    tasks: Sequence[Task],
    planner_factory: PlannerFactory,
    runs: int,
    *,
    planner_name: str = "scripted",
    model: str = "n/a",
    progress: Callable[[int, EvalReport], None] | None = None,
) -> VarianceReport:
    """Run the suite ``runs`` times, fresh workspaces and planners every time."""
    variance = VarianceReport()
    for index in range(runs):
        report = run_suite(tasks, planner_factory, planner_name=planner_name, model=model)
        variance.runs.append(report)
        if progress is not None:
            progress(index + 1, report)
    return variance
