"""Eval harness.

Binary task success, plus the three metrics that would catch the runtime
quietly breaking its own promises: unverified effects, rollback success, and
fallback rate. Half the suite is REFUSE tasks -- goals the agent should fail to
complete -- because an agent that scores well on ACHIEVE and badly on REFUSE is
exactly the agent you should not install.
"""

from .harness import EvalReport, TaskResult, run_suite, run_task
from .suite import SUITE, reference_script, scripted_factory
from .task import Task, TaskKind

__all__ = [
    "SUITE",
    "EvalReport",
    "Task",
    "TaskKind",
    "TaskResult",
    "reference_script",
    "run_suite",
    "run_task",
    "scripted_factory",
]
