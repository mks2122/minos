"""The eval harness, and the suite's own integrity.

An eval suite that is never itself tested is a suite that quietly stops
measuring. These tests check three things: that the harness computes its metrics
correctly, that every task in the suite is *achievable* by its reference script,
and that the REFUSE tasks would actually fail if containment broke.
"""

from __future__ import annotations

import json

import pytest

from minos.evals import SUITE, run_suite, run_task
from minos.evals.harness import EvalReport, TaskResult
from minos.evals.suite import reference_script, scripted_factory
from minos.evals.task import TaskKind
from minos.planner.base import Done
from minos.planner.scripted import ScriptedPlanner

# -- the suite itself ------------------------------------------------------


def test_every_task_has_a_reference_solution(tmp_path):
    """A task with no known-good solution cannot tell you anything about a model."""
    missing = [
        task.id
        for task in SUITE
        if any(
            isinstance(step, Done) and "no reference solution" in step.summary
            for step in reference_script(task, tmp_path)
        )
    ]
    assert not missing, f"tasks without a reference solution: {missing}"


def test_task_ids_are_unique():
    ids = [t.id for t in SUITE]
    assert len(ids) == len(set(ids))


def test_suite_has_meaningful_refuse_coverage():
    """Containment is the claim, so it needs real weight in the suite."""
    refuse = [t for t in SUITE if t.kind is TaskKind.REFUSE]
    assert len(refuse) >= 5
    assert len(refuse) / len(SUITE) >= 0.3


@pytest.mark.parametrize("task", SUITE, ids=lambda t: t.id)
def test_reference_script_passes(task):
    """Every task is achievable, and every REFUSE task is genuinely contained."""
    result = run_task(task, scripted_factory)
    assert result.succeeded, f"{task.id}: {result.summary} {result.error}"


@pytest.mark.parametrize(
    "task", [t for t in SUITE if t.kind is TaskKind.REFUSE], ids=lambda t: t.id
)
def test_refuse_tasks_actually_attempt_the_forbidden_thing(task, tmp_path):
    """A REFUSE task passed by a timid planner proves nothing.

    The reference script must really try, so that passing means the runtime
    contained it rather than the planner declining to look.
    """
    script = reference_script(task, tmp_path)
    actions = [s for s in script if not isinstance(s, Done)]
    assert actions, f"{task.id} never attempts anything"


def test_refuse_task_would_fail_if_containment_broke():
    """Negative control: grant the scope the task withholds, and it must fail."""
    task = next(t for t in SUITE if t.id == "refuse.write_outside_scope")
    leaky = type(task)(
        id=task.id,
        goal=task.goal,
        category=task.category,
        setup=task.setup,
        check=task.check,
        # Deliberately too wide: the parent directory, not just the workspace.
        scopes=lambda ws: [f"fs.read:{ws.parent}/**", f"fs.write:{ws.parent}/**"],
        kind=task.kind,
    )
    assert not run_task(leaky, scripted_factory).succeeded


# -- harness metrics -------------------------------------------------------


def _result(**kwargs) -> TaskResult:
    base = dict(
        task_id="t",
        category="c",
        kind="achieve",
        succeeded=True,
        steps=1,
        duration_s=0.1,
        tier_counts={"L1": 1},
        fallback_rate=0.0,
        rollback_attempts=0,
        rollback_successes=0,
        unverified_effects=0,
        verified_effects=1,
        halted=False,
        audit_intact=True,
        summary="",
    )
    base.update(kwargs)
    return TaskResult(**base)  # type: ignore[arg-type]


def test_success_rate():
    report = EvalReport(results=[_result(), _result(succeeded=False)])
    assert report.success_rate == 0.5
    assert report.passed == 1


def test_unverified_effect_rate():
    report = EvalReport(results=[_result(verified_effects=3, unverified_effects=1)])
    assert report.unverified_effect_rate == 0.25


def test_rollback_success_rate_is_one_when_nothing_was_reversed():
    """No reversals is not a failure; it is an absence of evidence."""
    assert EvalReport(results=[_result()]).rollback_success_rate == 1.0


def test_rollback_success_rate_counts_failures():
    report = EvalReport(results=[_result(rollback_attempts=4, rollback_successes=3)])
    assert report.rollback_success_rate == 0.75


def test_fallback_rate_aggregates_across_tasks():
    report = EvalReport(
        results=[
            _result(tier_counts={"L1": 8}),
            _result(tier_counts={"L1": 0, "L2": 2}),
        ]
    )
    assert report.fallback_rate == 0.2


def test_audit_break_is_surfaced():
    assert EvalReport(results=[_result(audit_intact=False)]).audit_breaks == 1


def test_report_text_flags_a_missed_target():
    report = EvalReport(results=[_result(verified_effects=1, unverified_effects=1)])
    assert "target is 0" in report.text()


def test_report_text_confirms_a_met_target():
    assert "target met" in EvalReport(results=[_result()]).text()


def test_report_round_trips_through_json():
    report = EvalReport(results=[_result(task_id="a"), _result(task_id="b")])
    parsed = json.loads(report.to_json())
    assert parsed["summary"]["tasks"] == 2
    assert {r["task_id"] for r in parsed["results"]} == {"a", "b"}


def test_regression_detection():
    """An aggregate can hold steady while two tasks swap places."""
    baseline = json.loads(
        EvalReport(results=[_result(task_id="a"), _result(task_id="b", succeeded=False)]).to_json()
    )
    now = EvalReport(results=[_result(task_id="a", succeeded=False), _result(task_id="b")])
    assert now.success_rate == 0.5  # unchanged headline
    assert now.regressions(baseline) == ["a"]  # but 'a' broke


# -- harness behaviour -----------------------------------------------------


def test_harness_records_a_crashing_planner_as_a_result():
    """A crash is data, not a reason to abandon the run."""

    class Exploding:
        def next_action(self, goal, observations, scopes):
            raise RuntimeError("planner exploded")

    task = next(t for t in SUITE if t.id == "read.cell")
    result = run_task(task, lambda t, ws: Exploding())
    assert not result.succeeded
    assert "planner exploded" in result.error


def test_harness_gives_each_task_a_fresh_workspace():
    task = next(t for t in SUITE if t.id == "write.set_cell")
    first = run_task(task, scripted_factory)
    second = run_task(task, scripted_factory)
    assert first.succeeded and second.succeeded


def test_harness_never_auto_approves_irreversible_effects():
    """An unattended eval must not click through an approval prompt."""
    task = next(t for t in SUITE if t.id == "refuse.spawn_without_approval")
    result = run_task(task, scripted_factory)
    assert result.succeeded  # success here means the spawn did not happen


def test_run_suite_reports_every_task():
    subset = [t for t in SUITE if t.category == "read"]
    report = run_suite(subset, scripted_factory)
    assert report.total == len(subset)
    assert report.passed == len(subset)


def test_a_planner_that_does_nothing_fails_achieve_tasks():
    """Negative control on the harness: doing nothing must not score."""
    task = next(t for t in SUITE if t.id == "write.set_cell")
    idle = run_task(task, lambda t, ws: ScriptedPlanner([Done(summary="", succeeded=True)]))
    assert not idle.succeeded
